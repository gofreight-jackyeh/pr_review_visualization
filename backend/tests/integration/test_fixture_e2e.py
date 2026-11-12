"""End-to-end on the local fixture repo (no network): real git, fake Claude."""

import json
import subprocess
from pathlib import Path

from conftest import git, load_docs, local_request
from fakes import RecordingRunner, SmartClaude, call_kind, ordered, claude_envelope, prompt_data

import make_django_fixture
from pr_explorer.application.ports import ContextDoc
from pr_explorer.bootstrap import build_container
from pr_explorer.infrastructure.process.runner import RunResult


def test_shas_match_git(shop_docs, shop_repo):
    smap, ov, _, _ = shop_docs
    pr = ov["pr"]
    assert pr["base_sha"] == git(shop_repo, "rev-parse", "main")
    assert pr["head_sha"] == git(shop_repo, "rev-parse", "feature/discount")
    assert pr["merge_base_sha"] == git(shop_repo, "merge-base", "main", "feature/discount") == smap["commit_sha"]
    assert ov["system_map_id"] == smap["system_map_id"]


def test_system_map_content(shop_docs):
    smap, _, _, _ = shop_docs
    caps = {c["id"]: c for c in smap["capabilities"]}
    assert set(caps) == {"cap:catalog", "cap:catalog/admin_api", "cap:core", "cap:deploy", "cap:inventory", "cap:orders",
                         "cap:payments", "cap:shop"}
    # layering (schema 2.1.0): catalog has > 50 entry points → split; core is shared; deploy/ is engineering
    assert caps["cap:catalog/admin_api"]["parent_capability_id"] == "cap:catalog" and len(caps["cap:catalog/admin_api"]["entry_point_ids"]) == 55
    assert {c: x["category"] for c, x in caps.items()} == {
        "cap:catalog": "business", "cap:catalog/admin_api": "business", "cap:core": "shared", "cap:deploy": "engineering",
        "cap:inventory": "business", "cap:orders": "business", "cap:payments": "business", "cap:shop": "business"}
    assert all(d["source"] == "static" for d in smap["domains"])  # no Claude: one domain per top-level directory
    assert {x["domain_id"] for x in caps.values() if x["category"] == "business"} == {d["id"] for d in smap["domains"]}
    assert caps["cap:catalog/admin_api"]["domain_id"] == caps["cap:catalog"]["domain_id"] == "domain:catalog"
    assert not any("domain_id" in x for x in caps.values() if x["category"] != "business")
    assert not any(f.get("phases") for f in smap["flows"])  # phases need Claude
    kinds = {e["kind"] for e in smap["entry_points"]}
    assert kinds == {"route", "api", "task", "command", "schedule"}
    assert {e["technical_name"] for e in smap["entities"]} == {"Product", "Order", "OrderItem", "StockItem", "Reservation", "Payment"}
    rel = {(r["source"], r["target"], r["kind"]): r for r in smap["relations"]}
    assert rel[("cap:orders", "cap:payments", "calls")]["confidence"] == "confirmed"
    assert ("cap:orders", "cap:inventory", "calls") in rel and ("cap:payments", "entity:py:payments/models.py::Payment", "uses_entity") in rel
    assert smap["flow_budget"] == {"entry_points_total": 70, "flows_built": 20}
    # interface rings (schema 2.2.0): outer = API routes (with serializer fields) + the path .pr-explorer.yml declares
    ifs = {i["id"]: i for i in smap["interfaces"]}
    outer = {i["technical_name"]: i for i in ifs.values() if i["ring"] == "outer"}
    assert set(outer) == {"/orders/api/<int:pk>/summary/", "/payments/api/history/", "/payments/webhook/", "orders/notifications.py"}
    assert outer["orders/notifications.py"]["kind"] == "outbound_format" and outer["orders/notifications.py"]["detected_by"] == "config"
    assert outer["/payments/api/history/"]["kind"] == "http_api" and outer["/payments/api/history/"]["entry_point_id"]
    assert "amount, id, status" in outer["/payments/api/history/"]["description"]["text"]
    middle = {i["technical_name"]: i["kind"] for i in ifs.values() if i["ring"] == "middle"}
    assert middle["orders.services"] == "module_api" and middle["payments.tasks.capture_payment"] == "task"
    assert "orders.views" not in middle  # nothing outside orders' views layer imports it
    checkout = next(f for f in smap["flows"] if f["technical_name"] == "/orders/checkout/")
    assert {"cap:orders", "cap:catalog", "cap:inventory", "cap:payments"} <= set(checkout["capability_ids"])
    assert {s["kind"] for s in checkout["timeline"]["steps"]} >= {"enter", "call", "create", "write", "emit"}
    capture = next(f for f in smap["flows"] if f["technical_name"] == "task payments.tasks.capture_payment")
    assert [s["kind"] for s in capture["timeline"]["steps"]] == ["enter", "read", "external", "write"]
    assert smap["semantic"]["status"] == "skipped"
    assert not any("business_name" in x for k in ("capabilities", "entities", "entry_points", "flows") for x in smap[k])
    assert "not runtime traces" in smap["disclaimer"].lower()
    assert all(r["side"] == "base" for e in smap["evidence"] for r in e["refs"])


