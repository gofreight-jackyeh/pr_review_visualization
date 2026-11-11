"""Fallback analysis for any language: module nodes + regex import extraction (capability: generic)."""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from ....domain.model import ids
from ....domain.model.graph import CodeRef, Edge, Evidence, Node, Producer, SideGraph
from ...security.repo_files import read_repo_text
from .languages import detect_language

PRODUCER = Producer("generic_analyzer", "1")
IMPORT_PATTERNS = [
    re.compile(r"""^\s*import\s+(?:[\w*{}\s,]+\s+from\s+)?['"]([^'"]+)['"]"""),  # JS/TS import
    re.compile(r"""^\s*export\s+[\w*{}\s,]+\s+from\s+['"]([^'"]+)['"]"""),
    re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""),  # CommonJS / Ruby-ish
    re.compile(r"""^\s*(?:require|require_relative|load)\s+['"]([^'"]+)['"]"""),  # Ruby
    re.compile(r"""^\s*#include\s+"([^"]+)\""""),  # C/C++
    re.compile(r"""^\s*import\s+"([^"]+)\""""),  # Go single
    re.compile(r"""^\s*@import\s+['"]([^'"]+)['"]"""),  # CSS
]
RESOLVE_SUFFIXES = ["", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".rb", ".h", ".css", ".scss", "/index.ts", "/index.js"]
MAX_BYTES = 512_000


def extract_imports(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for pat in IMPORT_PATTERNS:
            for m in pat.finditer(line):
                out.append((m.group(1), i))
    return out


def resolve_specifier(spec: str, from_path: str, files: set[str]) -> str | None:
    if not spec.startswith("."):
        candidates = [spec]
    else:
        candidates = [posixpath.normpath(posixpath.join(posixpath.dirname(from_path), spec))]
    for base in candidates:
        for suf in RESOLVE_SUFFIXES:
            if base + suf in files:
                return base + suf
    return None


class GenericAnalyzer:
    name = "generic"

    def analyze(self, root: Path, paths: list[str], side: str, files: set[str]) -> SideGraph:
        g = SideGraph(side=side, files=files)
        for path in sorted(paths):
            text = read_repo_text(root, path, MAX_BYTES) if path in files else None
            if text is None:
                continue
            lang = detect_language(path)
            n_lines = max(text.count("\n") + (0 if text.endswith("\n") else 1), 1)
            mid = ids.module_id("file", path)
            ev = g.add_evidence(Evidence(ids.evidence_id("import", side, path, 1, "module"), "import",
                                         f"File {path} ({lang}, generic analysis)", [CodeRef(side, path, 1, n_lines)], PRODUCER))
            g.add_node(Node(mid, "module", "module", path.rsplit("/", 1)[-1], None, [CodeRef(side, path, 1, n_lines)],
                            [ev], language=lang, tags=["generic"], meta={"hash": ids.short_hash(" ".join(text.split()), length=16)}))
            for spec, line in extract_imports(text):
                target = resolve_specifier(spec, path, files)
                if target is None or target == path:
                    continue
                tid = ids.module_id("file", target)
                if tid not in g.nodes and (t_text := read_repo_text(root, target, MAX_BYTES)) is not None:
                    t_lines = max(t_text.count("\n"), 1)
                    tev = g.add_evidence(Evidence(ids.evidence_id("import", side, target, 1, "module"), "import",
                                                  f"File {target} (generic analysis)", [CodeRef(side, target, 1, t_lines)], PRODUCER))
                    g.add_node(Node(tid, "module", "module", target.rsplit("/", 1)[-1], None, [CodeRef(side, target, 1, t_lines)],
                                    [tev], language=detect_language(target), tags=["generic"],
                                    meta={"hash": ids.short_hash(" ".join(t_text.split()), length=16)}))
                eev = g.add_evidence(Evidence(ids.evidence_id("import", side, path, line, spec), "import",
                                              f"{path}:{line} imports '{spec}' (regex)", [CodeRef(side, path, line)], PRODUCER))
                g.add_edge(Edge(ids.edge_id("imports", mid, tid), mid, tid, "imports", "confirmed", [eev]))
        return g
