"""Prompt-size caps and call measurement (on a large repo a single capability batch could grow huge and one call run for minutes)."""

import json
from types import SimpleNamespace

from fakes import RecordingRunner, claude_envelope
from test_layering import big_repo, make_map

from pr_explorer.application.pipeline.stages import call_stats
from pr_explorer.application.ports import SemanticRequest
from pr_explorer.domain.model.semantic import SemanticDraft
from pr_explorer.domain.services.semantic_digest import (
    ENTRY_POINTS_PER_CAP, capability_batches, capability_costs, phase_batches, relevant_rules, system_map_digest,
)
from pr_explorer.infrastructure.semantic.claude_cli import ClaudeCliSemanticAnalyzer
from pr_explorer.infrastructure.semantic.dry_run import DryRunSemanticAnalyzer


def test_a_capability_with_many_entry_points_is_capped_and_batches_follow_a_char_budget(tmp_path):
    sg, _, smap = make_map(tmp_path, big_repo(60))
    d = system_map_digest(smap, sg, ["cap:shop/reports"], [], first=False)
    reports = d["capabilities"][0]
    assert len(d["entry_points"]) == ENTRY_POINTS_PER_CAP and reports["entry_points_total"] == 60
    assert len(reports["entry_point_ids"]) == ENTRY_POINTS_PER_CAP and len(d["flows"]) <= ENTRY_POINTS_PER_CAP
    built = {f.entry_point_id for f in smap.flows if len(f.timeline.steps) > 1}
    assert built & {e.id for e in smap.entry_points if e.capability_id == "cap:shop/reports"} <= {e["id"] for e in d["entry_points"]}

    caps = [c.id for c in smap.capabilities]
    cost = capability_costs(smap, sg, caps)
    budget = max(cost.values())
    batches = capability_batches(smap, 15, sg=sg, char_budget=budget)
    assert [c for b in batches for c in b] == caps and len(batches) > 1  # nothing lost, order kept
    for b in batches:
        assert len(b) == 1 or sum(cost[c] for c in b) <= budget
    assert capability_batches(smap, 15) == [caps]  # count only (no budget): one batch as before


def test_phase_calls_are_batched_by_size(tmp_path):
    _, _, smap = make_map(tmp_path, big_repo(5))
    flows = [f.id for f in smap.flows]
    assert phase_batches(smap, flows, size=10, char_budget=10**9) == [flows[i: i + 10] for i in range(0, len(flows), 10)]
    tiny = phase_batches(smap, flows, size=10, char_budget=1)
    assert tiny == [[f] for f in flows]  # one flow per call when each alone is over the budget


def test_pr_call_gets_only_the_rules_this_pr_touches():
    rules = [{"id": f"r{i}", "scope_capability_ids": [f"cap:{i}"]} for i in range(100)]
    checks = [SimpleNamespace(rule_id="r50", capability_ids=["cap:50"])]
    got = relevant_rules(rules, {"cap:3", "cap:7"}, checks)
    assert [r["id"] for r in got] == ["r50", "r3", "r7"]


def test_every_call_records_prompt_size_and_wall_time(tmp_path):
    log = tmp_path / "logs" / "semantic-calls.jsonl"
    runner = RecordingRunner(lambda a, i: claude_envelope({"steps": []}))
    req = SemanticRequest("system_map", "light", "en", {"x": "y" * 1000}, tmp_path)
    draft = ClaudeCliSemanticAnalyzer(runner, log_path=log).analyze(req)
    sent = len(runner.calls[0]["input"])
    assert draft.status == "done" and draft.prompt_chars > sent > 1000 and draft.seconds >= 0
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["kind"] == "system_map" and rec["prompt_chars"] == draft.prompt_chars and "y" * 50 not in log.read_text()
    msg = call_stats("PR", [SemanticDraft("done", prompt_chars=41_200, seconds=312.4)])
    assert msg == "PR call 41k chars, 312 s"
    many = call_stats("capabilities", [SemanticDraft("done", prompt_chars=12_000, seconds=40),
                                       SemanticDraft("failed", error="claude exited 124: timed out", prompt_chars=31_000, seconds=600)])
    assert many == "capabilities 2 calls 12k–31k chars, ≤ 600 s (1 failed: claude exited 124: timed out)"


def test_dry_run_writes_prompts_and_calls_nothing(tmp_path):
    dr = DryRunSemanticAnalyzer(tmp_path / "prompts")
    draft = dr.analyze(SemanticRequest("pr", "deep", "en", {"pr": {"title": "t"}}, tmp_path))
    files = list((tmp_path / "prompts").iterdir())
    assert draft.status == "done" and draft.payload == {} and [f.name for f in files] == ["001-pr.txt"]
    assert draft.prompt_chars == len(files[0].read_text()) - len("\n\n----- user prompt -----\n")