def test_overlay_content(shop_docs):
    smap, ov, _, _ = shop_docs
    statuses = {s["step_change_status"] for fc in ov["flow_changes"] for s in fc["steps"]}
    assert statuses == {"added", "removed", "changed", "unchanged"}
    by_flow = {fc["flow_id"]: fc for fc in ov["flow_changes"]}
    added = by_flow["flow:route:orders/urls.py::checkout/apply-discount/"]
    assert added["change_status"] == "added" and added["flow_id"] in {f["id"] for f in ov["head_additions"]["flows"]}
    checkout = by_flow["flow:route:orders/urls.py::checkout/"]
    removed = [s for s in checkout["steps"] if s["step_change_status"] == "removed"]
    assert removed and "member_price" in removed[0]["technical_label"]
    assert any(s["evidence_status"] == "unknown" for s in checkout["steps"])  # getattr discount dispatch
    assert any(s["evidence_status"] == "possible" and s.get("branch") for s in checkout["steps"])
    assert {e["technical_name"] for e in ov["head_additions"]["entities"]} == {"DiscountCode"}
    rules = {f["rule"] for f in ov["findings"] if f["source"] == "static" and f["status"] == "verified"}
    assert {"call_signature_mismatch", "model_field_without_migration", "removed_symbol_still_referenced"} <= rules
    # a sibling-decorator pattern is a question, not a verified fact
    assert {f["status"] for f in ov["findings"] if f["rule"] == "view_missing_sibling_decorator"} == {"needs_confirmation"}
    sig = next(f for f in ov["findings"] if f["rule"] == "call_signature_mismatch")
    assert sig["capability_ids"] == ["cap:orders"] and "flow:py:orders/tasks.py::reorder_last" in sig["flow_ids"]
    assert sig["business_impact"]
    impacts = {(i["target_kind"], i["impact_status"]) for i in ov["impacts"]}
    assert {("capability", "direct"), ("flow", "direct"), ("flow", "evidence_supported"), ("entry_point", "direct"),
            ("entity", "direct")} <= impacts
    # the cancel flow changed only through a dynamic call to the removed notifications.order_cancelled → unknown
    assert ("flow", "unknown") in impacts
    assert {i["impact_status"] for i in ov["impacts"]} == {"direct", "evidence_supported", "confirmed_dependency", "potential", "unknown"}
    files = {f["path"]: f["capability_id"] for f in ov["changes"]["files"]}
    assert files["orders/services.py"] == "cap:orders" and files["docs/deploy.md"] is None
    checks = ov["architecture_checks"]
    assert all(c["source"] == "static" for c in checks)
    assert all(c["status"] == "verified" for c in checks if c.get("interface_change", {}).get("compatibility") != "unknown")
    rings = [c["ring"] for c in checks]
    assert rings == sorted(rings, key=["outer", "middle", "inner"].index)  # spec §8: outer → middle → inner
    iface = {c["title"].split("：", 1)[-1]: (c["ring"], c["interface_change"]["change"], c["interface_change"]["compatibility"])
             for c in checks if c["check"] == "interface_change"}
    assert iface == {
        "/orders/api/<int:pk>/summary/": ("outer", "modified", "breaking"),  # total renamed to grand_total
        "/payments/api/history/": ("outer", "modified", "compatible"),  # new read-only provider_ref
        "/orders/api/discounts/<str:code>/": ("outer", "added", "compatible"),
        "orders/notifications.py": ("outer", "modified", "unknown"),  # declared outer by .pr-explorer.yml
        "orders.services": ("middle", "modified", "breaking"),  # create_order gains a required parameter
        "orders.discounts": ("middle", "added", "compatible"), "catalog.utils": ("middle", "added", "compatible"),
    }
    summary = next(c for c in checks if c["title"].endswith("/orders/api/<int:pk>/summary/"))
    assert summary["origin"] == "observed" and summary["verdict"] == "violates" and "grand_total" in summary["interface_change"]["detail"]
    ha_ifaces = {i["id"] for i in ov["head_additions"]["interfaces"]}
    assert all(c["interface_change"]["interface_id"] in ha_ifaces | {i["id"] for i in smap["interfaces"]}
               for c in checks if c["check"] == "interface_change")
    assert {c["ring"] for c in checks if c["check"] != "interface_change"} == {"middle", "inner"}
    assert ov["purpose"]["source"] == "pr_description" and ov["purpose"]["text"].startswith("Add discount codes")
    assert ov["behavior_changes"] and all(b["after"]["source"] == "static" for b in ov["behavior_changes"])
    assert [s["id"] for s in ov["walkthrough"]["steps"]] == ["wt:system", "wt:capabilities", "wt:flows", "wt:behavior",
                                                            "wt:impact", "wt:questions"]
    assert ov["walkthrough"]["steps"][0]["title"] == "這個系統在做什麼"  # README is Chinese → zh-Hant
    assert ov["semantic"]["status"] == "skipped" and ov["semantic"]["decision"]["mode"] == "off"


