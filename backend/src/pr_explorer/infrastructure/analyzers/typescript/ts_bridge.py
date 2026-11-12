"""Bridge to tools/ts-analyzer (TypeScript Compiler API, separate Node process).

Runs OUR analyzer CLI, which parses files with the TS compiler; analyzed code is never executed.
If the CLI is missing or fails, callers fall back to the generic analyzer (capability note recorded).

With a result cache (bootstrap passes one unless PR_EXPLORER_INCREMENTAL=off), each file's result is cached by its
content and analyzer environment, and a run re-analyzes only the files whose cached result may no longer hold
(ts_incremental): changed files and everything that depends on them. Those go to a program rooted at them plus every
file adding to the global scope; TypeScript loads the rest of their import closure itself. The SideGraph is built from
the same per-file results either way, so it is the same as a full run's.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Protocol

from ....application.ports import ANALYZER_VERSION, AnalyzerUnavailable
from ....domain.model import ids
from ....domain.model.graph import CodeRef, Edge, Evidence, Node, Producer, SideGraph
from ...process.runner import CommandRunner
from ...security.repo_files import read_repo_bytes, read_repo_text
from ..generic.languages import detect_language
from .ts_incremental import TS_EXT, Entry, GlobalIndex, cone_hashes, dependents, reusable

PRODUCER = Producer("ts_analyzer", "1")
TS_LANGS = {"typescript", "javascript"}
LANG = "ts"
SKIP = {"node_modules", "dist", "build", ".next", "coverage"}
MAX_FILES = 40_000  # whole-program safety cap (same as the Python analyzer); a capped run says so in its notes
LOW_MEMORY_FILES = 3000  # retried with this many files when the whole program does not fit Node's heap
MAX_BYTES = 1_000_000  # keep in sync with MAX_BYTES in tools/ts-analyzer/src/analyze.ts
CACHE_FORMAT = "1"  # bump when Entry's stored shape or meaning changes
# package.json fields TypeScript's module resolution reads (a dependency bump does not change how imports resolve)
PKG_FIELDS = ("name", "type", "main", "module", "types", "typings", "typesVersions", "exports", "imports")
log = logging.getLogger(__name__)


class ResultCache(Protocol):  # infrastructure/persistence/file_cache.FileResultCache
    def get_many(self, keys: list[str]) -> dict[str, bytes]: ...
    def put_many(self, items: dict[str, bytes]) -> None: ...
    def discard(self, keys: list[str]) -> None: ...


class TsAnalyzerUnavailable(AnalyzerUnavailable):
    pass


def ts_files(files: set[str]) -> list[str]:
    return sorted(f for f in files if detect_language(f) in TS_LANGS and not (set(f.split("/")) & SKIP))


class TypeScriptAnalyzer:
    name = "typescript"
    language = "typescript"

    def claims(self, path: str) -> bool:
        return self.available() and detect_language(path) in TS_LANGS

    def __init__(self, runner: CommandRunner, cli_path: Path, node_bin: str = "node", timeout: float = 300,
                 cache: ResultCache | None = None):
        self.runner = runner
        self.cli_path = cli_path
        self.node_bin = node_bin
        self.timeout = timeout
        self.cache = cache  # None: every run analyzes every file (PR_EXPLORER_INCREMENTAL=off)
        self.last_stats: dict = {}
        self.last_analyzed: list[str] = []  # files the last incremental run (re-)analyzed

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
        if self.cache is not None:
            try:
                out, texts = self._incremental(root, files, paths, side)
            except Exception as exc:  # the cache is an optimisation: anything wrong with it means a full run
                log.warning("incremental TS analysis failed (%s: %s); analyzing every file", type(exc).__name__, exc)
            else:
                map_output(out, g, root, side, set(paths), texts)
                g.notes["typescript"][1:1] = notes
                return g
        t0 = time.perf_counter()
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
        self.last_stats = {"side": side, "files": len(paths), "analyzed": len(paths), "reused": 0, "mode": "full",
                           "s": round(time.perf_counter() - t0, 3)}
        return g

    def _run(self, root: Path, paths: list[str], node_args: tuple[str, ...] = (), **extra):
        payload = json.dumps({"root": str(root), "files": paths, **extra})
        return self.runner.run([self.node_bin, *node_args, str(self.cli_path)], cwd=str(root), input=payload,
                               timeout=self.timeout)

    # ------------------------------------------------------------------ incremental
    def _incremental(self, root: Path, files: set[str], paths: list[str], side: str) -> tuple[dict, dict[str, bytes]]:
        """(analyzer output for `paths` in the full run's shape, from cached per-file results plus a run over the
        files whose results may have changed; the files' bytes, read once here, for map_output)."""
        assert self.cache is not None
        t0 = time.perf_counter()
        env = self._env(root)
        universe = set(paths)
        texts: dict[str, bytes] = {}
        blobs = {p: _content_id(root, p, texts) for p in paths}
        keys = {p: _key(env, p, b) for p, b in blobs.items() if b is not None}
        gkey = _key(env, "", "global-diagnostics")
        raw = self.cache.get_many([*keys.values(), gkey])
        entries: dict[str, Entry] = {}
        damaged = []
        for p, k in keys.items():
            if k in raw:
                try:
                    entries[p] = Entry.from_json(raw[k])
                except (ValueError, TypeError, KeyError):
                    damaged.append(k)
        try:
            global_diags = [d for d in json.loads(raw[gkey]) if isinstance(d, str)] if gkey in raw else None
        except ValueError:
            global_diags = None
        self.cache.discard(damaged)

        other: dict[str, str | None] = {}

        def content(p: str) -> str | None:
            if p in universe:
                return blobs[p]
            if p not in other:
                other[p] = _content_id(root, p) if p in files else None
            return other[p]

        exists = files.__contains__
        valid = reusable(entries, exists, content)
        todo = sorted(universe - valid)
        if global_diags is None and not todo:  # (evicted on its own) one file's run reports the tsconfig diagnostics
            todo = paths[:1]
            valid.discard(paths[0])
        fresh: dict[str, Entry] = {}
        passes = 0
        while todo:
            passes += 1
            if len(todo) == len(paths):
                out = self._run_meta(root, paths)
            else:  # rooted at `todo` + every file adding to the global scope, loaded in the full program's order
                ts = set(todo)
                out = self._run_meta(root, paths, analyze=todo, context=sorted(p for p, e in entries.items() if e.context),
                                     order={p: e.order for p, e in entries.items() if p not in ts})
            global_diags = [d for d in out.get("diagnostics", []) if isinstance(d, str)]
            got = {f["path"]: f for f in out.get("files", []) if isinstance(f, dict) and isinstance(f.get("path"), str)}
            fdiag = {d["path"]: d for d in out.get("file_diagnostics", []) if isinstance(d, dict) and d.get("path")}
            for p in todo:
                if p in got:
                    f = got[p]
                    meta = f.pop("meta", None)
                    if not isinstance(meta, dict):
                        raise TsAnalyzerUnavailable("ts-analyzer returned no dependency data (old build?)")
                    fresh[p] = entries[p] = Entry.from_output(f, None, meta)
                elif p in fdiag:
                    d = fdiag[p]
                    meta = d.get("meta")
                    fresh[p] = entries[p] = Entry.from_output(None, [str(d.get("kind")), str(d.get("text"))],
                                                              meta if isinstance(meta, dict) else {"context": True})
                    if not isinstance(meta, dict):
                        keys.pop(p, None)  # what it depends on is unknown: analyze it again next time
                else:
                    raise TsAnalyzerUnavailable(f"ts-analyzer returned nothing for {p}")
            valid -= set(todo)
            # Globals declared by freshly analyzed files may be used by reused ones (a new or changed script):
            # those, and whatever depends on them, are analyzed in one more pass.
            gidx = GlobalIndex(entries)
            seeds = {p for p in valid if not gidx.declarers(p, entries[p]) <= {*entries[p].deps, *entries[p].shallow}}
            todo = sorted(dependents(seeds, {p: entries[p] for p in valid}) & valid) if seeds else []

        if fresh:
            gidx = GlobalIndex(entries)
            for p, e in fresh.items():
                e.shallow = sorted(gidx.declarers(p, e) - set(e.deps))
                if any(TS_EXT.search(d) and d not in universe for d in e.deps):
                    keys.pop(p, None)  # depends on a TS/JS file outside the analysis, whose own deps are unknown
            hashes = cone_hashes({p: e.deps for p, e in entries.items()}, content, (),
                                 {p: e.shallow for p, e in entries.items()})
            store = {}
            for p, e in fresh.items():
                if p in keys:
                    e.cone = hashes[p]
                    store[keys[p]] = e.to_json()
            store[gkey] = json.dumps(global_diags or []).encode()
            self.cache.put_many(store)

        self.last_analyzed = sorted(fresh)
        res = [entries[p] for p in paths if p in entries]
        diags = list(global_diags or [])
        diags += [e.diag[1] for e in res if e.diag and e.diag[0] == "skip"]
        diags += [e.diag[1] for e in res if e.diag and e.diag[0] != "skip"]
        self.last_stats = {"side": side, "files": len(paths), "analyzed": len(fresh), "reused": len(paths) - len(fresh),
                           "passes": passes, "mode": "incremental", "s": round(time.perf_counter() - t0, 3)}
        return {"version": "1", "files": [e.result for e in res if e.result], "diagnostics": diags}, texts

    def _run_meta(self, root: Path, paths: list[str], **extra) -> dict:
        # A cold run (every file, plus dependency data) needs a little more heap than a plain full run, which already
        # sits near Node's default limit on an 8k-file program; incremental runs need far less.
        res = self._run(root, paths, node_args=_heap_args(), meta=True, **extra)
        if res.returncode != 0:
            raise TsAnalyzerUnavailable(f"ts-analyzer exited {res.returncode}: {res.stderr.strip()[:300]}")
        return json.loads(res.stdout)

    def _env(self, root: Path) -> str:
        """Everything besides a file's own content and its dependencies that its result depends on: this analyzer's
        code, the TypeScript version, the repository's root tsconfig (and the local configs it extends)."""
        h = hashlib.sha256(f"{ANALYZER_VERSION}\0{CACHE_FORMAT}\0{MAX_BYTES}".encode())
        for f in (self.cli_path, self.cli_path.with_name("analyze.js"),
                  self.cli_path.parent.parent / "node_modules" / "typescript" / "package.json"):
            h.update(b"\0" + (f.read_bytes() if f.is_file() else b"-"))
        cfg, seen = "tsconfig.json", set()
        while cfg and cfg not in seen and len(seen) < 8:
            seen.add(cfg)
            raw = read_repo_bytes(root, cfg, MAX_BYTES)
            h.update(b"\0" + cfg.encode() + b"\0" + (raw if raw is not None else b"-"))
            m = re.search(rb'"extends"\s*:\s*"([^"]+)"', raw or b"")
            nxt = m.group(1).decode("utf-8", "replace") if m else ""
            cfg = _norm(str(Path(cfg).parent / nxt)) if nxt.startswith(".") else ""  # a package's config: not in the repo
        return h.hexdigest()[:32]


def _heap_args() -> tuple[str, ...]:
    """Node heap limit: a quarter of physical memory, at least Node's usual 4 GiB, at most 8 GiB."""
    try:
        ram_mb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1 << 20)
    except (ValueError, OSError, AttributeError):
        return ()
    return (f"--max-old-space-size={max(4096, min(8192, ram_mb // 4))}",)


