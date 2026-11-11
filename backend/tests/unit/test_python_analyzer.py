from pathlib import Path

from pr_explorer.infrastructure.analyzers.python.graph_builder import PythonAnalyzer

SRC = {
    "app/__init__.py": "",
    "app/handlers.py": "def on_a(x):\n    return x\n\n\ndef on_b(x):\n    return x\n",
    "app/core.py": (
        "from . import handlers\n"
        "from .handlers import on_a\n\n\n"
        "def run(kind, x):\n"
        "    on_a(x)\n"
        "    fn = getattr(handlers, f'on_{kind}')\n"
        "    if x:\n"
        "        fn(x)\n"
        "    handlers.missing(x)\n\n\n"
        "class Svc:\n"
        "    def go(self):\n"
        "        return self.helper()\n\n"
        "    def helper(self):\n"
        "        try:\n"
        "            run('a', 1)\n"
        "        except ValueError:\n"
        "            raise\n"
    ),
}


def analyze(tmp_path: Path):
    for p, s in SRC.items():
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text(s)
    return PythonAnalyzer().analyze(tmp_path, set(SRC), "head")


def test_resolution_confirmed_and_unknown(tmp_path):
    g = analyze(tmp_path)
    run = "node:py:app/core.py::run"
    e = {(x.source, x.target): x for x in g.edges.values()}
    assert e[(run, "node:py:app/handlers.py::on_a")].confidence == "confirmed"
    # getattr dispatch → candidates matched by the f-string pattern, confidence unknown
    assert e[(run, "node:py:app/handlers.py::on_b")].confidence == "unknown"
    assert e[("node:py:app/core.py::Svc.go", "node:py:app/core.py::Svc.helper")].confidence == "confirmed"
    # attribute missing on a project module → unknown edge + unresolved reference
    assert any(u.target == "app.handlers.missing" for u in g.unresolved)
    steps = g.flows[run]
    dyn = next(s for s in steps if s.confidence == "unknown" and "on_b" in s.label)
    assert dyn.conditions == ("x",)
    helper = g.flows["node:py:app/core.py::Svc.helper"]
    assert next(s for s in helper if s.kind == "raise").conditions[0].startswith("except ValueError")


def test_analyzer_never_executes_code(tmp_path):
    (tmp_path / "boom.py").write_text("raise SystemExit('executed!')\nimport os\nos.system('touch PWNED')\n")
    g = PythonAnalyzer().analyze(tmp_path, {"boom.py"}, "head")
    assert "node:py:boom.py" in g.nodes and not (tmp_path / "PWNED").exists()


def test_syntax_error_is_a_capability_note(tmp_path):
    (tmp_path / "bad.py").write_text("def (:\n")
    g = PythonAnalyzer().analyze(tmp_path, {"bad.py"}, "head")
    assert any("could not parse bad.py" in n for n in g.notes["python"])
