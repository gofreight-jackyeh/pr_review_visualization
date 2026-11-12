"""Incremental TS analysis: which cached per-file results still hold (ts_incremental), the on-disk cache, and — with
the real ts-analyzer CLI — that an incremental run's output equals a full run's after each kind of change."""

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from pr_explorer.infrastructure.analyzers.typescript.ts_bridge import TypeScriptAnalyzer, ts_files
from pr_explorer.infrastructure.analyzers.typescript.ts_incremental import Entry, cone_hashes, reusable
from pr_explorer.infrastructure.persistence.file_cache import FileResultCache
from pr_explorer.infrastructure.process.runner import SubprocessRunner

CLI = Path(__file__).resolve().parents[3] / "tools/ts-analyzer/dist/cli.js"


# ------------------------------------------------------------------ pure invalidation rule

def _state(files: dict[str, str], deps: dict[str, list[str]], **extra):
    """files: path -> content id now; deps: path -> direct deps when the entries were made (all made at this state)."""
    content = files.get
    entries = {p: Entry({"path": p}, None, deps=list(deps.get(p, [])), **extra.get(p, {})) for p in files}
    hashes = cone_hashes({p: e.deps for p, e in entries.items()}, content, (), {p: e.shallow for p, e in entries.items()})
    for p, e in entries.items():
        e.cone = hashes[p]
    return entries


def _check(before: dict[str, str], deps, after: dict[str, str], **extra) -> set[str]:
    """Entries made at `before`; returns the files reusable at `after` (an entry exists only for unchanged content)."""
    entries = _state(before, deps, **extra)
    live = {p: e for p, e in entries.items() if after.get(p) == before[p]}
    return reusable(live, after.__contains__, after.get)


def test_changed_import_invalidates_importer_only():
    before = {"a.ts": "1", "b.ts": "1", "c.ts": "1"}
    assert _check(before, {"a.ts": ["b.ts"]}, {**before, "b.ts": "2"}) == {"c.ts"}
    assert _check(before, {"a.ts": ["b.ts"]}, before) == set(before)  # nothing changed: everything reused


def test_barrel_reexport_chain_is_followed():
    """page imports the barrel index.ts, which re-exports impl.ts: changing impl recomputes page and the barrel."""
    before = {"page.ts": "1", "lib/index.ts": "1", "lib/impl.ts": "1", "other.ts": "1"}
    deps = {"page.ts": ["lib/index.ts"], "lib/index.ts": ["lib/impl.ts"]}
    assert _check(before, deps, {**before, "lib/impl.ts": "2"}) == {"other.ts"}


def test_import_cycle_is_fine():
    before = {"a.ts": "1", "b.ts": "1", "c.ts": "1", "d.ts": "1"}
    deps = {"a.ts": ["b.ts"], "b.ts": ["a.ts", "c.ts"], "d.ts": ["a.ts"]}
    assert _check(before, deps, before) == set(before)
    assert _check(before, deps, {**before, "c.ts": "2"}) == set()  # c changed: a, b (cycle) and d depend on it


def test_deleted_file_invalidates_its_importers():
    before = {"a.ts": "1", "b.ts": "1", "c.ts": "1"}
    after = {"a.ts": "1", "c.ts": "1"}
    assert _check(before, {"a.ts": ["b.ts"]}, after) == {"c.ts"}


def test_renamed_file_invalidates_old_importers_and_files_that_probed_the_new_name():
    """b.ts renamed to n.ts: a (imported ./b) and x (whose `./n` did not resolve: probe n.ts) are recomputed."""
    before = {"a.ts": "1", "b.ts": "1", "x.ts": "1", "y.ts": "1"}
    after = {"a.ts": "1", "n.ts": "1", "x.ts": "1", "y.ts": "1"}
    got = _check(before, {"a.ts": ["b.ts"]}, after, **{"x.ts": {"probes": ["n.ts", "n.tsx", "n/index.ts"]}})
    assert got == {"y.ts"}


def test_new_global_declaration_invalidates_users_of_the_name():
    """A script declares `fmt` globally; files using `fmt` depend on it (only on its own content)."""
    before = {"g.js": "1", "use.js": "1", "lib.ts": "1", "other.ts": "1"}
    extra = {"g.js": {"module": False, "context": True, "globals": ["fmt"]},
             "use.js": {"vocab": ["fmt"], "shallow": ["g.js"]}, "other.ts": {"vocab": ["unrelated"]}}
    deps = {"g.js": ["lib.ts"]}
    assert _check(before, deps, before, **extra) == set(before)
    assert _check(before, deps, {**before, "g.js": "2"}, **extra) == {"lib.ts", "other.ts"}
    # shallow: what the global file imports does not count for its users
    assert _check(before, deps, {**before, "lib.ts": "2"}, **extra) == {"use.js", "other.ts"}
    # a second file now declaring the name is a dependency the entry did not record
    extra2 = {**extra, "h.js": {"module": False, "context": True, "globals": ["fmt"]}}
    entries = _state({**before, "h.js": "1"}, deps, **extra2)
    entries["use.js"].shallow = ["g.js"]
    assert "use.js" not in reusable(entries, lambda p: True, {**before, "h.js": "1"}.get)


