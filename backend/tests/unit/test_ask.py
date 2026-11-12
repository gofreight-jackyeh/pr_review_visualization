"""Follow-up questions: digest (what Claude sees), cite gate, reply shape check, persistence."""

import json

from pr_explorer.domain.services.ask import DIFF_BUDGET, HISTORY_TURNS, ask_digest, cite_index, gate_answer
from pr_explorer.infrastructure.persistence.sqlite.analysis_repository import SqliteAnalysisRepository
from pr_explorer.infrastructure.semantic.claude_cli import malformed


def _answered(i: int) -> dict:
    return {"question": f"q{i}", "answer": {"text": f"a{i}", "cites": []}, "status": "done"}


def test_digest_carries_overlay_selection_history_and_bounded_diff(shop_docs):
    smap, overlay, _, _ = shop_docs
    files = overlay["changes"]["files"]
    flow = overlay["flow_changes"][0]
    history = [_answered(i) for i in range(10)] + [{"question": "unanswered", "answer": None}]
    d = ask_digest(overlay, smap, question="Why is the total recalculated?", diff="diff --git a/x b/x\n" + "+x\n" * 40_000,
                   selected_id=flow["flow_id"], history=history)
    assert d["question"]["text"] == "Why is the total recalculated?"
    assert {f["id"] for f in d["changed_files"]} == {f["id"] for f in files}
    ev = {f["path"]: f["evidence_id"] for f in d["changed_files"]}
    assert ev["orders/services.py"] == "ev:diff:orders/services.py"  # the diff evidence Claude can cite per file
    assert [x["flow_id"] for x in d["flows_touched"]] == [fc["flow_id"] for fc in overlay["flow_changes"]]
    assert {x["id"] for x in d["findings"]} == {f["id"] for f in overlay["findings"]}
    assert d["selected"]["id"] == flow["flow_id"] and d["selected"]["kind"] == "flow"
    assert [h["question"] for h in d["history"]] == [f"q{i}" for i in range(10 - HISTORY_TURNS, 10)]
    assert len(d["diff_excerpt"]) == DIFF_BUDGET
    # an unknown selection is simply left out
    assert "selected" not in ask_digest(overlay, smap, question="q", diff="", selected_id="cap:nope")


def test_digest_fits_its_budget(shop_docs):
    smap, overlay, _, _ = shop_docs
    big = json.loads(json.dumps(overlay))
    big["findings"] = [{**big["findings"][0], "id": f"fd:{i}"} for i in range(500)] if big["findings"] else \
        [{"id": f"fd:{i}", "title": "t" * 200, "description": "d" * 300, "evidence_refs": []} for i in range(500)]
    d = ask_digest(big, smap, question="q", diff="", budget=20_000)
    del d["diff_excerpt"]
    assert len(json.dumps(d, ensure_ascii=False, separators=(",", ":"))) <= 20_000


def test_gate_keeps_known_cites_resolves_refs_and_drops_unknown(shop_docs):
    smap, overlay, _, _ = shop_docs
    idx = cite_index(overlay, smap)
    f = next(x for x in overlay["changes"]["files"] if x["status"] != "removed")
    ev = next(e for e in overlay["evidence"] if e["kind"] == "diff" and e["refs"])
    out = gate_answer({"answer": "  **Because** of the discount.  ",
                       "cites": [f["id"], f"{f['id']}:12", ev["id"], "pr", "ev:invented", "file:not/changed.py:3", 7, f["id"]]}, idx)
    assert out["text"] == "**Because** of the discount." and out["unsupported"] is False
    assert [c["id"] for c in out["cites"]] == [f["id"], f"{f['id']}:12", ev["id"], "pr"]
    assert out["cites"][0]["refs"] == [{"side": "head", "path": f["path"]}]
    assert out["cites"][1]["refs"] == [{"side": "head", "path": f["path"], "start_line": 12}]
    assert out["cites"][2]["refs"][0]["path"] == ev["refs"][0]["path"] and out["cites"][2]["kind"] == "evidence"
    assert out["dropped"] == ["ev:invented", "file:not/changed.py:3"]


def test_gate_marks_an_answer_without_valid_cites_unsupported(shop_docs):
    smap, overlay, _, _ = shop_docs
    idx = cite_index(overlay, smap)
    out = gate_answer({"answer": "No idea where this comes from.", "cites": ["ev:ghost", "flow:ghost"]}, idx)
    assert out["text"] == "No idea where this comes from." and out["cites"] == [] and out["unsupported"] is True
    assert gate_answer({"answer": "x"}, idx)["unsupported"] is True
    assert gate_answer({"answer": ["not", "text"], "cites": "pr"}, idx) == {"text": "", "cites": [], "unsupported": True, "dropped": []}


def test_ask_reply_shape_is_checked_so_a_bad_reply_is_retried():
    assert malformed({"answer": "ok", "cites": ["pr"]}, "ask") == ""
    assert "missing" in malformed({"cites": ["pr"]}, "ask")
    assert "missing" in malformed({"answer": ""}, "ask")
    assert "'answer' is a list" in malformed({"answer": ["x"]}, "ask")
    assert "'cites' is a str" in malformed({"answer": "x", "cites": "pr"}, "ask")


def test_question_persistence_round_trip(tmp_path):
    db = tmp_path / "pr_explorer_v2.db"
    r = SqliteAnalysisRepository(db)
    q = r.create_question("q_1", "ov_a", "Why?", "flow:checkout")
    assert (q["status"], q["answer"], q["selected_id"]) == ("queued", None, "flow:checkout")
    r.create_question("q_2", "ov_a", "And?", None)
    r.create_question("q_3", "ov_b", "Other PR", None)
    answer = {"text": "Because 折扣.", "cites": [{"id": "pr", "kind": "pr", "label": "PR", "refs": []}], "unsupported": False, "dropped": []}
    r.update_question("q_1", "running")
    r.update_question("q_1", "done", answer=answer)
    again = SqliteAnalysisRepository(db)  # a fresh connection (server restart) reads the same rows
    assert [x["id"] for x in again.list_questions("ov_a")] == ["q_1", "q_2"]
    assert again.get_question("q_1")["answer"] == answer and again.get_question("q_1")["status"] == "done"
    assert again.fail_unfinished_questions("restarted") == 2
    assert (again.get_question("q_2")["status"], again.get_question("q_2")["error"]) == ("failed", "restarted")
    assert again.get_question("q_1")["status"] == "done"
