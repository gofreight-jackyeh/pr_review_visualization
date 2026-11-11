"""Regressions for bugs found by an independent review of the v2 backend."""

import json
import threading

from pr_explorer.domain.services.semantic_gate import gate_pr
from pr_explorer.infrastructure.analyzers.generic.diff_parser import parse_unified_diff
from pr_explorer.infrastructure.knowledge.local_docs import LocalDocsKnowledge
from pr_explorer.infrastructure.semantic.claude_cli import parse_reply
from test_semantic_gate import _known

import pytest


def test_error_envelope_without_result_is_a_failure():
    with pytest.raises(ValueError):
        parse_reply(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}))


def test_finding_needs_evidence_in_a_changed_file_not_just_a_claimed_file_id():
    out = gate_pr({"findings": [{"title": "x", "cites": ["ev:base:b.py"], "changed_file_ids": ["file:a.py"]}]}, _known())
    assert out.findings == [] and out.dropped == 1


def test_question_citing_unknown_ids_is_dropped():
    out = gate_pr({"questions": [{"question": "q", "answer": "a", "cites": ["pr"], "capability_ids": ["cap:ghost"]}]}, _known())
    assert out.questions == [] and out.dropped == 1


def test_unquoted_path_with_spaces_and_no_hunks():
    diff = "diff --git a/img one.png b/img one.png\nnew file mode 100644\nBinary files /dev/null and b/img one.png differ\n"
    [f] = parse_unified_diff(diff)
    assert f.path == "img one.png" and f.status == "added"


def test_symlinked_docs_are_not_followed(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("root:x:0:0 private host data")
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "hosts.md").symlink_to(outside)
    (repo / "README.md").write_text("# Shop\n")
    k = LocalDocsKnowledge().collect(repo, {"docs/hosts.md", "README.md"}, [])
    assert [e.path for e in k.excerpts] == ["README.md"] and "private host data" not in json.dumps([e.text for e in k.excerpts])


def test_concurrent_writes_of_one_document_do_not_fail(tmp_path):
    from pr_explorer.infrastructure.persistence import document_fs

    store = document_fs.FsDocumentStore(tmp_path)
    errors = []

    def write():
        try:
            store._write("system_map", "sm_x", {"schema_version": "2.0.0", "kind": "system_map"})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [] and store.load_system_map("sm_x")["kind"] == "system_map"
