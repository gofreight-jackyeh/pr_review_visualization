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
