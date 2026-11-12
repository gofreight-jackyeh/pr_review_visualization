"""Bridge to tools/ts-analyzer (TypeScript Compiler API, separate Node process).

Runs OUR analyzer CLI, which parses files with the TS compiler; analyzed code is never executed.
If the CLI is missing or fails, callers fall back to the generic analyzer (capability note recorded).
"""

from __future__ import annotations

import json
from pathlib import Path

from ....application.ports import AnalyzerUnavailable
from ....domain.model import ids
from ....domain.model.graph import CodeRef, Edge, Evidence, Node, Producer, SideGraph
from ...process.runner import CommandRunner
from ...security.repo_files import read_repo_text
from ..generic.languages import detect_language

PRODUCER = Producer("ts_analyzer", "1")
TS_LANGS = {"typescript", "javascript"}
LANG = "ts"
SKIP = {"node_modules", "dist", "build", ".next", "coverage"}
MAX_FILES = 40_000  # whole-program safety cap (same as the Python analyzer); a capped run says so in its notes
LOW_MEMORY_FILES = 3000  # retried with this many files when the whole program does not fit Node's heap
MAX_BYTES = 1_000_000  # keep in sync with MAX_BYTES in tools/ts-analyzer/src/analyze.ts


class TsAnalyzerUnavailable(AnalyzerUnavailable):
    pass


def ts_files(files: set[str]) -> list[str]:
    return sorted(f for f in files if detect_language(f) in TS_LANGS and not (set(f.split("/")) & SKIP))


class TypeScriptAnalyzer:
    name = "typescript"
    language = "typescript"

    def claims(self, path: str) -> bool:
        return self.available() and detect_language(path) in TS_LANGS

    def __init__(self, runner: CommandRunner, cli_path: Path, node_bin: str = "node", timeout: float = 300):
        self.runner = runner
        self.cli_path = cli_path
        self.node_bin = node_bin
        self.timeout = timeout

    def available(self) -> bool:
        return self.cli_path.is_file()

    def analyze(self, root: Path, files: set[str], side: str) -> SideGraph:
        paths = ts_files(files)
        g = SideGraph(side=side, files=files)
        if not paths:
            return g
        if not self.available():
            raise TsAnalyzerUnavailable(f"ts-analyzer CLI not found at {self.cli_path}")
        notes = [f"analyzed the first {MAX_FILES} of {len(paths)} TS/JS files (size cap)"] if len(paths) > MAX_FILES else []
        paths = paths[:MAX_FILES]
        res = self._run(root, paths)
        if res.returncode != 0 and len(paths) > LOW_MEMORY_FILES and "heap out of memory" in res.stderr:
            # Node's heap grows with the program (~3 GB for 8k files) and its default limit with the machine's memory:
            # a smaller program beats losing deep TS analysis altogether.
            notes.append(f"the whole program ({len(paths)} TS/JS files) did not fit Node's memory; analyzed the first "
                         f"{LOW_MEMORY_FILES}")
            paths = paths[:LOW_MEMORY_FILES]
            res = self._run(root, paths)
        if res.returncode != 0:
            raise TsAnalyzerUnavailable(f"ts-analyzer exited {res.returncode}: {res.stderr.strip()[:300]}")
        try:
            out = json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            raise TsAnalyzerUnavailable(f"ts-analyzer returned invalid JSON: {exc}") from exc
        map_output(out, g, root, side, set(paths))
        g.notes["typescript"][1:1] = notes
        return g

    def _run(self, root: Path, paths: list[str]):
        payload = json.dumps({"root": str(root), "files": paths})
        return self.runner.run([self.node_bin, str(self.cli_path)], cwd=str(root), input=payload, timeout=self.timeout)


def _lines(root: Path, path: str) -> list[str]:
    text = read_repo_text(root, path, MAX_BYTES)
    return text.splitlines() if text is not None else []