def test_system_map_reused_across_prs_on_the_same_base(shop_repo, home):
    claude = SmartClaude()
    c = build_container(home=home, claude_runner=RecordingRunner(claude))
    paths1, rec1 = c.analyze.analyze_sync(local_request(shop_repo, mode="standard"))
    assert ordered(claude.calls) == ["system_map", "domains", "phases", "pr"] and c.analyze.pipeline.system_map_builds == 1
    paths2, rec2 = c.analyze.analyze_sync(local_request(shop_repo, mode="standard", head="feature/restock-alert"))
    stages = {s["name"]: s for s in rec2["stages"]}
    assert stages["system_map"]["status"] == "skipped" and stages["system_map"]["message"] == f"reused {rec1['system_map_id']}"
    assert c.analyze.pipeline.system_map_builds == 1 and ordered(claude.calls) == ["system_map", "domains", "phases", "pr", "pr"]  # no call (a)
    assert rec2["system_map_id"] == rec1["system_map_id"] and paths2["system_map"] == paths1["system_map"]
    smap, ov2 = load_docs(paths2)
    assert ov2["system_map_id"] == smap["system_map_id"] and ov2["pr"]["head_ref"] == "feature/restock-alert"
    statuses = {s["step_change_status"] for fc in ov2["flow_changes"] for s in fc["steps"]}
    assert {"added", "unchanged"} <= statuses  # restock now e-mails purchasing
    # a different semantic setting is a different map (static names vs business names)
    _, rec3 = c.analyze.analyze_sync(local_request(shop_repo, mode="off", head="feature/restock-alert"))
    assert rec3["system_map_id"] != rec1["system_map_id"] and c.analyze.pipeline.system_map_builds == 2


def test_overlay_cache_reuse_and_version_invalidation(shop_repo, home):
    c = build_container(home=home)
    first, rec1 = c.analyze.analyze_sync(local_request(shop_repo))
    assert c.analyze.pipeline_runs == 1
    aid, status, cached = c.analyze.start(local_request(shop_repo))
    assert (status, cached) == ("done", True) and c.analyze.pipeline_runs == 1
    assert c.status.get(aid)["overlay_id"] == rec1["overlay_id"] and c.status.get(aid)["system_map_id"] == rec1["system_map_id"]
    assert c.analyze.start(local_request(shop_repo, mode="auto"))[2] is False  # different semantic_mode
    c.analyze.analyzer_version = "999.0.0"
    second, rec2 = c.analyze.analyze_sync(local_request(shop_repo))
    assert c.analyze.pipeline_runs == 2 and rec2["overlay_id"] != rec1["overlay_id"] and rec2["system_map_id"] != rec1["system_map_id"]
    smap, ov = load_docs(second)
    assert smap["analyzer_version"] == ov["analyzer_version"] == "999.0.0"


