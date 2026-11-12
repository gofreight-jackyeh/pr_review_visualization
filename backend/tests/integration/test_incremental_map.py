"""Speed work on real git + fake Claude: incremental map reuse, parallel batches, single-flight builds, progressive
results, worktree cleanup, and the language / head-addition content fixes."""

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import git, load_docs, local_request
from fakes import RecordingRunner, SmartClaude, call_kind, claude_envelope, ordered, prompt_data

from pr_explorer.application.pipeline import PipelineConfig
from pr_explorer.bootstrap import build_container


@pytest.fixture(autouse=True)
def _english(monkeypatch):
    """SmartClaude replies in English; reuse only takes donor texts in the output language."""
    monkeypatch.setenv("PR_EXPLORER_LANG", "en")
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


def test_moved_base_with_an_implementation_only_change_reuses_every_text(fresh_shop, home):
    """2.6.0: main changed inventory's code but nothing business-facing (a comment), so no capability is re-explained
    (before 2.6.0 every blob change re-asked its capability: here cap:inventory)."""
    claude = SmartClaude()
    c = build_container(home=home, claude_runner=RecordingRunner(claude))
    p1, rec1 = c.analyze.analyze_sync(local_request(fresh_shop, mode="standard"))
    smap1, _ = load_docs(p1)
    assert len(map_calls(claude)) == 1 and len(map_calls(claude)[0]) == 7  # cold: every non-engineering capability
    move_main(fresh_shop)
    p2, rec2 = c.analyze.analyze_sync(local_request(fresh_shop, mode="standard", head="feature/on-new-base"))
    smap2, _ = load_docs(p2)
    assert rec2["system_map_id"] != rec1["system_map_id"] and smap2["commit_sha"] != smap1["commit_sha"]
    assert map_calls(claude)[1:] == []  # implementation-only: nothing re-asked
    # same business capabilities → domains reused; unchanged step IDs → phases reused: neither step is asked again
    assert ordered(claude.calls) == ["system_map", "domains", "phases", "pr", "pr"]
    assert smap2["domains"] == smap1["domains"] and all(d["source"] == "semantic" for d in smap2["domains"])
    assert [f.get("phases") for f in smap2["flows"]] == [f.get("phases") for f in smap1["flows"]]
    caps = {x["id"]: x for x in smap2["capabilities"]}
    assert all("business_name" in x for x in caps.values() if x["category"] != "engineering") and caps["cap:orders"]["business_name"]["text"] == "Biz orders"
    assert smap2["system"]["summary"]["text"] == "An online shop backend." and smap2["semantic"]["status"] == "done"
    # the reused texts keep where they were written and count the base change
    inv1, inv2 = (next(x for x in m["capabilities"] if x["id"] == "cap:inventory")["text_basis"] for m in (smap1, smap2))
    assert inv2["surface"] == inv1["surface"] and inv2["written_at"] == inv1["written_at"]
    assert inv2["written_commit"] == smap1["commit_sha"] and inv2["base_changes"] == 1 and inv2["changed_lines"] == 2
    stages = {s["name"]: s["message"] for s in rec2["stages"]}
    assert "reused texts of 7/7 capabilities" in stages["system_map"] and "+ domains" in stages["system_map"]
    assert re.search(r"PR call \d+k chars, \d+ s", stages["semantic"])
    assert (home / "logs" / "semantic-calls.jsonl").read_text().count('"kind": "pr"') == 2


def move_main_surface(repo: Path) -> None:
    """main adds a field to an inventory model (business-facing: the entity's fields) and a PR branch starts from it."""
    git(repo, "checkout", "-q", "main")
    models = repo / "inventory" / "models.py"
    models.write_text(models.read_text().replace(
        "    quantity = models.PositiveIntegerField(default=0)\n",
        "    quantity = models.PositiveIntegerField(default=0)\n    location = models.CharField(max_length=40, blank=True)\n", 1))
    commit(repo, "inventory: stock location")
    git(repo, "checkout", "-q", "-b", "feature/on-new-base")
    (repo / "orders" / "constants.py").write_text("MAX_ITEMS = 50\n")
    commit(repo, "orders constant")
    git(repo, "checkout", "-q", "main")


