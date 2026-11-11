"""Evidence gate on both Claude calls: statements citing unknown IDs (or nothing) are dropped and counted."""

import pytest
from test_system_map import analyze

from pr_explorer.domain.model.changes import ChangedFile
from pr_explorer.domain.model.document import Repo, SemanticStatus
from pr_explorer.domain.model.graph import CodeRef, Evidence, Producer
from pr_explorer.domain.services.semantic_digest import capability_batches, detect_language as text_language, system_map_digest
from pr_explorer.domain.services.semantic_gate import PrKnown, apply_system_semantics, gate_pr
from pr_explorer.domain.services.system_map_builder import assemble_system_map, build_flows
from pr_explorer.domain.services.system_structure import build_structure
from pr_explorer.infrastructure.analyzers.generic.languages import detect_language


@pytest.fixture()
def smap(tmp_path):
    sg = analyze(tmp_path)
    st = build_structure(sg, detect_language)
    flows, built = build_flows(sg, st, 20)
    return assemble_system_map(sg, st, flows, built, repo=Repo("shop"), commit_sha="a" * 40, analyzer_version="t",
                               system_map_id="sm_t", generated_at="2026-01-01T00:00:00Z", coverage=[],
                               semantic=SemanticStatus("skipped"))


def test_system_map_gate(smap):
    cap = next(c for c in smap.capabilities if c.id == "cap:shop")
    flow = next(f for f in smap.flows if f.timeline.steps)
    step = flow.timeline.steps[0]
    ev = cap.evidence_refs[0]
    replies = [{
        "system_summary": {"text": "A shop.", "cites": ["cap:shop"]},
        "capabilities": [
            {"id": "cap:shop", "business_name": "Orders", "description": "Takes orders.", "cites": [ev]},
            {"id": "cap:ghost", "business_name": "Ghost"},  # unknown element → dropped
            {"id": "cap:billing", "business_name": "Billing", "cites": ["ev:invented"]},  # unknown cite → dropped
        ],
        "flows": [{"id": flow.id, "business_name": "Checkout"}],
        "steps": [{"id": step.id, "business_label": "Customer checks out"}, {"id": "step:nope", "business_label": "x"}],
        "nodes": [{"id": "evil"}],  # structure is never read
    }, "not an object"]
    r = apply_system_semantics(smap, replies)
    assert smap.system.summary.text == "A shop." and smap.system.summary.evidence_refs == cap.evidence_refs[:6]
    assert cap.business_name.text == "Orders" and cap.business_name.source == "semantic" and ev in cap.business_name.evidence_refs
    assert flow.business_name.text == "Checkout" and step.business_label.text == "Customer checks out"
    assert all(e in {x.id for x in smap.evidence} for e in step.business_label.evidence_refs)
    billing = next(c for c in smap.capabilities if c.id == "cap:billing")
    assert billing.business_name is None
    assert r.dropped == 4  # ghost capability, invented cite, unknown step, non-object reply
    assert not any(c.id == "evil" for c in smap.components)


def test_system_map_gate_requires_an_existing_citation(smap):
    r = apply_system_semantics(smap, [{"system_summary": {"text": "Uncited summary"}}])
    assert smap.system.summary is None and r.dropped == 1


def _known():
    ev = {
        "ev:diff:a.py": Evidence("ev:diff:a.py", "diff", "a.py modified", [CodeRef("head", "a.py", 3, 9)], Producer("git_diff", "1")),
        "ev:base:b.py": Evidence("ev:base:b.py", "ast", "b.py", [CodeRef("base", "b.py", 1)], Producer("python_ast", "2")),
        "ev:pr:metadata": Evidence("ev:pr:metadata", "pr_metadata", "PR", [], Producer("pr_metadata", "1")),
    }
    files = [ChangedFile("file:a.py", "a.py", "modified", "python", 3, 1)]
    elements = {"cap:a": ["ev:base:b.py"], "flow:a": ["ev:base:b.py"], "step:1": ["ev:diff:a.py"], "file:a.py": ["ev:diff:a.py"],
                "pr": ["ev:pr:metadata"]}
    return PrKnown(elements, ev, {"cap:a"}, {"flow:a"}, {"step:1"}, files)


def test_pr_gate():
    payload = {
        "purpose": {"text": "Let customers use codes.", "cites": ["pr", "file:a.py"]},
        "design_rationale": {"text": "Uncited."},  # → dropped
        "behavior_changes": [
            {"flow_ids": ["flow:a"], "capability_ids": ["cap:a"], "before": "No codes.", "after": "Codes apply.", "cites": ["step:1"]},
            {"flow_ids": ["flow:ghost"], "after": "Invented flow."},  # → dropped
        ],
        "findings": [
            {"title": "Can totals go negative?", "severity": "warning", "status": "verified", "capability_ids": ["cap:a"],
             "cites": ["ev:diff:a.py"]},
            {"title": "Unanchored", "cites": ["ev:base:b.py"]},  # evidence not in a changed file → dropped
            {"title": "Invented", "cites": ["ev:nope"]},  # → dropped
        ],
        "step_labels": [{"step_id": "step:1", "business_label": "Check the code"}, {"step_id": "step:9", "business_label": "x"}],
        "walkthrough": {"behavior": {"text": "Codes now reduce totals.", "cites": ["flow:a"]}, "impact": {"text": "x", "cites": ["cap:zz"]}},
        "questions": [{"question": "Who uses codes?", "answer": "Customers.", "cites": ["pr"]}],
        "graph": {"nodes": []},
    }
    out = gate_pr(payload, _known())
    assert out.purpose.text == "Let customers use codes." and set(out.purpose.evidence_refs) == {"ev:pr:metadata", "ev:diff:a.py"}
    assert out.design_rationale is None
    [bc] = out.behavior_changes
    assert bc.before.text == "No codes." and bc.after.source == "semantic" and bc.flow_ids == ["flow:a"]
    [f] = out.findings
    assert f.source == "semantic" and f.status == "needs_confirmation" and f.changed_file_ids == ["file:a.py"]
    assert f.refs == [CodeRef("head", "a.py", 3, 9)] and f.capability_ids == ["cap:a"]
    assert out.step_labels["step:1"].text == "Check the code" and "step:9" not in out.step_labels
    assert set(out.walkthrough) == {"behavior"} and out.questions[0]["answer"].text == "Customers."
    # design_rationale, ghost flow, unanchored + invented finding, unknown step, walkthrough.impact
    assert out.dropped == 6


def test_pr_gate_rejects_non_object():
    with pytest.raises(ValueError):
        gate_pr(["not", "an", "object"], _known())


def test_system_map_call_is_batched_by_capability(smap, tmp_path):
    batches = capability_batches(smap, size=2)
    assert [len(b) for b in batches] == [2, 1] and sum(batches, []) == [c.id for c in smap.capabilities]
    assert all(len(b) <= 15 for b in capability_batches(smap))
    sg = analyze(tmp_path / "again")
    first = system_map_digest(smap, sg, batches[0], [], first=True)
    second = system_map_digest(smap, sg, batches[1], [], first=False)
    assert first["write_system_summary"] and not second["write_system_summary"]
    assert {c["id"] for c in first["capabilities"]} == set(batches[0])
    assert {e["capability_id"] for e in second["entry_points"]} <= set(batches[1])
    assert {c["id"] for c in second["other_capabilities"]} == set(batches[0])


def test_output_language_follows_the_docs():
    assert text_language("一個小型的 Django 網路商店後端，顧客瀏覽商品目錄並結帳。") == "zh-Hant"
    assert text_language("A small Django shop.", "Add discount codes") == "en"