def _key(env: str, path: str, blob: str) -> str:
    return hashlib.sha256(f"ts\0{env}\0{path}\0{blob}".encode("utf-8", "surrogatepass")).hexdigest()


def _norm(p: str) -> str:
    parts: list[str] = []
    for seg in p.split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg not in ("", "."):
            parts.append(seg)
    out = "/".join(parts)
    return out if out.endswith(".json") else out + ".json"


def _content_id(root: Path, path: str, keep: dict[str, bytes] | None = None) -> str | None:
    """Git blob SHA of what the analyzer reads of the file (its first MAX_BYTES); for a package.json, a hash of the
    fields module resolution reads. None when the file cannot be read (symlink, missing, not a regular file)."""
    raw = read_repo_bytes(root, path, MAX_BYTES)
    if raw is None:
        return None
    if keep is not None:
        keep[path] = raw
    if path == "package.json" or path.endswith("/package.json"):
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                sel = json.dumps({k: d.get(k) for k in PKG_FIELDS}, sort_keys=True)
                return "pkg:" + hashlib.sha256(sel.encode()).hexdigest()
        except ValueError:
            pass
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


def _lines(root: Path, path: str, texts: dict[str, bytes] | None = None) -> list[str]:
    raw = texts.get(path) if texts else None
    text = raw.decode("utf-8", "replace") if raw is not None else read_repo_text(root, path, MAX_BYTES)
    return text.splitlines() if text is not None else []


def map_output(out: dict, g: SideGraph, root: Path, side: str, requested: set[str],
               texts: dict[str, bytes] | None = None) -> None:
    def ev(path: str, line: int, desc: str, *disc: object) -> str:
        return g.add_evidence(Evidence(ids.evidence_id("ts_compiler", side, path, line, *disc), "ts_compiler", desc,
                                       [CodeRef(side, path, line)], PRODUCER))

    files = [f for f in out.get("files", []) if isinstance(f, dict) and f.get("path") in g.files]
    sym_ids: dict[tuple[str, str], str] = {}
    for f in files:
        path = f["path"]
        lines = _lines(root, path, texts)
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