def test_moved_base_reasks_only_the_capability_whose_surface_changed(fresh_shop, home):
    claude = SmartClaude()
    c = build_container(home=home, claude_runner=RecordingRunner(claude))
    p1, rec1 = c.analyze.analyze_sync(local_request(fresh_shop, mode="standard"))
    smap1, _ = load_docs(p1)
    move_main_surface(fresh_shop)
    p2, rec2 = c.analyze.analyze_sync(local_request(fresh_shop, mode="standard", head="feature/on-new-base"))
    smap2, _ = load_docs(p2)
    assert map_calls(claude)[1:] == [["cap:inventory"]]  # unchanged capabilities were NOT sent to Claude
    assert claude.map_digests[1]["write_system_summary"] is False  # 1/7 changed ≤ 25%: summary reused
    assert ordered(claude.calls) == ["system_map", "domains", "phases", "pr", "system_map", "pr"]
    inv = next(x for x in smap2["capabilities"] if x["id"] == "cap:inventory")
    assert inv["business_name"]["text"] == "Biz inventory" and inv["text_basis"]["written_commit"] == smap2["commit_sha"]
    assert inv["text_basis"]["base_changes"] == 0 and inv["text_basis"]["surface"] != next(
        x for x in smap1["capabilities"] if x["id"] == "cap:inventory")["text_basis"]["surface"]
    stages = {s["name"]: s["message"] for s in rec2["stages"]}
    assert "reused texts of 6/7 capabilities" in stages["system_map"] and "re-explained: 1 surface" in stages["system_map"]
    assert re.search(r"Claude capabilities call \d+k? chars, \d+ s", stages["semantic"]) and re.search(r"PR call \d+k chars, \d+ s", stages["semantic"])


def move_main_shift(repo: Path) -> None:
    """main inserts a comment at the top of inventory/models.py (every line below moves) and a docstring inside the
    StockItem class (the range its entity cites changes; its fields do not); a PR branch starts from it."""
    git(repo, "checkout", "-q", "main")
    models = repo / "inventory" / "models.py"
    models.write_text("# stock levels\n" + models.read_text().replace(
        "class StockItem(TimeStampedModel):\n", "class StockItem(TimeStampedModel):\n    \"\"\"Units on hand per product.\"\"\"\n\n", 1))
    commit(repo, "inventory: comments")
    git(repo, "checkout", "-q", "-b", "feature/on-new-base")
    (repo / "orders" / "constants.py").write_text("MAX_ITEMS = 50\n")
    commit(repo, "orders constant")
    git(repo, "checkout", "-q", "main")


def test_moved_evidence_is_remapped_and_only_an_element_whose_cited_code_changed_is_reasked(fresh_shop, home):
    claude = SmartClaude()
    c = build_container(home=home, claude_runner=RecordingRunner(claude))
    p1, _ = c.analyze.analyze_sync(local_request(fresh_shop, mode="standard"))
    smap1, _ = load_docs(p1)
    move_main_shift(fresh_shop)
    p2, rec2 = c.analyze.analyze_sync(local_request(fresh_shop, mode="standard", head="feature/on-new-base"))
    smap2, _ = load_docs(p2)
    e1 = {e["technical_name"]: e for e in smap1["entities"]}
    e2 = {e["technical_name"]: e for e in smap2["entities"]}
    # one partial call: the inventory capability with only the StockItem entity (the class it cites changed)
    asked = claude.map_digests[1:]
    assert len(asked) == 1 and [x["id"] for x in asked[0]["capabilities"]] == ["cap:inventory"]
    assert [x["id"] for x in asked[0]["entities"]] == [e2["StockItem"]["id"]]
    assert asked[0]["entry_points"] == [] and asked[0]["flows"] == []
    assert e2["StockItem"]["business_name"]["text"] == "Data StockItem"  # rewritten by that call
    # Reservation moved three lines down: its reused text cites the remapped evidence
    old_ev, new_ev = e1["Reservation"]["business_name"]["evidence_refs"], e2["Reservation"]["business_name"]["evidence_refs"]
    assert e2["Reservation"]["business_name"]["text"] == "Data Reservation" and old_ev != new_ev
    assert set(new_ev) <= {e["id"] for e in smap2["evidence"]}
    inv = next(x for x in smap2["capabilities"] if x["id"] == "cap:inventory")
    assert inv["text_basis"]["written_commit"] == smap1["commit_sha"]  # the capability's own texts were kept
    msg = {s["name"]: s["message"] for s in rec2["stages"]}["system_map"]
    assert "reused texts of 7/7 capabilities" in msg and re.search(r"[1-9]\d* remapped", msg), msg
    assert "1 element(s) of 1 reused" in msg, msg


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

    c = build_container(home=home, claude_runner=RecordingRunner(slow), config=PipelineConfig(capability_batch=1, output_language="en"))
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


