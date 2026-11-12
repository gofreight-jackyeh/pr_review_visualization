"""Incremental reuse of Claude texts between system maps (pure)."""

from pr_explorer.domain.model.document import Capability, FlowBudget, Repo, SemanticStatus, SystemInfo, SystemMap, Text
from pr_explorer.domain.model.graph import CodeRef, Evidence, Producer
from pr_explorer.domain.services.map_reuse import capability_fingerprints, reuse_texts, skeleton

P = Producer("t", "1")


def new_map(caps: list[str]) -> SystemMap:
    ev = [Evidence(f"ev:{c}", "ast", c, [CodeRef("base", f"{c}/apps.py", 1)], P) for c in caps] + \
         [Evidence("ev:doc:base:README.md", "doc", "readme", [CodeRef("base", "README.md", 1)], P)]
    return SystemMap("2.1.0", "sm_new", "now", Repo("shop"), "c2", SystemInfo("shop"), [],
                     [Capability(f"cap:{c}", c, c, [f"ev:{c}"]) for c in caps], [], [], [], [], [], FlowBudget(0, 0), ev,
                     SemanticStatus("skipped"))


def donor(caps: list[str], refs: dict[str, str] | None = None) -> dict:
    t = lambda text, ref: {"text": text, "source": "semantic", "evidence_refs": [ref]}  # noqa: E731
    return {"system_map_id": "sm_old", "analyzer_version": "2.0.0",
            "system": {"technical_name": "shop", "summary": t("A shop.", "ev:doc:base:README.md")},
            "capabilities": [{"id": f"cap:{c}", "business_name": t(f"Biz {c}", (refs or {}).get(c, f"ev:{c}")),
                              "description": t(f"Does {c}.", f"ev:{c}")} for c in caps],
            "entities": [], "entry_points": [], "flows": []}


def fps(caps: list[str], changed: set[str] = frozenset()) -> tuple[dict, dict]:
    new = {f"cap:{c}": ("NEW" if c in changed else "same") + c for c in caps}
    return new, {f"cap:{c}": "same" + c for c in caps}


def test_unchanged_capabilities_reuse_texts_and_only_changed_or_new_are_asked():
    caps = ["a", "b", "c", "d", "e"]
    smap = new_map(caps)
    new_fp, old_fp = fps(caps, changed={"b"})
    plan = reuse_texts(smap, donor(["a", "b", "c", "d"]), new_fp, old_fp)  # "e" is new
    assert plan.ask == ["cap:b", "cap:e"] and plan.reused == ["cap:a", "cap:c", "cap:d"]
    by = {c.id: c for c in smap.capabilities}
    assert by["cap:a"].business_name.text == "Biz a" and by["cap:a"].description.text == "Does a."
    assert by["cap:b"].business_name is None and by["cap:e"].business_name is None
    assert plan.summary_reused is False  # 2/5 = 40% changed > 25%


def test_reused_text_citing_vanished_evidence_is_dropped_and_its_capability_reasked():
    caps = ["a", "b", "c", "d", "e"]
    smap = new_map(caps)
    plan = reuse_texts(smap, donor(caps, refs={"a": "ev:gone"}), *fps(caps))
    by = {c.id: c for c in smap.capabilities}
    assert by["cap:a"].business_name is None and plan.dropped == 1 and "ev:gone" in plan.reasons[0]
    assert plan.ask == ["cap:a"] and by["cap:a"].description.text == "Does a."  # the still-grounded text survives
    assert plan.summary_reused is True and smap.system.summary == Text("A shop.", "semantic", ["ev:doc:base:README.md"])


def test_summary_reused_only_up_to_a_quarter_of_capabilities_changed():
    caps = ["a", "b", "c", "d"]
    one = reuse_texts(new_map(caps), donor(caps), *fps(caps, changed={"a"}))  # 25%
    two = reuse_texts(new_map(caps), donor(caps), *fps(caps, changed={"a", "b"}))  # 50%
    assert one.summary_reused is True and two.summary_reused is False


def test_no_donor_asks_everything():
    plan = reuse_texts(new_map(["a", "b"]), None, {}, {})
    assert plan.ask == ["cap:a", "cap:b"] and plan.donor_id is None and not plan.summary_reused


