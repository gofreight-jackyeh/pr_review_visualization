"""Schema 2.1.0+ layering: capability split + category, domains, phases, import-linter contracts, retry on malformed replies."""

import copy
from pathlib import Path

import pytest
from fakes import RecordingRunner, claude_envelope
from test_system_map import SRC, analyze

from pr_explorer.application.ports import SemanticRequest
from pr_explorer.domain.model.document import Repo, SemanticStatus
from pr_explorer.domain.services.domains import gate_domains, reuse_domains, static_domains
from pr_explorer.domain.services.map_reuse import reuse_texts
from pr_explorer.domain.services.semantic_gate import Resolver, apply_phases, gate_phases
from pr_explorer.domain.services.system_map_builder import assemble_system_map, build_flows
from pr_explorer.domain.services.system_structure import SPLIT_MAX_ENTRY_POINTS, build_structure
from pr_explorer.infrastructure.analyzers.generic.languages import detect_language
from pr_explorer.infrastructure.knowledge.import_linter import parse_ini, parse_pyproject
from pr_explorer.infrastructure.persistence.document_serializer import to_json
from pr_explorer.infrastructure.process.runner import RunResult
from pr_explorer.infrastructure.semantic.claude_cli import ClaudeCliSemanticAnalyzer


def big_repo(n_routes: int) -> dict[str, str]:
    """SRC plus `shop/reports/` with n generated routes, `shop/formatting/` (no entry points), `shop/tools/` and `scripts/`."""
    src = dict(SRC)
    src["shop/reports/__init__.py"] = ""
    src["shop/reports/views.py"] = "".join(f"def r{i}(request):\n    return {i}\n\n\n" for i in range(n_routes))
    src["shop/reports/urls.py"] = ("from django.urls import path\n\nfrom . import views\n\nurlpatterns = [\n"
                                   + "".join(f"    path('r{i}/', views.r{i}),\n" for i in range(n_routes)) + "]\n")
    src["shop/formatting/__init__.py"] = ""
    src["shop/formatting/fmt.py"] = "def fmt(x):\n    return str(x)\n"
    src["shop/tools/__init__.py"] = ""
    src["shop/tools/reindex.py"] = "def reindex():\n    return None\n"
    src["scripts/__init__.py"] = ""
    src["scripts/backfill.py"] = "def run():\n    return None\n"
    src["proj/urls.py"] = src["proj/urls.py"].replace("]\n", "    path('reports/', include('shop.reports.urls')),\n]\n")
    return src


def make_map(tmp_path, src):
    sg = analyze(tmp_path, src)
    st = build_structure(sg, detect_language)
    flows, built = build_flows(sg, st, 20)
    return sg, st, assemble_system_map(sg, st, flows, built, repo=Repo("shop"), commit_sha="a" * 40, analyzer_version="t",
                                       system_map_id="sm_t", generated_at="2026-01-01T00:00:00Z", coverage=[],
                                       semantic=SemanticStatus("skipped"))


# ---------------------------------------------------------------- split + category

def test_split_over_threshold_by_sub_directory_with_stable_ids(tmp_path):
    under = build_structure(analyze(tmp_path / "a", big_repo(SPLIT_MAX_ENTRY_POINTS - 3)), detect_language)
    assert "cap:shop/reports" not in under.capabilities  # 47 + 3 shop routes = 50: not over the threshold
    sg = analyze(tmp_path / "b", big_repo(60))
    st = build_structure(sg, detect_language)
    reports = st.capabilities["cap:shop/reports"]
    assert reports.parent_capability_id == "cap:shop" and len(reports.entry_point_ids) == 60
    assert "cap:shop/formatting" not in st.capabilities  # fewer than SPLIT_MIN_CHILD_ENTRY_POINTS: merged into "other"
    other = st.capabilities["cap:shop/*"]
    assert other.parent_capability_id == "cap:shop" and other.path == "shop/*" and other.technical_name == "Other"
    assert st.node_cap["node:py:shop/formatting/fmt.py"] == "cap:shop/*"
    assert st.node_cap["node:py:shop/tools/reindex.py"] == "cap:shop"  # engineering sub-directories stay with the parent
    assert set(st.capabilities["cap:shop"].entity_ids) == {"entity:py:shop/models.py::Order", "entity:py:shop/models.py::Line"}
    # deterministic: same IDs on a rebuild, and the PR head can reuse the map's split verbatim
    again = build_structure(analyze(tmp_path / "c", big_repo(60)), detect_language)
    assert sorted(again.capabilities) == sorted(st.capabilities)
    forced = build_structure(sg, detect_language, splits=dict(st.caps.subs))
    assert sorted(forced.capabilities) == sorted(st.capabilities)