def map_output(out: dict, g: SideGraph, root: Path, side: str, requested: set[str]) -> None:
    def ev(path: str, line: int, desc: str, *disc: object) -> str:
        return g.add_evidence(Evidence(ids.evidence_id("ts_compiler", side, path, line, *disc), "ts_compiler", desc,
                                       [CodeRef(side, path, line)], PRODUCER))

    files = [f for f in out.get("files", []) if isinstance(f, dict) and f.get("path") in g.files]
    sym_ids: dict[tuple[str, str], str] = {}
    for f in files:
        path = f["path"]
        lines = _lines(root, path)
        n = max(len(lines), 1)
        mid = ids.module_id(LANG, path)
        g.add_node(Node(mid, "module", "module", path, None, [CodeRef(side, path, 1, n)], [ev(path, 1, f"Module {path}", "module")],
                        language=detect_language(path),
                        meta={"hash": ids.short_hash(" ".join(" ".join(lines).split()), length=16)}))
        symbols = [s for s in f.get("symbols", []) if isinstance(s, dict) and s.get("name")]
        for s in symbols:
            qual = f"{s['parent']}.{s['name']}" if s.get("parent") else s["name"]
            sym_ids[(path, qual)] = ids.symbol_id(LANG, path, qual)
            sym_ids.setdefault((path, s["name"]), ids.symbol_id(LANG, path, qual))
        for s in symbols:
            qual = f"{s['parent']}.{s['name']}" if s.get("parent") else s["name"]
            start = max(int(s.get("start_line") or 1), 1)
            end = max(int(s.get("end_line") or start), start)
            kind = {"function": "function", "class": "class", "method": "method", "component": "component"}.get(s.get("kind"), "function")
            parent = sym_ids.get((path, s["parent"])) if s.get("parent") else mid
            body = " ".join(" ".join(lines[start - 1:end]).split())
            g.add_node(Node(sym_ids[(path, qual)], kind, "symbol", qual, parent or mid, [CodeRef(side, path, start, end, qual)],
                            [ev(path, start, f"{kind} {qual} in {path}", "sym", qual)], language=detect_language(path),
                            tags=["exported"] if s.get("exported") else [], meta={"hash": ids.short_hash(body, length=16)}))

    def resolve(r: dict | None) -> str | None:
        if not isinstance(r, dict) or not r.get("path"):
            return None
        if r.get("symbol"):
            return sym_ids.get((r["path"], r["symbol"]))
        mid = ids.module_id(LANG, r["path"])
        return mid if mid in g.nodes else None

    for f in files:
        path = f["path"]
        mid = ids.module_id(LANG, path)
        for imp in f.get("imports", []):
            rp = imp.get("resolved_path")
            tid = ids.module_id(LANG, rp) if rp else None
            if tid and tid in g.nodes and tid != mid:
                e = ev(path, int(imp.get("line") or 1), f"{path} imports '{imp.get('specifier')}'", "import", rp)
                g.add_edge(Edge(ids.edge_id("imports", mid, tid), mid, tid, "imports", "confirmed", [e]))
        for kind_key, edge_kind in (("calls", "calls"), ("component_usages", "renders")):
            for c in f.get(kind_key, []):
                src = sym_ids.get((path, c.get("from_symbol"))) if c.get("from_symbol") else mid
                if src is None:
                    continue
                line = int(c.get("line") or 1)
                tid = resolve(c.get("resolved"))
                label = c.get("callee_text") or c.get("component") or "?"
                if tid is None:
                    if kind_key == "calls" and label.rstrip().endswith("]"):  # obj[key](): element-access dispatch
                        xid = ids.external_id(f"dynamic:{path}:{line}:{label}")
                        e = ev(path, line, f"{path}:{line} dynamic call {label[:80]} (not statically resolvable)", "dyn", label)
                        # evidence = the call that created this placeholder, so the UI can explain why it exists
                        g.add_node(Node(xid, "external", "symbol", f"unresolved: {label[:60]}", None, [], [e], tags=["unknown"]))
                        g.add_edge(Edge(ids.edge_id("calls", src, xid), src, xid, "calls", "unknown", [e]))
                    continue
                if tid == src:
                    continue
                e = ev(path, line, f"{path}:{line} {'renders' if edge_kind == 'renders' else 'calls'} {label[:80]}", kind_key, tid)
                g.add_edge(Edge(ids.edge_id(edge_kind, src, tid), src, tid, edge_kind, "confirmed", [e]))
    diags = [d for d in out.get("diagnostics", []) if isinstance(d, str)][:5]
    g.notes["typescript"] = [f"{len(files)} TS/JS files analyzed with the TypeScript compiler API"] + diags + [
        "no branch/statement ordering from ts-analyzer: TS business flows render as static flows, not animations"
    ]
