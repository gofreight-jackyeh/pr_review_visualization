"""Speed work on real git + fake Claude: incremental map reuse, parallel batches, single-flight builds, progressive
results, worktree cleanup, and the language / head-addition content fixes."""

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from conftest import git, load_docs, local_request
from fakes import RecordingRunner, SmartClaude, call_kind, claude_envelope, ordered, prompt_data

from pr_explorer.application.pipeline import PipelineConfig
from pr_explorer.bootstrap import build_container
from pr_explorer.infrastructure.process.runner import SubprocessRunner
from pr_explorer.infrastructure.source.git_cache import GitCache

CJK = re.compile(r"[一-鿿]")


def commit(repo: Path, msg: str) -> None:
    git(repo, "add", "-A")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", msg], check=True)


def move_main(repo: Path) -> None:
    """main gains a change in one app (appended, so no other line moves) and a PR branch starts from it."""
    git(repo, "checkout", "-q", "main")
    with open(repo / "inventory" / "services.py", "a") as f:
        f.write("\n# restock thresholds are reviewed quarterly\n")
    commit(repo, "inventory note")
    git(repo, "checkout", "-q", "-b", "feature/on-new-base")
    (repo / "orders" / "constants.py").write_text("MAX_ITEMS = 50\n")
    commit(repo, "orders constant")
    git(repo, "checkout", "-q", "main")


def map_calls(claude: SmartClaude) -> list[list[str]]:
    return [[c["id"] for c in d["capabilities"]] for d in claude.map_digests]


def test_moved_base_reasks_only_the_changed_capability(fresh_shop, home):
    claude = SmartClaude()
    c = build_container(home=home, claude_runner=RecordingRunner(claude))
    p1, rec1 = c.analyze.analyze_sync(local_request(fresh_shop, mode="standard"))
    smap1, _ = load_docs(p1)
    assert len(map_calls(claude)) == 1 and len(map_calls(claude)[0]) == 7  # cold: every non-engineering capability
    move_main(fresh_shop)
    p2, rec2 = c.analyze.analyze_sync(local_request(fresh_shop, mode="standard", head="feature/on-new-base"))
    smap2, _ = load_docs(p2)
    assert rec2["system_map_id"] != rec1["system_map_id"] and smap2["commit_sha"] != smap1["commit_sha"]
    assert map_calls(claude)[1:] == [["cap:inventory"]]  # unchanged capabilities were NOT sent to Claude
    assert claude.map_digests[1]["write_system_summary"] is False  # 1/7 changed ≤ 25%: summary reused
    # same business capabilities → domains reused; unchanged step IDs → phases reused: neither step is asked again
    assert ordered(claude.calls) == ["system_map", "domains", "phases", "pr", "system_map", "pr"]
    assert smap2["domains"] == smap1["domains"] and all(d["source"] == "semantic" for d in smap2["domains"])
    assert [f.get("phases") for f in smap2["flows"]] == [f.get("phases") for f in smap1["flows"]]
    caps = {x["id"]: x for x in smap2["capabilities"]}
    assert all("business_name" in x for x in caps.values() if x["category"] != "engineering") and caps["cap:orders"]["business_name"]["text"] == "Biz orders"
    assert smap2["system"]["summary"]["text"] == "An online shop backend." and smap2["semantic"]["status"] == "done"
    stages = {s["name"]: s["message"] for s in rec2["stages"]}
    assert "reused texts of 6/7 capabilities" in stages["system_map"] and "+ domains" in stages["system_map"]
    # every Claude call kind reports its prompt size and wall time (find the slow call without guessing)
    assert re.search(r"Claude capabilities call \d+k? chars, \d+ s", stages["semantic"]) and re.search(r"PR call \d+k chars, \d+ s", stages["semantic"])
    assert (home / "logs" / "semantic-calls.jsonl").read_text().count('"kind": "pr"') == 2