def test_freshness_detects_new_head_commit(fresh_shop, home):
    c = build_container(home=home)
    _, rec = c.analyze.analyze_sync(local_request(fresh_shop))
    assert c.freshness(rec["overlay_id"])["stale"] is False
    git(fresh_shop, "checkout", "-q", "feature/discount")
    (fresh_shop / "orders" / "extra.py").write_text("X = 1\n")
    git(fresh_shop, "add", "-A")
    subprocess.run(["git", "-C", str(fresh_shop), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "more"], check=True)
    f = c.freshness(rec["overlay_id"])
    assert f["stale"] is True and f["current_head_sha"] == git(fresh_shop, "rev-parse", "feature/discount")


def test_user_checkout_untouched(fresh_shop, home):
    before = git(fresh_shop, "status", "--porcelain"), git(fresh_shop, "rev-parse", "--abbrev-ref", "HEAD")
    build_container(home=home).analyze.analyze_sync(local_request(fresh_shop))
    assert (git(fresh_shop, "status", "--porcelain"), git(fresh_shop, "rev-parse", "--abbrev-ref", "HEAD")) == before
    assert len(git(fresh_shop, "worktree", "list").splitlines()) == 1  # our worktrees live in the cache, not here


def test_secret_scrub_end_to_end(shop_docs):
    _, _, paths, _ = shop_docs
    raw = Path(paths["overlay"]).read_text() + Path(paths["system_map"]).read_text()
    assert make_django_fixture.FAKE_GH_TOKEN not in raw and make_django_fixture.FAKE_AWS_SECRET not in raw
    # the token really would have reached the overlay (branch condition) — proven by redaction; the documented
    # secret now only reaches the Claude prompt (architecture docs), see test_fake_claude_both_calls_are_gated
    assert "ghp_[REDACTED]" in raw


def test_context_docs_reach_knowledge_layer_and_cache_key(shop_repo, home):
    c = build_container(home=home)
    c.analyze.analyze_sync(local_request(shop_repo))
    doc = ContextDoc("jira-SHOP-42.md", "## Rules\n- Discounts must never exceed the order total.\n")
    aid, _, cached = c.analyze.start(local_request(shop_repo, context_docs=[doc]))
    assert cached is False
    c.analyze.run(aid)
    rec = c.status.get(aid)
    ov = c.get_overlay(rec["overlay_id"])
    assert "context:jira-SHOP-42.md" in ov["semantic"]["knowledge_sources"] and "repo:README.md" in ov["semantic"]["knowledge_sources"]
    other = ContextDoc("b.md", "x")
    c.analyze.analyze_sync(local_request(shop_repo, context_docs=[doc, other]))
    assert c.analyze.start(local_request(shop_repo, context_docs=[other, doc]))[2] is True  # order-independent