def test_entry_round_trip_and_damage():
    e = Entry({"path": "a.ts"}, None, deps=["b.ts"], cone="x")
    assert Entry.from_json(e.to_json()) == e
    for bad in (b"{}", b"[]", b'{"cone": ""}', b'{"result": null, "diag": null, "cone": "x", "deps": "b"}'):
        with pytest.raises((ValueError, TypeError)):
            Entry.from_json(bad)


# ------------------------------------------------------------------ on-disk cache

def test_file_cache_lru_bound_and_corruption(tmp_path):
    c = FileResultCache(tmp_path / "c.db", max_bytes=10_000)
    c.put_many({f"k{i}": os.urandom(3000) for i in range(10)})  # incompressible: 3 KB each stored
    n, size = c.size()
    assert size <= 10_000 and 0 < n < 10
    c.put_many({"ok": b"hello"})
    assert c.get_many(["ok", "missing"]) == {"ok": b"hello"}
    with sqlite3.connect(tmp_path / "c.db") as db:  # a damaged value reads as a miss and is dropped
        db.execute("UPDATE entries SET data = x'00ff' WHERE key = 'ok'")
    assert c.get_many(["ok"]) == {} and c.size()[0] == n
    (tmp_path / "bad.db").write_bytes(b"not a database" * 100)  # a damaged file is moved aside and recreated
    c2 = FileResultCache(tmp_path / "bad.db")
    c2.put_many({"a": b"1"})
    assert c2.get_many(["a"]) == {"a": b"1"} and list(tmp_path.glob("bad.corrupt-*"))


# ------------------------------------------------------------------ real analyzer: incremental == full

needs_cli = pytest.mark.skipif(not CLI.is_file() or shutil.which("node") is None, reason="ts-analyzer not built")

BASE = {
    "tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@lib/*": ["src/lib/*"]}}}',
    "src/lib/price.ts": "export function price(n: number) { return n * 2 }\nexport function tax(n: number) { return n / 10 }\n",
    "src/lib/index.ts": "export * from './price'\n",
    "src/lib/cart.ts": "import { price } from './index'\nexport class Cart { total(n: number) { return price(n) } }\n",
    "src/page.tsx": "import { Cart } from '@lib/cart'\nimport { tax } from './lib'\n"
                    "export function Page() { const c = new Cart(); return c.total(tax(1)) }\n",
    "src/legacy/util.js": "function fmtMoney(x) { return '$' + x }\n",
    "src/legacy/view.js": "function render(x) { return fmtMoney(x) }\n",
    "src/other.ts": "import { later } from './later'\nexport const o = () => later()\n",
}


def _write(root: Path, files: dict[str, str | None]) -> None:
    for p, text in files.items():
        f = root / p
        if text is None:
            f.unlink()
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)


def _out(an: TypeScriptAnalyzer, root: Path, incremental: bool) -> dict:
    files = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    paths = ts_files(files)
    if incremental:
        return an._incremental(root, files, paths, "base")[0]
    return json.loads(an._run(root, paths).stdout)


@needs_cli
@pytest.mark.parametrize("change, expect_reanalyzed", [
    ({"src/lib/price.ts": "export function price(n: number) { return n * 3 }\nexport function tax2(n: number) { return n }\n"},
     {"src/lib/price.ts", "src/lib/index.ts", "src/lib/cart.ts", "src/page.tsx"}),  # changed import via the barrel
    ({"src/lib/index.ts": "export { price } from './price'\n"},
     {"src/lib/index.ts", "src/lib/cart.ts", "src/page.tsx"}),  # barrel re-export narrowed
    ({"src/legacy/util.js": None}, {"src/legacy/view.js"}),  # deleted file (a global script)
    ({"src/lib/price.ts": None, "src/lib/pricing.ts": BASE["src/lib/price.ts"],
      "src/lib/index.ts": "export * from './pricing'\n"},
     {"src/lib/pricing.ts", "src/lib/index.ts", "src/lib/cart.ts", "src/page.tsx"}),  # renamed file
    ({"src/later.ts": "export function later() { return 1 }\n"}, {"src/later.ts", "src/other.ts"}),  # import now resolves
    ({"src/legacy/util2.js": "function fmtMoney(x) { return x }\n"},  # a second global declaration of a used name
     {"src/legacy/util2.js", "src/legacy/view.js", "src/legacy/util.js"}),
])
def test_incremental_equals_full_after_change(tmp_path, change, expect_reanalyzed):
    root = tmp_path / "repo"
    _write(root, BASE)
    cache = FileResultCache(tmp_path / "cache.db")
    inc = TypeScriptAnalyzer(SubprocessRunner(), CLI, cache=cache)
    full = TypeScriptAnalyzer(SubprocessRunner(), CLI)
    assert _out(inc, root, True) == _out(full, root, False)  # cold: everything analyzed, cache filled
    assert inc.last_stats["analyzed"] == len(ts_files(set(BASE)))
    assert _out(inc, root, True) == _out(full, root, False) and inc.last_stats["analyzed"] == 0  # warm, unchanged
    _write(root, change)
    assert _out(inc, root, True) == _out(full, root, False)
    assert set(inc.last_analyzed) == expect_reanalyzed