def test_worktrees_are_kept_idle_after_an_analysis_and_stale_ones_pruned(shop_repo, home):
    c = build_container(home=home)
    _, rec = c.analyze.analyze_sync(local_request(shop_repo))
    wt = home / "worktrees"
    kept = {p.resolve() for p in wt.glob("*/*") if (p / ".git").exists()}
    assert {p.name for p in kept} == {git(shop_repo, "rev-parse", r) for r in ("main", "feature/discount")}
    assert c.freshness(rec["overlay_id"])["stale"] is False  # freshness never needed a worktree
    # worktrees left behind by an earlier server (idle or killed mid-analysis) are pruned at the next start;
    # unrelated dirs survive
    cache = GitCache(home, SubprocessRunner())
    key = next((home / "repos").glob("*.git")).name.removesuffix(".git")
    left = cache.worktree(key, git(shop_repo, "rev-parse", "main"))  # held by this (killed) server
    other = wt / "not-ours"
    other.mkdir(parents=True)
    assert (left / ".git").exists()
    assert set(build_container(home=home).prune_worktrees()) == kept | {left.resolve()}
    assert not left.exists() and other.exists()
    assert len(cache.git(["worktree", "list"], key=key).splitlines()) == 1  # only the bare repo itself


def test_an_idle_worktree_is_reused_and_recycled_to_exactly_the_other_commit(fresh_shop, home):
    """Same commit: the idle checkout is reset in place. Another commit: the idle checkout is moved and holds exactly that
    commit's files (stray and edited files reset); at most MAX_IDLE stay idle, and they expire after idle_ttl."""
    move_main(fresh_shop)
    cache = GitCache(home, SubprocessRunner(), idle_ttl=600)
    cache.ensure_clone(key := "local-shop", str(fresh_shop))
    a, b, d = (git(fresh_shop, "rev-parse", r) for r in ("main~1", "main", "feature/discount"))
    pa = cache.worktree(key, a)
    (pa / "stray.txt").write_text("not in any commit")
    (pa / "README.md").write_text("changed in place")
    cache.release_worktree(key, a)
    assert (pa / ".git").exists()  # idle, not removed
    assert cache.worktree(key, a) == pa and not (pa / "stray.txt").exists()  # same commit: reset in place
    assert (pa / "README.md").read_text() != "changed in place"
    cache.release_worktree(key, a)
    pb = cache.worktree(key, b)
    assert not pa.exists() and pb.name == b
    tracked = set(cache.git(["ls-tree", "-r", "--name-only", b], key=key).split())
    on_disk = {str(p.relative_to(pb)) for p in pb.rglob("*") if p.is_file() and p.name != ".git"}
    assert on_disk == tracked
    assert all((pb / f).read_bytes() == subprocess.run(["git", "--git-dir", str(cache.repo_dir(key)), "show", f"{b}:{f}"],
                                                        capture_output=True, check=True).stdout for f in tracked)
    pa = cache.worktree(key, a)
    pd = cache.worktree(key, d)
    for sha in (b, a, d):
        cache.release_worktree(key, sha)
    assert not pb.exists() and pa.exists() and pd.exists()  # the oldest idle one went (MAX_IDLE = 2)
    assert cache.remove_expired(now=time.monotonic() + 601) == [pa, pd]
    assert not pa.exists() and not pd.exists()
    assert len(cache.git(["worktree", "list"], key=key).splitlines()) == 1


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
        for k in ("category", "parent_capability_id", "domain_id", "text_basis"):
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
    """main changes the surface of three of the seven explained capabilities (3/7 > 25%): the system summary must be
    re-asked."""
    git(repo, "checkout", "-q", "main")
    for app in ("inventory", "orders", "payments"):  # a new model each: a business-facing (surface) change
        with open(repo / app / "models.py", "a") as f:
            f.write(f"\n\nclass {app.title()}Note(models.Model):\n    text = models.TextField()\n")
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

    cfg = PipelineConfig(capability_batch=2, output_language="en")  # several batches, like a large repo
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
    assert "re-explained: 3 surface" in {s["name"]: s["message"] for s in rec2["stages"]}["system_map"]

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