def test_fake_claude_both_calls_are_gated(shop_repo, home):
    claude = SmartClaude()
    runner = RecordingRunner(claude)
    c = build_container(home=home, claude_runner=runner)
    paths, _ = c.analyze.analyze_sync(local_request(shop_repo, mode="standard"))
    smap, ov = load_docs(paths)
    assert ordered(claude.calls) == ["system_map", "domains", "phases", "pr"]
    pr_prompt = runner.calls[-1]["input"]  # the documented secret reaches the prompt only redacted
    assert make_django_fixture.FAKE_AWS_SECRET not in pr_prompt and "AWS_SECRET_ACCESS_KEY=[REDACTED]" in pr_prompt
    # domains + phases: Claude's assignment passed validation; every built flow with ≥ 4 steps is cut 3 ways
    assert [d["business_name"]["text"] for d in smap["domains"]] == ["Selling", "Supply and money"]
    assert all(d["source"] == "semantic" for d in smap["domains"])
    long_flows = [f for f in smap["flows"] if len(f["timeline"]["steps"]) >= 4]
    assert long_flows and all(len(f["phases"]) == 3 for f in long_flows)
    assert all(len(fc["phases"]) == 3 for fc in ov["flow_changes"] if len(fc["steps"]) >= 4)
    hyp, ihyp = sorted((a for a in ov["architecture_checks"] if a["origin"] == "hypothesis"), key=lambda a: a["check"] == "interface_change")
    assert hyp["status"] == "needs_confirmation" and hyp["source"] == "semantic" and "rule_id" not in hyp and hyp["ring"] == "inner"
    # interfaces: Claude may add an outer-impact hypothesis (needs confirmation) and say who each outer change affects
    assert ihyp["check"] == "interface_change" and ihyp["ring"] == "outer" and ihyp["status"] == "needs_confirmation"
    assert ihyp["interface_change"]["compatibility"] == "unknown"
    outer = [a for a in ov["architecture_checks"] if a["check"] == "interface_change" and a["source"] == "static" and a["ring"] == "outer"]
    assert outer and all(a["business_impact"].startswith("Clients of ") for a in outer)
    assert [a["ring"] for a in ov["architecture_checks"]] == sorted((a["ring"] for a in ov["architecture_checks"]),
                                                                   key=["outer", "middle", "inner"].index)
    pr_data = prompt_data(pr_prompt)
    assert pr_data["interface_changes"][0]["ring"] == "outer" and len(pr_data["interface_changes"]) == 7
    # (a) system map: grounded names kept, the invented capability dropped and counted, structure ignored
    assert smap["semantic"]["status"] == "done" and smap["semantic"]["dropped_claims"] == 1
    assert smap["system"]["summary"]["text"] == "An online shop backend." and smap["system"]["summary"]["source"] == "semantic"
    orders = next(x for x in smap["capabilities"] if x["id"] == "cap:orders")
    assert orders["business_name"]["text"] == "Biz orders" and orders["technical_name"] == "orders"
    assert not any(x["id"] == "cap:ghost" for x in smap["capabilities"]) and not any(x["id"] == "evil" for x in smap["components"])
    assert all(s.get("business_label") for f in smap["flows"] for s in f["timeline"]["steps"])
    # (b) PR: purpose/behavior from Claude, invented flow dropped, finding is a question, labels on changed steps
    sem = ov["semantic"]
    assert sem["status"] == "done" and sem["tier"] == "standard" and sem["dropped_claims"] == 1
    assert ov["purpose"] == {"text": "Let customers apply discount codes at checkout.", "source": "semantic",
                             "evidence_refs": ov["purpose"]["evidence_refs"]}
    assert [b["after"]["text"] for b in ov["behavior_changes"]] == ["A discount code reduces the total."]
    [sf] = [f for f in ov["findings"] if f["source"] == "semantic"]
    assert sf["status"] == "needs_confirmation" and sf["changed_file_ids"] == ["file:orders/services.py"]
    labels = [s for fc in ov["flow_changes"][:1] for s in fc["steps"] if s["step_change_status"] != "unchanged"]
    assert labels and all(s["business_label"]["source"] == "semantic" for s in labels)
    unchanged = [s for fc in ov["flow_changes"] for s in fc["steps"] if s["step_change_status"] == "unchanged"]
    assert any(s.get("business_label", {}).get("text", "").startswith("Do ") for s in unchanged)  # reused from the map
    wt = {s["id"]: s["body"] for s in ov["walkthrough"]["steps"]}
    assert wt["wt:behavior"]["text"] == "Checkout now accepts discount codes." and wt["wt:system"]["source"] == "semantic"
    assert any(q["id"].startswith("q:semantic") for q in ov["walkthrough"]["questions"])


def test_semantic_off_shows_technical_names_and_pr_description(shop_docs):
    smap, ov, _, _ = shop_docs
    assert smap["semantic"]["status"] == "skipped" and "summary" not in smap["system"]
    assert all("business_name" not in c and c["technical_name"] for c in smap["capabilities"])
    assert ov["purpose"]["source"] == "pr_description" and ov["purpose"]["evidence_refs"] == ["ev:pr:metadata"]
    assert "ev:pr:metadata" in {e["id"] for e in ov["evidence"]}
    assert "業務說明不可用" in ov["walkthrough"]["steps"][0]["body"]["text"]


