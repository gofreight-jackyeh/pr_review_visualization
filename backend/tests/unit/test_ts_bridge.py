import json

import pytest
from fakes import RecordingRunner

from pr_explorer.application.ports import AnalyzerUnavailable
from pr_explorer.infrastructure.analyzers.typescript.ts_bridge import TypeScriptAnalyzer

OUT = {"version": "1", "diagnostics": [], "files": [
    {"path": "src/a.ts", "imports": [{"specifier": "./b", "resolved_path": "src/b.ts", "line": 1}],
     "symbols": [{"name": "App", "kind": "component", "start_line": 3, "end_line": 9, "parent": None, "exported": True}],
     "calls": [{"from_symbol": "App", "callee_text": "helper", "resolved": {"path": "src/b.ts", "symbol": "helper"}, "line": 4},
               {"from_symbol": "App", "callee_text": "handlers[key]", "resolved": None, "line": 5}],
     "component_usages": [{"from_symbol": "App", "component": "Button", "resolved": {"path": "src/b.ts", "symbol": "Button"}, "line": 6}]},
    {"path": "src/b.ts", "imports": [], "calls": [], "component_usages": [],
     "symbols": [{"name": "helper", "kind": "function", "start_line": 1, "end_line": 2, "parent": None, "exported": True},
                 {"name": "Button", "kind": "component", "start_line": 4, "end_line": 6, "parent": None, "exported": True}]},
]}


def test_maps_ts_analyzer_output(tmp_path):
    cli = tmp_path / "cli.js"
    cli.write_text("// fake")
    (tmp_path / "src").mkdir()
    for p in ("src/a.ts", "src/b.ts"):
        (tmp_path / p).write_text("x\n" * 10)
    runner = RecordingRunner(lambda a, i: json.dumps(OUT))
    g = TypeScriptAnalyzer(runner, cli).analyze(tmp_path, {"src/a.ts", "src/b.ts"}, "head")
    assert json.loads(runner.calls[0]["input"]) == {"root": str(tmp_path), "files": ["src/a.ts", "src/b.ts"]}
    kinds = {(e.kind, e.confidence) for e in g.edges.values()}
    assert {("imports", "confirmed"), ("calls", "confirmed"), ("renders", "confirmed"), ("calls", "unknown")} <= kinds
    assert g.nodes["node:ts:src/a.ts::App"].kind == "component"
    ext = [n for n in g.nodes.values() if n.kind == "external"]  # placeholder cites the dynamic call that created it
    assert ext and all(n.evidence_refs and set(n.evidence_refs) <= set(g.evidence) for n in ext)


def test_missing_cli_raises_unavailable(tmp_path):
    an = TypeScriptAnalyzer(RecordingRunner(lambda a, i: ""), tmp_path / "nope.js")
    assert not an.available() and not an.claims("a.ts")
    with pytest.raises(AnalyzerUnavailable):
        an.analyze(tmp_path, {"a.ts"}, "head")


def test_all_ts_files_go_to_one_program_and_a_heap_overflow_retries_with_fewer(tmp_path):
    """No silent 3,000-file cut: every TS/JS file is sent; only when Node runs out of heap is the old 3,000 retried."""
    from pr_explorer.infrastructure.analyzers.typescript.ts_bridge import LOW_MEMORY_FILES
    from pr_explorer.infrastructure.process.runner import RunResult

    cli = tmp_path / "cli.js"
    cli.write_text("// fake")
    files = {f"src/m{i:05}.ts" for i in range(LOW_MEMORY_FILES + 500)}
    ok = lambda a, i: json.dumps({"version": "1", "diagnostics": [], "files": []})
    runner = RecordingRunner(ok)
    g = TypeScriptAnalyzer(runner, cli).analyze(tmp_path, files, "base")
    assert [len(json.loads(c["input"])["files"]) for c in runner.calls] == [len(files)]
    assert not any("did not fit" in n for n in g.notes["typescript"])

    oom = RunResult(134, "", "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory")
    runner = RecordingRunner(lambda a, i: oom if len(json.loads(i)["files"]) > LOW_MEMORY_FILES else ok(a, i))
    g = TypeScriptAnalyzer(runner, cli).analyze(tmp_path, files, "base")
    sent = [json.loads(c["input"])["files"] for c in runner.calls]
    assert [len(s) for s in sent] == [len(files), LOW_MEMORY_FILES] and sent[1] == sorted(files)[:LOW_MEMORY_FILES]
    assert any(f"({len(files)} TS/JS files) did not fit" in n for n in g.notes["typescript"])

    runner = RecordingRunner(lambda a, i: RunResult(1, "", "ts-analyzer: boom"))  # other failures: no retry
    with pytest.raises(AnalyzerUnavailable):
        TypeScriptAnalyzer(runner, cli).analyze(tmp_path, files, "base")
    assert len(runner.calls) == 1