def test_categories_engineering_shared_business(tmp_path):
    src = dict(SRC)
    src["common/__init__.py"] = ""
    src["common/money.py"] = "def cents(x):\n    return x\n"
    for app in ("a", "b", "c"):
        src[f"{app}/__init__.py"] = ""
        src[f"{app}/apps.py"] = "from django.apps import AppConfig\n"
        src[f"{app}/logic.py"] = f"from common.money import cents\n\n\ndef go_{app}():\n    return cents(1)\n"
    src["scripts/__init__.py"] = ""
    src["scripts/backfill.py"] = "from common.money import cents\n\n\ndef run():\n    return cents(2)\n"
    st = build_structure(analyze(tmp_path, src), detect_language)
    cat = {c: x.category for c, x in st.capabilities.items()}
    assert cat["cap:scripts"] == "engineering"  # path pattern + no entry points / entities
    assert cat["cap:common"] == "shared"  # 3 of 5 other non-engineering capabilities import it, no entry points
    assert cat["cap:shop"] == cat["cap:billing"] == cat["cap:a"] == "business"
    st2 = build_structure(analyze(tmp_path / "x", {**SRC, "tools/__init__.py": "", "tools/apps.py": "x = 1\n"}), detect_language)
    assert st2.capabilities["cap:tools"].category == "engineering"


# ---------------------------------------------------------------- domains

def test_static_domains_one_per_top_level_directory(tmp_path):
    _, _, smap = make_map(tmp_path, big_repo(60))
    doms = {d.id: d for d in smap.domains}
    by = {c.id: c for c in smap.capabilities}
    assert by["cap:shop/reports"].domain_id == by["cap:shop"].domain_id == "domain:shop" and doms["domain:shop"].source == "static"
    assert by["cap:scripts"].domain_id is None  # engineering: no domain
    assert all(c.domain_id in doms for c in smap.capabilities if c.category == "business")


def test_static_domains_split_a_dominant_directory_like_a_monorepo_app():
    """A monorepo keeps all its apps under one directory: one top-level domain per directory would put most capabilities in "app".
    Its sub-directories become the domains instead, the smallest merged so there are at most 10."""
    from pr_explorer.domain.model.document import Capability

    def cap(path, eps=1, parent=None, category="business"):
        return Capability(f"cap:{path}", path.rsplit("/", 1)[-1], path, [f"ev:{path}"], entry_point_ids=[f"ep:{path}:{i}" for i in range(eps)],
                          parent_capability_id=parent, category=category)

    caps = [cap("app", 20), cap("app/*", 20, "cap:app"),
            cap("app/catalog", 0), *(cap(f"app/catalog/{x}", 10, "cap:app/catalog") for x in ("search", "pricing", "media", "*")),
            cap("app/orders", 0), *(cap(f"app/orders/{x}", 80, "cap:app/orders") for x in ("web", "api", "admin")),
            *(cap(f"app/{x}", n) for x, n in [("search", 5), ("reviews", 40), ("products", 100), ("inventory", 3), ("analytics", 0), ("blog", 1),
                                               ("tags", 0), ("workflow", 12), ("loyalty", 8), ("feeds", 2), ("email", 1), ("wishlist", 1)]),
            cap("core", 3), cap("bigapp", 9), cap("lib", 30), cap("static", 0), cap("extras", 0),
            *(cap(f"web/{x}", 0) for x in ("src", "types")),
            cap("app/orders/common", 1, "cap:app/orders", "shared"), cap("ops", 0, category="engineering")]
    doms = static_domains(caps)
    by = {c.id: c.domain_id for c in caps}
    biz = [c for c in caps if c.category == "business"]
    assert len(doms) <= 10 and all(by[c.id] in {d.id for d in doms} for c in biz)
    assert by["cap:app/orders/common"] is None and by["cap:ops"] is None  # shared / engineering: no domain
    sizes = {d.id: sum(1 for c in biz if c.domain_id == d.id) for d in doms}
    assert max(sizes.values()) * 2 <= len(biz)  # no single "app" domain holding most of the system
    # a split capability stays with its parent; the parent directory's own files and "app/*" stay as "app"
    assert {by[c] for c in ("cap:app/catalog", "cap:app/catalog/search", "cap:app/catalog/*")} == {"domain:app/catalog"}
    assert by["cap:app/orders/web"] == "domain:app/orders" and by["cap:app"] == by["cap:app/*"] == "domain:app"
    # single-capability directories share balanced runs; multi-capability directories stay their own domain
    merged = [d for d in doms if d.id.startswith("domain:*:")]
    assert merged and doms[-len(merged):] == merged and all("," in d.technical_name for d in merged)
    assert max(sizes[d.id] for d in merged) - min(sizes[d.id] for d in merged) <= 1
    assert by["cap:app/analytics"].startswith("domain:*:") and {"domain:app/catalog", "domain:app/orders", "domain:web"} <= set(sizes)
    # small systems keep one domain per top-level directory (shop: reports stays with shop)
    small = [cap("shop"), cap("shop/reports", 60, "cap:shop"), cap("billing")]
    assert [d.id for d in static_domains(small)] == ["domain:billing", "domain:shop"]