def test_claude_unavailable_still_complete_and_map_not_cached_as_final(shop_repo, home):
    c = build_container(home=home, claude_runner=RecordingRunner(lambda a, i: RunResult(127, "", "claude: not found")))
    paths, _ = c.analyze.analyze_sync(local_request(shop_repo, mode="auto"))
    smap, ov = load_docs(paths)
    assert smap["semantic"]["status"] == "unavailable" and ov["semantic"]["status"] == "unavailable"
    assert smap["capabilities"] and ov["flow_changes"] and ov["purpose"]["source"] == "pr_description"
    c.analyze.analyze_sync(local_request(shop_repo, mode="auto", head="feature/restock-alert"))
    assert c.analyze.pipeline.system_map_builds == 2  # a map without its explanation is retried, not reused


def test_malformed_claude_replies_keep_the_static_result(shop_repo, home):
    """Garbage or wrongly-typed replies on either call fail only the semantic part; both documents are stored."""
    def garbage(args, prompt):
        return "not json at all" if call_kind(args) == "system_map" else claude_envelope({"purpose": "a bare string"})

    def wrong_types(args, prompt):
        if call_kind(args) == "system_map":
            return claude_envelope({"capabilities": "oops", "steps": [{"id": ["x"]}], "system_summary": [1]})
        return claude_envelope({"behavior_changes": [{"flow_ids": "flow:x", "after": 3}], "findings": {"a": 1},
                                "walkthrough": [], "questions": [{"question": ["q"]}], "step_labels": [{"step_id": {}}]})

    for i, handler in enumerate([garbage, wrong_types]):
        c = build_container(home=home / str(i), claude_runner=RecordingRunner(handler))
        paths, rec = c.analyze.analyze_sync(local_request(shop_repo, mode="standard"))
        smap, ov = load_docs(paths)
        assert rec["status"] == "done"
        assert smap["capabilities"] and smap["flows"] and ov["flow_changes"] and any(f["source"] == "static" for f in ov["findings"])
        assert ov["purpose"]["source"] == "pr_description"
        # both kinds of malformed reply are retried once (owner decision), then the semantic part fails
        assert smap["semantic"]["status"] == "failed" and "after 1 retry" in smap["semantic"]["error"]
        assert ov["semantic"]["status"] == "failed" and "after 1 retry" in ov["semantic"]["error"]
        assert ("unparseable" if handler is garbage else "malformed") in smap["semantic"]["error"]
        assert not any("business_name" in x for x in smap["capabilities"])
        assert all(d["source"] == "static" for d in smap["domains"])


def test_semantic_content_that_breaks_validation_falls_back_to_static(shop_repo, home, monkeypatch):
    from pr_explorer.infrastructure.persistence.document_fs import FsDocumentStore

    original = FsDocumentStore.save_overlay

    def save_overlay(self, overlay, smap, files):  # any validation failure caused by semantic content keeps the static result
        if overlay.semantic.status == "done":
            raise ValueError("simulated invalid semantic payload")
        return original(self, overlay, smap, files)

    monkeypatch.setattr(FsDocumentStore, "save_overlay", save_overlay)
    c = build_container(home=home, claude_runner=RecordingRunner(SmartClaude()))
    paths, rec = c.analyze.analyze_sync(local_request(shop_repo, mode="standard"))
    _, ov = load_docs(paths)
    assert rec["status"] == "done" and ov["semantic"]["status"] == "failed" and "simulated invalid" in ov["semantic"]["error"]
    assert not any(f["source"] == "semantic" for f in ov["findings"]) and ov["purpose"]["source"] == "pr_description"
    assert not any("business_label" in s and s["business_label"]["text"].startswith("Changed:")
                   for fc in ov["flow_changes"] for s in fc["steps"])


def test_missing_ts_cli_degrades_to_generic(shop_repo, home, tmp_path):
    c = build_container(home=home, ts_cli=tmp_path / "missing-cli.js")
    paths, _ = c.analyze.analyze_sync(local_request(shop_repo))
    smap, ov = load_docs(paths)
    for doc in (smap, ov):
        cov = {x["language"]: x for x in doc["coverage"]}
        assert cov["javascript"]["level"] == "generic" and any("unavailable" in n for n in cov["javascript"]["notes"])
        assert cov["python"]["level"] == "deep"
    assert json.dumps(ov)  # still a complete overlay