def test_donor_with_an_older_analyzer_version_still_donates(shop_repo, home):
    claude = SmartClaude()
    c = build_container(home=home, claude_runner=RecordingRunner(claude))
    c.analyze.analyzer_version = "2.0.0"
    _, rec1 = c.analyze.analyze_sync(local_request(shop_repo, mode="standard"))
    c.analyze.analyzer_version = "2.1.0"  # a backend upgrade: new cache keys, same code
    paths, rec2 = c.analyze.analyze_sync(local_request(shop_repo, mode="standard"))
    smap, ov = load_docs(paths)
    assert rec2["system_map_id"] != rec1["system_map_id"] and smap["analyzer_version"] == "2.1.0"
    assert ordered(claude.calls) == ["system_map", "domains", "phases", "pr", "pr"]  # nothing left: no Claude call (a)
    assert all("business_name" in x for x in smap["capabilities"] if x["category"] != "engineering") and smap["semantic"]["status"] == "done"


def test_map_batches_run_in_parallel_with_bounded_concurrency(shop_repo, home):
    smart, lock, state = SmartClaude(), threading.Lock(), {"now": 0, "max": 0}

    def slow(args, prompt):
        if call_kind(args) != "system_map":
            return smart(args, prompt)
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.3)
        with lock:
            state["now"] -= 1
        return smart(args, prompt)

    c = build_container(home=home, claude_runner=RecordingRunner(slow), config=PipelineConfig(capability_batch=1))
    t0 = time.monotonic()
    paths, _ = c.analyze.analyze_sync(local_request(shop_repo, mode="standard"))
    smap, _ = load_docs(paths)
    # 7 one-capability batches + the system summary in its own call (several batches: never riding on one of them)
    assert len(smart.map_digests) == 8 and state["max"] == 4  # overlapping, never more than 4
    assert time.monotonic() - t0 < 8 * 0.3 + 3
    assert all("business_name" in x for x in smap["capabilities"] if x["category"] != "engineering")
    summary_calls = [d for d in smart.map_digests if d["write_system_summary"]]
    assert len(summary_calls) == 1 and summary_calls[0]["capabilities"] == []


def test_two_concurrent_analyses_share_one_map_build(shop_repo, home):
    smart, gate = SmartClaude(), threading.Event()

    def gated(args, prompt):
        if call_kind(args) == "system_map":
            gate.wait(10)
        return smart(args, prompt)

    c = build_container(home=home, claude_runner=RecordingRunner(gated))
    a = c.analyze.start(local_request(shop_repo, mode="standard"))[0]
    b = c.analyze.start(local_request(shop_repo, mode="standard", head="feature/restock-alert"))[0]
    threads = [threading.Thread(target=c.analyze.run, args=(x,)) for x in (a, b)]
    for t in threads:
        t.start()
    time.sleep(1.0)
    gate.set()
    for t in threads:
        t.join(60)
    ra, rb = c.status.get(a), c.status.get(b)
    assert ra["status"] == rb["status"] == "done" and ra["system_map_id"] == rb["system_map_id"]
    assert c.analyze.pipeline.system_map_builds == 1 and smart.calls.count("system_map") == 1
    for r in (ra, rb):
        assert c.get_overlay(r["overlay_id"])["semantic"]["status"] == "done"
    assert c.get_system_map(ra["system_map_id"])["semantic"]["status"] == "done"


def test_static_documents_are_readable_while_semantic_is_pending(shop_repo, home):
    smart, gate = SmartClaude(), threading.Event()

    def gated(args, prompt):
        gate.wait(20)
        return smart(args, prompt)

    c = build_container(home=home, claude_runner=RecordingRunner(gated))
    aid = c.analyze.start(local_request(shop_repo, mode="standard"))[0]
    t = threading.Thread(target=c.analyze.run, args=(aid,))
    t.start()
    deadline = time.monotonic() + 20
    while not c.status.get(aid)["overlay_id"] and time.monotonic() < deadline:
        time.sleep(0.05)
    rec = c.status.get(aid)
    assert rec["status"] == "running" and rec["overlay_id"] and rec["system_map_id"]
    ov, smap = c.get_overlay(rec["overlay_id"]), c.get_system_map(rec["system_map_id"])
    assert ov["semantic"]["status"] == "pending" and smap["semantic"]["status"] == "pending"
    assert ov["flow_changes"] and ov["findings"] and ov["purpose"]["source"] == "pr_description"
    gate.set()
    t.join(60)
    rec = c.status.get(aid)
    final_ov, final_map = c.get_overlay(rec["overlay_id"]), c.get_system_map(rec["system_map_id"])
    assert rec["status"] == "done" and final_ov["semantic"]["status"] == "done" and final_map["semantic"]["status"] == "done"
    assert final_ov["purpose"]["source"] == "semantic" and final_map["capabilities"][0]["business_name"]
    assert final_ov["overlay_id"] == ov["overlay_id"]  # replaced in place
    # the final overlay's findings speak the map's business names
    sig = next(f for f in final_ov["findings"] if f["rule"] == "call_signature_mismatch")
    assert "Biz orders" in sig["business_impact"]