def test_domain_gate_rejects_bad_assignments_and_accepts_a_full_one(tmp_path):
    _, _, smap = make_map(tmp_path, big_repo(60))
    biz = [c.id for c in smap.capabilities if c.category == "business"]
    ok = {"domains": [{"name": "Selling", "capability_ids": biz[:2], "cites": ["ev:invented"]},
                      {"name": "Back office", "description": "Payments.", "capability_ids": biz[2:]}]}
    g = gate_domains(smap, ok)
    assert not g.problems and len(g.domains) == 2 and set(g.assignment) == set(biz)
    assert g.domains[0].business_name.text == "Selling" and g.domains[0].source == "semantic" and g.dropped == 1
    bad = [
        {"domains": [{"name": "All", "capability_ids": biz}]},  # 1 domain: below 2
        {"domains": [{"name": "A", "capability_ids": biz[:1]}, {"name": "B", "capability_ids": biz[1:2]}]},  # missing
        {"domains": [{"name": "A", "capability_ids": biz}, {"name": "B", "capability_ids": biz[:1]}]},  # twice
        {"domains": [{"name": "A", "capability_ids": biz + ["cap:ghost"]}, {"name": "B", "capability_ids": ["cap:scripts"]}]},
        {"domains": "nope"},
    ]
    for reply in bad:
        assert gate_domains(smap, reply).problems, reply


def test_domain_reuse_needs_the_same_business_capabilities(tmp_path):
    _, _, smap = make_map(tmp_path, big_repo(60))
    biz = [c.id for c in smap.capabilities if c.category == "business"]
    g = gate_domains(smap, {"domains": [{"name": "A", "capability_ids": biz[:1]}, {"name": "B", "capability_ids": biz[1:]}]})
    from pr_explorer.domain.services.domains import apply_domains
    explained = copy.deepcopy(smap)
    apply_domains(explained, g)
    donor = to_json(explained)
    fresh = copy.deepcopy(smap)
    assert reuse_domains(fresh, donor)[0] is True
    assert [d.id for d in fresh.domains] == [d.id for d in explained.domains]
    assert {c.id: c.domain_id for c in fresh.capabilities} == {c.id: c.domain_id for c in explained.capabilities}
    donor["capabilities"] = [c for c in donor["capabilities"] if c["id"] != biz[0]]  # set changed → re-ask
    assert reuse_domains(copy.deepcopy(smap), donor)[0] is False
    v20 = to_json(smap)  # a 2.0.0-era donor has no domains at all
    v20.pop("domains")
    assert reuse_domains(copy.deepcopy(smap), v20)[0] is False


# ---------------------------------------------------------------- phases