def test_partially_explained_map_is_rebuilt_and_map_tier_is_fixed(shop_repo, home):
    """One capability batch failing (e.g. rate limit) must not freeze a half-named map in the cache."""
    from pr_explorer.application.pipeline import PipelineConfig

    smart, seen = SmartClaude(), {"map": 0}

    def flaky(args, prompt):
        if call_kind(args) == "system_map":
            seen["map"] += 1
            assert args[args.index("--max-turns") + 1] == "9"  # standard (8) + 1 structured-output turn, whatever the request's mode
            if seen["map"] == 2:
                return RunResult(1, "", "rate limited")
        return smart(args, prompt)

    c = build_container(home=home, claude_runner=RecordingRunner(flaky), config=PipelineConfig(capability_batch=2))
    paths, _ = c.analyze.analyze_sync(local_request(shop_repo, mode="light"))
    smap, _ = load_docs(paths)
    assert smap["semantic"]["status"] == "done" and smap["semantic"]["error"].startswith("partial:")
    assert 0 < sum("business_name" in x for x in smap["capabilities"]) < len(smap["capabilities"])
    c.analyze.analyze_sync(local_request(shop_repo, mode="light", head="feature/restock-alert"))
    assert c.analyze.pipeline.system_map_builds == 2  # rebuilt, not reused


def test_architecture_rules_and_checks_on_the_discount_pr(shop_docs):
    smap, ov, _, _ = shop_docs
    rules = {r["id"]: r for r in smap["architecture_rules"]}
    layers, forbidden = rules["rule:importlinter:.importlinter.ini:layers"], rules["rule:importlinter:.importlinter.ini:catalog-independent"]
    assert layers["origin"] == forbidden["origin"] == "documented" and (layers["kind"], forbidden["kind"]) == ("layers", "forbidden")
    assert forbidden["source_ref"] == {"side": "base", "path": ".importlinter.ini", "start_line": 20}
    assert {"cap:catalog", "cap:catalog/admin_api", "cap:orders"} <= set(forbidden["scope_capability_ids"])
    iface = rules["rule:inferred:interface:cap:catalog"]
    assert iface["origin"] == "inferred" and iface["statistic"] == "其他能力呼叫 catalog 的 3 次中，有 3 次透過 catalog.services"
    assert "rule:inferred:acyclic" in rules and "rule:inferred:layers:cap:orders" in rules
    checks = {(c["check"], c.get("rule_id")): c for c in ov["architecture_checks"]}
    # documented violation: catalog/services.py now imports orders.discounts
    v = checks[("dependency_direction", forbidden["id"])]
    assert (v["origin"], v["verdict"], v["status"]) == ("documented", "violates", "verified")
    assert v["violating_relation"] == {"source": "cap:catalog", "target": "cap:orders"}
    assert v["pr_refs"] == [{"side": "head", "path": "catalog/services.py", "start_line": 5}]
    # new debt: the PR excuses orders.models → orders.services instead of fixing it (so no `violates` for it)
    d = checks[("new_debt", layers["id"])]
    assert (d["verdict"], d["status"], d["pr_refs"][0]["path"]) == ("new_debt", "verified", ".importlinter.ini")
    assert "orders.models -> orders.services" in d["description"]
    assert ("dependency_direction", layers["id"]) not in checks
    # inferred interface deviation: orders.discounts.discount_label calls catalog.utils
    i = checks[("interface", iface["id"])]
    assert (i["verdict"], i["status"]) == ("violates", "verified") and i["pr_refs"][0]["path"] == "orders/discounts.py"
    assert i["violating_relation"] == {"source": "cap:orders", "target": "cap:catalog"} and 1 <= len(i["existing_refs"]) <= 3
    # new cycle between business capabilities (catalog → orders closes orders → … → catalog)
    c = checks[("cycle", "rule:inferred:acyclic")]
    assert c["verdict"] == "violates" and {"cap:catalog", "cap:orders"} <= set(c["capability_ids"]) and c["existing_refs"]
    # the inferred view → service layering
    lay = checks[("dependency_direction", "rule:inferred:layers:cap:orders")]
    assert "apply_discount_code" in lay["title"] and lay["existing_refs"]
    assert all(x["source"] == "static" for x in ov["architecture_checks"])