def test_head_additions_get_business_names_from_the_pr_call(shop_repo, home):
    claude = SmartClaude()
    c = build_container(home=home, claude_runner=RecordingRunner(claude))
    paths, _ = c.analyze.analyze_sync(local_request(shop_repo, mode="standard"))
    _, ov = load_docs(paths)
    flow = next(f for f in ov["head_additions"]["flows"] if f["technical_name"] == "/orders/checkout/apply-discount/")
    assert flow["business_name"]["text"] == "New /orders/checkout/apply-discount/" and flow["business_name"]["evidence_refs"]
    assert ov["head_additions"]["entities"][0]["business_name"]["text"] == "New DiscountCode"


def test_static_findings_follow_the_repository_language(shop_docs):
    _, ov, _, _ = shop_docs  # the shop README is Traditional Chinese
    static = [f for f in ov["findings"] if f["source"] == "static"]
    assert static and all(CJK.search(f["title"]) and CJK.search(f["description"]) for f in static)
    sig = next(f for f in static if f["rule"] == "call_signature_mismatch")
    assert "參數" in sig["title"] and sig["business_impact"].startswith("影響的業務能力")


def test_worktrees_are_removed_after_an_analysis_and_stale_ones_pruned(shop_repo, home):
    c = build_container(home=home)
    _, rec = c.analyze.analyze_sync(local_request(shop_repo))
    wt = home / "worktrees"
    assert not wt.exists() or not any(p for p in wt.rglob("*") if p.is_dir() and (p / ".git").exists())
    assert c.freshness(rec["overlay_id"])["stale"] is False  # freshness never needed a worktree
    # a worktree left behind by an earlier (killed) server is pruned at the next start; unrelated dirs survive
    cache = GitCache(home, SubprocessRunner())
    key = next((home / "repos").glob("*.git")).name.removesuffix(".git")
    left = cache.worktree(key, git(shop_repo, "rev-parse", "main"))
    other = wt / "not-ours"
    other.mkdir(parents=True)
    assert (left / ".git").exists()
    assert build_container(home=home).prune_worktrees() == [left.resolve()]
    assert not left.exists() and other.exists()
    assert len(cache.git(["worktree", "list"], key=key).splitlines()) == 1  # only the bare repo itself


def test_a_stored_2_0_0_map_still_donates(shop_repo, home):
    """Across the 2.0.0 → 2.1.0 schema change: texts of unchanged capabilities are reused (IDs re-validated); only the
    split capability, the domain step and the phases are asked."""
    import json

    claude = SmartClaude()
    c = build_container(home=home, claude_runner=RecordingRunner(claude))
    p1, _ = c.analyze.analyze_sync(local_request(shop_repo, mode="standard"))
    path = Path(p1["system_map"])
    doc = json.loads(path.read_text())
    doc["schema_version"] = "2.0.0"  # what a 2.0.0 analyzer stored: no domains, categories, splits, phases or rules
    for k in ("domains", "architecture_rules"):
        doc.pop(k)
    for x in doc["capabilities"]:
        for k in ("category", "parent_capability_id", "domain_id"):
            x.pop(k, None)
    for f in doc["flows"]:
        f.pop("phases", None)
    path.unlink()
    path.with_name(path.name.replace(".v2.1.0.", ".v2.0.0.")).write_text(json.dumps(doc))
    claude.calls.clear()
    claude.map_digests.clear()
    c.analyze.analyzer_version = "2.1.1"
    p2, rec = c.analyze.analyze_sync(local_request(shop_repo, mode="standard"))
    smap, _ = load_docs(p2)
    assert ordered(claude.calls) == ["system_map", "domains", "phases", "pr"]
    assert map_calls(claude) == [["cap:catalog", "cap:catalog/admin_api"]]  # the split changed only their file sets
    caps = {x["id"]: x for x in smap["capabilities"]}
    assert caps["cap:orders"]["business_name"]["text"] == "Biz orders" and smap["semantic"]["status"] == "done"
    assert all(d["source"] == "semantic" for d in smap["domains"]) and any(f.get("phases") for f in smap["flows"])
    assert "reused texts of 5/7 capabilities" in {s["name"]: s["message"] for s in rec["stages"]}["system_map"]