def test_phase_validation_contiguity_and_coverage(tmp_path):
    _, _, smap = make_map(tmp_path, SRC)
    flow = max(smap.flows, key=lambda f: len(f.timeline.steps))
    sids = [s.id for s in flow.timeline.steps]
    assert len(sids) >= 4
    r = Resolver({s.id: s.evidence_refs for s in flow.timeline.steps}, {e.id for e in smap.evidence})
    good = [{"label": "Check", "step_ids": sids[:1]}, {"label": "Save", "step_ids": sids[1:3]}, {"label": "Tell", "step_ids": sids[3:]}]
    phases = gate_phases(flow.id, sids, good, r)
    assert [p.step_ids for p in phases] == [sids[:1], sids[1:3], sids[3:]] and phases[1].label.evidence_refs
    bad = {
        "gap": [good[0], good[2]],
        "reordered": [good[1], good[0], good[2]],
        "overlap": [good[0], {"label": "Save", "step_ids": sids[:3]}, good[2]],
        "unknown": [good[0], good[1], {"label": "Tell", "step_ids": sids[3:] + ["step:ghost"]}],
        "too few": [{"label": "All", "step_ids": sids}],
        "no label": [good[0], {"step_ids": sids[1:3]}, good[2]],
    }
    for why, items in bad.items():
        assert gate_phases(flow.id, sids, items, r) is None, why
    assert r.dropped == len(bad)
    res = apply_phases(smap, [{"flows": [{"flow_id": flow.id, "phases": good}, {"flow_id": "flow:ghost", "phases": good}]}])
    assert flow.phases and res.dropped == 1


def test_phases_reused_only_for_flows_with_unchanged_steps(tmp_path):
    _, _, smap = make_map(tmp_path, SRC)
    long = [f for f in smap.flows if len(f.timeline.steps) >= 4]
    for f in long:
        sids = [s.id for s in f.timeline.steps]
        apply_phases(smap, [{"flows": [{"flow_id": f.id, "phases": [{"label": "A", "step_ids": sids[:1]},
                                                                  {"label": "B", "step_ids": sids[1:2]},
                                                                  {"label": "C", "step_ids": sids[2:]}]}]}])
    donor = to_json(smap)
    changed = long[0].id
    for f in donor["flows"]:
        if f["id"] == changed:
            f["timeline"]["steps"] = f["timeline"]["steps"][:-1]  # the donor's flow had other steps
    fresh = copy.deepcopy(smap)
    for f in fresh.flows:
        f.phases = None
    plan = reuse_texts(fresh, donor, {}, {})
    assert changed in plan.phases_ask and changed not in plan.phases_reused
    assert set(plan.phases_reused) == {f.id for f in long} - {changed}
    assert all(f.phases for f in fresh.flows if f.id in plan.phases_reused)


# ---------------------------------------------------------------- import-linter

INI = """[importlinter]
root_packages =
    bigapp
    legacy

[importlinter:contract:clean]
name = Clean architecture
type = layers
containers =
    bigapp.shipping
layers =
    api
    (application)
    domain | infra

[importlinter:contract:legacy]
name = Legacy is isolated
type = custom_forbidden
source_modules =
    bigapp
forbidden_modules =
    legacy
ignore_imports =
    bigapp.billing.old -> legacy.money  # to be removed
    bigapp.*.compat -> legacy.**

[importlinter:contract:apps]
type = independence
modules =
    bigapp.shipping
    bigapp.billing

[importlinter:contract:odd]
type = custom_something_else
"""


