"""Which cached per-file TS results still hold at a commit, and what a fresh result depends on (used by ts_bridge).

A file's result (symbols, imports, resolved calls/renders) is a function of its own text and of what the type checker
can reach from it: its import closure (imports, re-exports through barrel files, `import()` types, JSDoc imports,
`/// <reference path>`, package.json files read while resolving) and the global scope, which in the full program holds
every script (a file without import/export) and every `declare global` / module augmentation. So each entry records:

- `deps`: the files it references directly — resolved module references and the files its calls/renders resolved
  into. These are followed transitively.
- `shallow`: the global files declaring a name it uses (`vocab` matched against their `globals`). Only their own
  content counts, not what they import: following those would tie every file that mentions `window` or `$` to the
  import cone of every spec that assigns `window.x = ...` (a third of a large frontend for a one-interface change).
  A type that reaches a file through a global AND changes where one of its calls resolves still invalidates it when
  the old target changes (targets are `deps`); not covered: a call that was unresolved becoming resolvable that way.
- `probes`: in-repository paths module resolution looked for and did not find (one appearing changes resolution);
- `cone`: a Merkle hash over its whole dependency cone — the content of every file reachable through `deps` (plus
  each one's `shallow` files) and the shape of that graph (strongly connected components hashed together, so import
  cycles are fine).

An entry is reused only when nothing reachable from it is dirty (no entry for its current content, a probe that now
exists, a global file that newly declares a name it uses) and the cone hash computed now equals the stored one. Any
change in the cone recomputes the file, even when its result would not have changed.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

TS_EXT = re.compile(r"\.(ts|tsx|js|jsx|mjs|cjs|mts|cts)$")


@dataclass
class Entry:
    """One file's cached result. `result` is the analyzer's per-file output; `diag` = [kind, text] when the file was
    skipped ("skip") or could not be analyzed ("fail")."""

    result: dict | None
    diag: list[str] | None
    module: bool = True
    context: bool = False
    globals: list[str] = field(default_factory=list)
    vocab: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
    shallow: list[str] = field(default_factory=list)
    probes: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)  # files TypeScript loads for it, in load order (program order)
    cone: str = ""

    def to_json(self) -> bytes:
        return json.dumps(self.__dict__, separators=(",", ":")).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> "Entry":
        d = json.loads(raw)
        if not isinstance(d, dict) or not isinstance(d.get("cone"), str) or not d["cone"]:
            raise ValueError("not a cache entry")
        e = cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
        if not all(isinstance(x, list) for x in (e.globals, e.vocab, e.deps, e.shallow, e.probes, e.order)):
            raise ValueError("not a cache entry")
        return e

    @classmethod
    def from_output(cls, result: dict | None, diag: list[str] | None, meta: dict) -> "Entry":
        deps = set(meta.get("deps") or [])
        if result:
            for c in [*result.get("calls", []), *result.get("component_usages", [])]:
                r = c.get("resolved")
                if isinstance(r, dict) and isinstance(r.get("path"), str):
                    deps.add(r["path"])
            deps.discard(result.get("path"))
        return cls(result, diag, bool(meta.get("module", True)), bool(meta.get("context", False)),
                   list(meta.get("globals") or []), list(meta.get("vocab") or []), sorted(deps), [],
                   list(meta.get("probes") or []), list(meta.get("order") or []))


class GlobalIndex:
    """Global name -> the files declaring it (scripts' top-level names, `declare global` members, ambient modules)."""

    def __init__(self, entries: dict[str, Entry]):
        self.names: dict[str, set[str]] = {}
        self.wild: list[tuple[re.Pattern, str]] = []  # `declare module "*.html"`
        for p, e in entries.items():
            if not e.context:
                continue
            for n in e.globals:
                if n.startswith('"') and "*" in n:
                    body = n[1:-1]
                    self.wild.append((re.compile("^" + ".*".join(map(re.escape, body.split("*"))) + "$"), p))
                else:
                    self.names.setdefault(n, set()).add(p)

    def declarers(self, path: str, e: Entry) -> set[str]:
        out: set[str] = set()
        for n in e.vocab:
            hit = self.names.get(n)
            if hit:
                out |= hit
            if self.wild and n.startswith('"'):
                out.update(p for rx, p in self.wild if rx.match(n[1:-1]))
        out.discard(path)
        return out


def dirty(entries: dict[str, Entry], exists: Callable[[str], bool], gidx: GlobalIndex) -> set[str]:
    """Entries whose own dependency facts no longer hold: a probe exists now, or a global file declares a name the
    file uses without being among its deps (a new global, or one that was not there when the entry was made)."""
    out = set()
    for p, e in entries.items():
        if any(exists(x) for x in e.probes) or not gidx.declarers(p, e) <= {*e.deps, *e.shallow}:
            out.add(p)
    return out


def cone_hashes(graph: dict[str, list[str]], content: Callable[[str], str | None],
                poisoned: Iterable[str] = (), shallow: dict[str, list[str]] | None = None) -> dict[str, str]:
    """Merkle hash of every node's reachable set. `graph`: node -> direct deps (nodes with a trusted entry); any other
    path is a leaf hashed by its content (None = missing). `shallow`: node -> files whose content alone counts.
    A poisoned node hashes to a value no stored cone can hold."""
    shallow = shallow or {}
    poison = set(poisoned)
    nonce = secrets.token_hex(8)
    succ = {p: ([] if p in poison else ds) for p, ds in graph.items()}
    for p in poison:
        succ.setdefault(p, [])
    adj = {p: [d for d in ds if d in succ] for p, ds in succ.items()}  # edges between nodes; the rest are leaves
    leaf_hash: dict[str, str] = {}

    def leaf(p: str) -> str:
        if p not in leaf_hash:
            leaf_hash[p] = _h(f"leaf\0{p}\0{content(p)}")
        return leaf_hash[p]

    # Tarjan's SCC (iterative); components come out in reverse topological order (dependencies first).
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    comp_of: dict[str, int] = {}
    comp_hash: list[str] = []
    counter = 0
    for start in succ:
        if start in index:
            continue
        work = [(start, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on.add(v)
            ds = adj[v]
            if i < len(ds):
                work.append((v, i + 1))
                w = ds[i]
                if w not in index:
                    work.append((w, 0))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            for w in ds:
                if w in on:
                    low[v] = min(low[v], low[w])
            if low[v] == index[v]:
                members = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    members.append(w)
                    if w == v:
                        break
                cid = len(comp_hash)
                for m in members:
                    comp_of[m] = cid
                own = sorted(f"{m}\t{('poison:' + nonce) if m in poison else content(m)}" for m in members)
                own += sorted({f"~{x}\t{content(x)}" for m in members if m not in poison for x in shallow.get(m, ())})
                outs = sorted({comp_hash[comp_of[d]] if d in succ else leaf(d)
                               for m in members for d in succ[m] if not (d in succ and comp_of.get(d) == cid)})
                comp_hash.append(_h("\n".join(own) + "\n--\n" + "\n".join(outs)))
    return {p: comp_hash[comp_of[p]] for p in succ}


def reusable(entries: dict[str, Entry], exists: Callable[[str], bool], content: Callable[[str], str | None]) -> set[str]:
    """`entries`: path -> the entry for the file's CURRENT content (files without one are simply absent). Returns
    the paths whose entry may be reused as is."""
    bad = dirty(entries, exists, GlobalIndex(entries))
    hashes = cone_hashes({p: e.deps for p, e in entries.items()}, content, bad, {p: e.shallow for p, e in entries.items()})
    return {p for p, e in entries.items() if p not in bad and hashes[p] == e.cone}


def dependents(seeds: set[str], entries: dict[str, Entry]) -> set[str]:
    """seeds plus every entry that reaches one of them through deps."""
    rev: dict[str, list[str]] = {}
    for p, e in entries.items():
        for d in e.deps:
            rev.setdefault(d, []).append(p)
    out, todo = set(seeds), list(seeds)
    while todo:
        for q in rev.get(todo.pop(), ()):
            if q not in out:
                out.add(q)
                todo.append(q)
    return out


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()