def test_fingerprint_follows_blob_shas_of_the_capability_files_only():
    lang = lambda p: "python" if p.endswith(".py") else "text"  # noqa: E731
    doc = skeleton(new_map(["a", "b"]))
    blobs = {"a/apps.py": "1", "a/models.py": "2", "b/apps.py": "3", "README.md": "4"}
    base = capability_fingerprints(doc, blobs, lang)
    assert capability_fingerprints(doc, {**blobs, "a/models.py": "9"}, lang)["cap:b"] == base["cap:b"]
    assert capability_fingerprints(doc, {**blobs, "a/models.py": "9"}, lang)["cap:a"] != base["cap:a"]
    assert capability_fingerprints(doc, {**blobs, "README.md": "9"}, lang) == base  # root docs belong to no capability


# ---------------------------------------------------------------- 2.6.0: surface-based reuse, evidence remap, staleness

from pr_explorer.domain.model.changes import ChangedFile, Hunk  # noqa: E402
from pr_explorer.domain.model.document import Entity, EntryPoint, TextBasis  # noqa: E402
from pr_explorer.domain.services.map_reuse import (  # noqa: E402
    BaseChange, ReuseLimits, capability_surfaces, shift_range,
)

ENT = "ev:ast:base:a/models.py:{}:aaaa1111"  # line-based evidence of the entity a/models.py::Thing
EP = "ev:framework_rule:base:a/views.py:{}:bbbb2222"


def surface_map(ent_line: int = 10, ep_line: int = 5, extra_ep: bool = False) -> SystemMap:
    smap = new_map(["a", "b"])
    smap.evidence += [Evidence(ENT.format(ent_line), "ast", "class Thing", [CodeRef("base", "a/models.py", ent_line, ent_line + 4)], P),
                      Evidence(EP.format(ep_line), "framework_rule", "view", [CodeRef("base", "a/views.py", ep_line)], P)]
    smap.entities = [Entity("entity:a.Thing", "Thing", "cap:a", [], [ENT.format(ent_line)])]
    smap.entry_points = [EntryPoint("ep:a.view", "view", "cap:a", "route", [], [EP.format(ep_line)])]
    if extra_ep:
        smap.entry_points.append(EntryPoint("ep:a.new", "new_view", "cap:a", "route", [], [EP.format(ep_line)]))
    return smap


def surface_donor(surfaces: dict, written_at: str = "2026-11-10T00:00:00Z", base_changes: int = 0, changed: int = 0) -> dict:
    d = donor(["a", "b"])
    t = lambda text, ref: {"text": text, "source": "semantic", "evidence_refs": [ref]}  # noqa: E731
    d.update(commit_sha="c1", generated_at="2026-11-10T00:00:00Z",
             evidence=[{"id": ENT.format(10), "refs": [{"side": "base", "path": "a/models.py", "start_line": 10, "end_line": 14}]},
                       {"id": EP.format(5), "refs": [{"side": "base", "path": "a/views.py", "start_line": 5}]}],
             entities=[{"id": "entity:a.Thing", "capability_id": "cap:a", "business_name": t("A thing", ENT.format(10))}],
             entry_points=[{"id": "ep:a.view", "capability_id": "cap:a", "business_name": t("Look at things", EP.format(5))}])
    for c in d["capabilities"]:
        c["text_basis"] = {"surface": surfaces[c["id"]], "written_at": written_at, "written_commit": "c0",
                           "base_changes": base_changes, "changed_lines": changed}
    return d


def change(*files: ChangedFile) -> BaseChange:
    return BaseChange.from_files(list(files))


def modified(path: str, *hunks: tuple[int, int, int, int]) -> ChangedFile:
    return ChangedFile(f"file:{path}", path, "modified", "python", sum(h[3] for h in hunks), sum(h[1] for h in hunks),
                       [Hunk(*h) for h in hunks])


def test_shift_range_moves_untouched_ranges_and_rejects_touched_ones():
    hunks = [(2, 0, 3, 2), (20, 1, 22, 3)]  # 2 lines inserted after line 2; line 20 replaced by 3 lines
    assert shift_range(hunks, 1, 2) == 1  # before every hunk
    assert shift_range(hunks, 5, 9) == 7  # after the insertion
    assert shift_range(hunks, 25, 30) == 29  # after both: +2, then +2
    assert shift_range(hunks, 18, 21) is None  # contains the replaced line
    assert shift_range([(5, 0, 6, 1)], 3, 8) is None  # an insertion inside the range
    assert shift_range([(1, 3, 0, 0)], 10, 12) == 7  # 3 lines deleted above