def test_import_linter_ini_with_custom_type_and_ignore_counts():
    cs = {c.key: c for c in parse_ini(INI, ".importlinter.ini")}
    assert set(cs) == {"clean", "legacy", "apps"}  # 'odd' has no module lists: skipped
    legacy = cs["legacy"]
    assert (legacy.kind, legacy.declared_type, legacy.line) == ("forbidden", "custom_forbidden", 16)
    assert legacy.ignore_imports == ["bigapp.billing.old -> legacy.money", "bigapp.*.compat -> legacy.**"]
    assert legacy.ignore_lines["bigapp.billing.old -> legacy.money"] == 24 and legacy.root_packages == ["bigapp", "legacy"]
    clean = cs["clean"]
    assert clean.layers == [["api"], ["application"], ["domain", "infra"]] and clean.layer_independent == [False, False, True]
    assert clean.containers == ["bigapp.shipping"] and cs["apps"].kind == "independence"

    from pr_explorer.domain.services.architecture import ignored_by, violation
    assert violation(legacy, "bigapp.shipping.x", "legacy.y") and not violation(legacy, "legacy.y", "bigapp.shipping.x")
    assert ignored_by(legacy.ignore_imports, "bigapp.orders.compat", "legacy.a.b", "") and not ignored_by(
        legacy.ignore_imports, "bigapp.orders.deep.compat", "legacy.a", "")
    assert violation(clean, "bigapp.shipping.domain.x", "bigapp.shipping.api") and not violation(clean, "bigapp.shipping.api", "bigapp.shipping.domain")
    assert violation(clean, "bigapp.shipping.domain", "bigapp.shipping.infra.db")  # independent siblings
    assert violation(cs["apps"], "bigapp.shipping.a", "bigapp.billing.b")


def test_import_linter_pyproject():
    text = ('[tool.importlinter]\nroot_package = "shop"\n\n[[tool.importlinter.contracts]]\nname = "A"\ntype = "forbidden"\n'
            'source_modules = ["shop.a"]\nforbidden_modules = ["shop.b"]\nignore_imports = ["shop.a.x -> shop.b.y"]\n')
    [c] = parse_pyproject(text)
    assert (c.kind, c.line, c.root_packages, len(c.ignore_imports)) == ("forbidden", 4, ["shop"], 1)


# ---------------------------------------------------------------- retry once on a malformed reply

def _req(tmp_path, kind="domains"):
    return SemanticRequest(kind, "standard", "en", {"business_capabilities": []}, tmp_path)


def test_malformed_reply_is_retried_once(tmp_path):
    replies = iter(["not json", claude_envelope({"domains": [{"name": "A"}]})])
    runner = RecordingRunner(lambda a, i: next(replies))
    d = ClaudeCliSemanticAnalyzer(runner).analyze(_req(tmp_path))
    assert d.status == "done" and d.calls == 2 and len(runner.calls) == 2 and "retried once" in d.error
    wrong = RecordingRunner(lambda a, i: claude_envelope({"domains": {"a": 1}}))  # wrong shape, twice
    d = ClaudeCliSemanticAnalyzer(wrong).analyze(_req(tmp_path))
    assert d.status == "failed" and len(wrong.calls) == 2 and "after 1 retry" in d.error
    crash = RecordingRunner(lambda a, i: RunResult(1, "", "rate limited"))  # a failed CLI run is not retried
    assert ClaudeCliSemanticAnalyzer(crash).analyze(_req(tmp_path)).status == "failed" and len(crash.calls) == 1


@pytest.mark.parametrize("second_ok", [True, False])
def test_domain_step_retries_with_the_problems_then_falls_back_to_static(tmp_path, second_ok):
    from fakes import prompt_data
    from pr_explorer.application.pipeline.stages import Pipeline

    _, _, smap = make_map(tmp_path, big_repo(60))
    biz = [c.id for c in smap.capabilities if c.category == "business"]
    prompts = []

    def handler(args, prompt):
        prompts.append(prompt_data(prompt))
        if len(prompts) == 1 or not second_ok:
            return claude_envelope({"domains": [{"name": "Only one", "capability_ids": biz[:1]}]})
        return claude_envelope({"domains": [{"name": "A", "capability_ids": biz[:2]}, {"name": "B", "capability_ids": biz[2:]}]})

    class D:
        semantic = ClaudeCliSemanticAnalyzer(RecordingRunner(handler))

    p = Pipeline.__new__(Pipeline)
    p.d = D()
    static = [d.id for d in smap.domains]
    msg, _, _, err = p._domains(smap, "en", Path(tmp_path), [])
    assert len(prompts) == 2 and "previous_reply_problems" not in prompts[0]
    assert any("not assigned" in x for x in prompts[1]["previous_reply_problems"]) and err is None
    if second_ok:
        assert "after one retry" in msg and {d.source for d in smap.domains} == {"semantic"}
    else:
        assert "static fallback" in msg and [d.id for d in smap.domains] == static