def move_main_widely(repo: Path) -> None:
    """main changes three of the seven explained capabilities (3/7 > 25%): the system summary must be re-asked."""
    git(repo, "checkout", "-q", "main")
    for app in ("inventory", "orders", "payments"):
        with open(repo / app / "models.py", "a") as f:
            f.write(f"\n# {app}: reviewed\n")
    commit(repo, "three apps")
    git(repo, "checkout", "-q", "-b", "feature/on-wide-base")
    (repo / "orders" / "constants.py").write_text("MAX_ITEMS = 50\n")
    commit(repo, "orders constant")
    git(repo, "checkout", "-q", "main")


def test_summary_is_reasked_over_25_percent_and_survives_a_timed_out_batch(fresh_shop, home):
    """Regression: when ~30% of capabilities changed → summary re-asked, but it rode on
    the first capability batch, that batch timed out, and the map ended without a system summary."""
    from pr_explorer.infrastructure.process.runner import RunResult

    claude, mode = SmartClaude(), {"batches": "ok", "summary": "ok"}

    def handler(args, prompt):
        if call_kind(args) == "system_map":
            data = prompt_data(prompt)
            which = "batches" if data["capabilities"] else "summary"
            if mode[which] == "timeout":
                claude.map_digests.append(data)
                return RunResult(124, "", "claude: timed out after 600s")
            if which == "summary" and mode["summary"] == "new":
                claude.map_digests.append(data)
                return claude_envelope({"system_summary": {"text": "The shop, re-explained.", "cites": [d["evidence_id"] for d in data["docs"][:1]]}})
        return claude(args, prompt)

    cfg = PipelineConfig(capability_batch=2)  # several batches, like a large repo
    c = build_container(home=home, claude_runner=RecordingRunner(handler), config=cfg)
    p1, _ = c.analyze.analyze_sync(local_request(fresh_shop, mode="standard"))
    assert load_docs(p1)[0]["system"]["summary"]["text"] == "An online shop backend."
    move_main_widely(fresh_shop)
    claude.map_digests.clear()
    mode.update(batches="timeout", summary="new")
    p2, rec2 = c.analyze.analyze_sync(local_request(fresh_shop, mode="standard", head="feature/on-wide-base"))
    smap2, _ = load_docs(p2)
    asked = [d for d in claude.map_digests if d["write_system_summary"]]
    assert len(asked) == 1 and asked[0]["capabilities"] == []  # re-asked (3/7 > 25%), in its own call
    assert smap2["system"]["summary"]["text"] == "The shop, re-explained."  # despite every capability batch timing out
    assert "failed" in {s["name"]: s["message"] for s in rec2["stages"]}["semantic"]

    # the re-ask itself fails too → the earlier map's (still grounded) summary is kept rather than none
    shutil.rmtree(home)
    c = build_container(home=home, claude_runner=RecordingRunner(handler), config=cfg)
    mode.update(batches="ok", summary="ok")
    git(fresh_shop, "checkout", "-q", "main")
    git(fresh_shop, "reset", "-q", "--hard", "HEAD~1")  # back to the first base
    c.analyze.analyze_sync(local_request(fresh_shop, mode="standard"))
    git(fresh_shop, "reset", "-q", "--hard", "HEAD@{1}")
    mode.update(summary="timeout")
    p3, _ = c.analyze.analyze_sync(local_request(fresh_shop, mode="standard", head="feature/on-wide-base"))
    assert load_docs(p3)[0]["system"]["summary"]["text"] == "An online shop backend."