def _run_pr(repo: Path, home: Path, early: bool, fail_orders: bool = False, hold: bool | None = None):
    """One analysis with SmartClaude. With `early`, the domain and phase calls hold until the PR call has arrived, so
    the analysis only finishes promptly when the PR call does not wait for them. Returns (PR call data, events, docs,
    record)."""
    from pr_explorer.infrastructure.process.runner import RunResult

    smart, pr_data, events, pr_seen = SmartClaude(), [], [], threading.Event()

    def handler(args, prompt):
        kind = call_kind(args)
        if kind == "pr":
            pr_data.append(prompt_data(prompt))
            events.append("pr")
            pr_seen.set()
        elif kind in ("domains", "phases") and (early if hold is None else hold):
            events.append(f"{kind}:{'after pr' if pr_seen.wait(20) else 'gave up waiting'}")
        elif kind == "system_map" and fail_orders and "cap:orders" in [c["id"] for c in prompt_data(prompt)["capabilities"]]:
            return RunResult(124, "", "claude: timed out after 600s")
        return smart(args, prompt)

    cfg = PipelineConfig(capability_batch=2, output_language="en", pr_early_start=early)  # several batches
    c = build_container(home=home, claude_runner=RecordingRunner(handler), config=cfg)
    paths, rec = c.analyze.analyze_sync(local_request(repo, mode="standard"))
    return pr_data, events, load_docs(paths), rec


def _stable(doc: dict) -> dict:
    """A document without its generation time and the call timings in its messages."""
    out = {k: v for k, v in doc.items() if k not in ("generated_at", "semantic")}
    for c in out.get("capabilities", []):
        (c.get("text_basis") or {}).pop("written_at", None)
    return out


def test_early_pr_call_gets_the_same_prompt_without_waiting_for_domains_and_phases(shop_repo, tmp_path):
    wait_data, wait_events, (wait_map, wait_ov), _ = _run_pr(shop_repo, tmp_path / "a", early=False)
    early_data, early_events, (early_map, early_ov), rec = _run_pr(shop_repo, tmp_path / "b", early=True)
    assert len(wait_data) == len(early_data) == 1
    assert early_data[0] == wait_data[0]  # byte-identical PR prompt data: same names, labels, summary
    assert early_data[0]["system"]["summary"] == "An online shop backend."
    assert any(e["name"].startswith("Flow ") for e in early_data[0]["elements"] + early_data[0]["flow_changes"]
               if "name" in e)  # the early call already saw Claude's names, not technical ones
    # the PR call went out while the domain and phase calls were still open
    assert early_events[0] == "pr" and all(e.endswith("after pr") for e in early_events[1:]), early_events
    stages = {s["name"]: s["message"] for s in rec["stages"]}
    assert "PR call started before the map's remaining calls finished" in stages["semantic"]
    assert "(first: the" in stages["system_map"]  # the capabilities this PR touches were asked first
    assert _stable(early_map) == _stable(wait_map) and _stable(early_ov) == _stable(wait_ov)


def test_early_pr_call_falls_back_to_the_whole_map_when_a_batch_it_needs_failed(shop_repo, tmp_path):
    wait_data, _, (_, wait_ov), _ = _run_pr(shop_repo, tmp_path / "a", early=False, fail_orders=True)
    early_data, _, (_, early_ov), rec = _run_pr(shop_repo, tmp_path / "b", early=True, fail_orders=True, hold=False)
    assert early_data == wait_data and len(early_data) == 1
    assert "PR call started before" not in {s["name"]: s["message"] for s in rec["stages"]}["semantic"]
    assert _stable(early_ov) == _stable(wait_ov)