def test_implementation_only_change_reuses_texts_with_remapped_evidence():
    old = surface_map()
    donor_doc = surface_donor(capability_surfaces(old, None))
    smap = surface_map(ent_line=13, ep_line=5)  # 3 lines inserted at the top of models.py; views.py untouched
    plan = reuse_texts(smap, donor_doc, surfaces=capability_surfaces(smap, None), change=change(modified("a/models.py", (1, 0, 2, 3))),
                       cap_lines={"cap:a": 100}, cap_changed={"cap:a": 3}, now="2026-11-12T00:00:00Z")
    assert plan.ask == [] and plan.partial == {} and plan.reused == ["cap:a", "cap:b"]
    assert smap.entities[0].business_name == Text("A thing", "semantic", [ENT.format(13)])  # remapped 10 → 13
    assert smap.entry_points[0].business_name.evidence_refs == [EP.format(5)]
    assert plan.evidence_moved == 1 and plan.evidence_failed == 0
    basis = smap.capabilities[0].text_basis
    assert basis == TextBasis(capability_surfaces(smap, None)["cap:a"], "2026-11-10T00:00:00Z", "c0", 1, 3)


def test_added_entry_point_changes_the_surface_and_reasks_the_capability():
    donor_doc = surface_donor(capability_surfaces(surface_map(), None))
    smap = surface_map(extra_ep=True)
    plan = reuse_texts(smap, donor_doc, surfaces=capability_surfaces(smap, None), change=BaseChange(same_commit=True))
    assert plan.ask == ["cap:a"] and plan.why == {"cap:a": "surface"} and plan.reused == ["cap:b"]
    assert smap.capabilities[0].business_name is None  # Claude rewrites the whole capability


def test_modified_cited_lines_reask_only_that_element():
    donor_doc = surface_donor(capability_surfaces(surface_map(), None))
    smap = surface_map(ent_line=11)  # one line inserted above the class AND one line inside it changed
    plan = reuse_texts(smap, donor_doc, surfaces=capability_surfaces(smap, None),
                       change=change(modified("a/models.py", (1, 0, 2, 1), (12, 1, 13, 1))))
    assert plan.ask == [] and plan.partial == {"cap:a": ["entity:a.Thing"]}
    assert smap.entities[0].business_name is None and smap.entry_points[0].business_name.text == "Look at things"
    assert smap.capabilities[0].business_name.text == "Biz a" and plan.evidence_failed == 1


def test_moved_or_deleted_file_reasks_the_element_citing_it():
    donor_doc = surface_donor(capability_surfaces(surface_map(), None))
    smap = surface_map(ent_line=11)
    moved = ChangedFile("file:a/models2.py", "a/models2.py", "renamed", "python", 0, 0, [], old_path="a/models.py")
    plan = reuse_texts(smap, donor_doc, surfaces=capability_surfaces(smap, None), change=change(moved))
    assert plan.partial == {"cap:a": ["entity:a.Thing"]}


def test_staleness_reasks_whole_capability_past_each_limit():
    surf = capability_surfaces(surface_map(), None)
    limits = ReuseLimits(max_changed_share=0.4, max_base_changes=3, max_age_days=14)

    def plan_for(**kw):
        opts = {"cap_lines": {"cap:a": 100}, "cap_changed": {"cap:a": 0}, "now": "2026-11-12T00:00:00Z"}
        dkw = {k: kw.pop(k) for k in ("written_at", "base_changes", "changed") if k in kw}
        opts.update(kw)
        return reuse_texts(surface_map(), surface_donor(surf, **dkw), surfaces=surf, change=change(), limits=limits, **opts)

    assert plan_for().ask == []
    assert plan_for(changed=30, cap_changed={"cap:a": 11}).why == {"cap:a": "stale: 41/100 lines changed"}  # cumulative
    assert plan_for(changed=30, cap_changed={"cap:a": 10}).ask == []  # exactly 40%: kept
    assert plan_for(base_changes=3).why == {"cap:a": "stale: 4 base changes", "cap:b": "stale: 4 base changes"}
    assert plan_for(written_at="2026-10-28T00:00:00Z").ask == ["cap:a", "cap:b"]  # 15 days old
