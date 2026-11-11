"""Client-facing surface of a repository (spec §8 outer ring), read statically — nothing is executed or installed.

* `.pr-explorer.yml` (optional, repository root): `outer:` / `exclude_outer:` glob lists for paths / routes / packages.
  Parsed by a tiny YAML-subset reader (two levels of keys, `- item` or `[a, b]` lists) so no YAML dependency is needed;
  anything else in the file is reported as a note and ignored.
* Web app routes: file conventions (Next.js pages/ and app/, SvelteKit src/routes, Nuxt pages/) plus `path: "..."`
  entries in router config files (files whose name contains "route"/"router"). Cheap and regex-based by design:
  the ceiling is dynamic route tables; upgrade path = emit routes from tools/ts-analyzer.
* Packages: pyproject.toml / setup.cfg / package.json. Published = a publish config, or not marked private
  (package.json `"private": true`, pyproject classifier "Private :: Do Not Upload" or poetry `package-mode = false`).
  Exports = `__all__` / public top-level names of the package's `__init__.py`, or `export ...` of the JS entry file.
"""

from __future__ import annotations

import ast
import configparser
import json
import re
import tomllib
from pathlib import Path

from ...domain.model.convention import CONFIG_KEYS, PackageExport, PackageInfo, RepoConfig, RepoSurface, WebRoute
from ..security.repo_files import read_repo_text

CONFIG_FILES = (".pr-explorer.yml", ".pr-explorer.yaml")
SKIP = {"node_modules", "dist", "build", ".next", "coverage", ".venv", "venv", "site-packages", "__pycache__"}
JS_EXT = (".tsx", ".ts", ".jsx", ".js", ".mjs", ".vue", ".svelte")
MAX_BYTES = 300_000
MAX_ROUTER_FILES = 200
MAX_EXPORTS = 200
_ROUTER_NAME = re.compile(r"route", re.I)
_ROUTE_PATH = re.compile(r"""\bpath\s*[:=]\s*["'`]([^"'`\n]{1,200})["'`]""")
_JSX_ROUTE = re.compile(r"""<Route\b[^>]*\bpath\s*=\s*["'{`]+([^"'`}\n]{1,200})""")
_EXPORT_DECL = re.compile(r"^\s*export\s+(?:default\s+)?(?:declare\s+)?(?:async\s+)?(?:function\*?|class|const|let|var|interface|type|enum)\s+([A-Za-z_$][\w$]*)", re.M)
_EXPORT_LIST = re.compile(r"^\s*export\s*(?:type\s*)?\{([^}]*)\}", re.M)


def _skipped(path: str) -> bool:
    return any(p in SKIP or (p.startswith(".") and p not in CONFIG_FILES) for p in path.split("/")[:-1])


def _read(root: Path, path: str) -> str | None:
    return read_repo_text(root, path, MAX_BYTES)


# ---------------------------------------------------------------- .pr-explorer.yml

def _unquote(v: str) -> str:
    v = v.strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for ch in line:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def parse_repo_config(text: str, path: str = CONFIG_FILES[0]) -> RepoConfig:
    """outer / exclude_outer → {paths|routes|packages: [(glob, line)]}. A bare list under `outer:` means paths."""
    cfg = RepoConfig(path=path)
    top: str | None = None
    sub: str | None = None
    for no, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line.strip() or line.strip() == "---":
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        target = {"outer": cfg.outer, "exclude_outer": cfg.exclude_outer}.get(top or "")
        if body.startswith("- "):
            if target is None:
                cfg.notes.append(f"{path}:{no}: list item outside outer / exclude_outer ignored")
                continue
            target.setdefault(sub or "paths", []).append((_unquote(body[2:]), no))
            continue
        key, sep, value = body.partition(":")
        key, value = key.strip(), value.strip()
        if not sep:
            cfg.notes.append(f"{path}:{no}: not understood, ignored")
            continue
        if indent == 0:
            top, sub = key, None
            if key not in ("outer", "exclude_outer"):
                cfg.notes.append(f"{path}:{no}: unknown key '{key}' ignored")
                top = None
                continue
            target = cfg.outer if key == "outer" else cfg.exclude_outer
            if value:
                _inline(value, target, "paths", no)
            continue
        if target is None:
            continue
        if key not in CONFIG_KEYS:
            cfg.notes.append(f"{path}:{no}: unknown key '{top}.{key}' ignored (use {', '.join(CONFIG_KEYS)})")
            sub = "__ignored__"
            continue
        sub = key
        if value:
            _inline(value, target, key, no)
    cfg.outer.pop("__ignored__", None)
    cfg.exclude_outer.pop("__ignored__", None)
    return cfg


def _inline(value: str, target: dict, key: str, no: int) -> None:
    items = value[1:-1].split(",") if value.startswith("[") and value.endswith("]") else [value]
    for v in items:
        if _unquote(v):
            target.setdefault(key, []).append((_unquote(v), no))


def read_config(root: Path, files: set[str]) -> RepoConfig:
    for name in CONFIG_FILES:
        if name in files:
            text = _read(root, name)
            if text is not None:
                return parse_repo_config(text, name)
    return RepoConfig()


# ---------------------------------------------------------------- web app routes

def _convention_route(path: str) -> str | None:
    if not path.endswith(JS_EXT):
        return None
    parts = path.split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    for i, seg in enumerate(parts[:-1]):
        rest = parts[i + 1:-1]
        if seg == "pages" and path.endswith((".tsx", ".jsx", ".js", ".ts", ".vue")):  # Next.js pages router / Nuxt
            if stem.startswith("_") or (rest and rest[0] == "api") or ".test" in parts[-1] or ".spec" in parts[-1]:
                return None
            segs = rest + ([] if stem == "index" else [stem])
            return "/" + "/".join(segs)
        if seg == "app" and stem == "page":  # Next.js app router; (group) segments do not appear in the URL
            return "/" + "/".join(s for s in rest if not (s.startswith("(") and s.endswith(")")))
        if seg == "routes" and parts[-1] == "+page.svelte":  # SvelteKit
            return "/" + "/".join(s for s in rest if not (s.startswith("(") and s.endswith(")")))
    return None


def web_routes(root: Path, files: set[str]) -> list[WebRoute]:
    out: dict[str, WebRoute] = {}
    for f in sorted(files):
        if _skipped(f):
            continue
        r = _convention_route(f)
        if r is not None:
            out.setdefault(r, WebRoute(f, 1, r, "file_convention"))
    routers = [f for f in sorted(files) if f.endswith(JS_EXT) and not _skipped(f)
               and _ROUTER_NAME.search(f.rsplit("/", 1)[-1]) and ".test." not in f and ".spec." not in f]
    for f in routers[:MAX_ROUTER_FILES]:
        text = _read(root, f)
        if not text:
            continue
        for rx in (_ROUTE_PATH, _JSX_ROUTE):
            for m in rx.finditer(text):
                r = m.group(1).strip()
                if not r or r.startswith(("http", "./", "../")) or " " in r:
                    continue
                r = r if r.startswith("/") or r == "*" else "/" + r
                out.setdefault(r, WebRoute(f, text.count("\n", 0, m.start()) + 1, r, "router_config"))
    return sorted(out.values(), key=lambda w: w.route)


# ---------------------------------------------------------------- packages

def _line_of(text: str, needle: str) -> int:
    i = text.find(needle)
    return text.count("\n", 0, i) + 1 if i >= 0 else 1


def _py_exports(root: Path, init: str) -> list[PackageExport]:
    text = _read(root, init)
    if text is None:
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    names: dict[str, int] = {}
    declared: list[str] | None = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets):
            if isinstance(stmt.value, (ast.List, ast.Tuple)):
                declared = [e.value for e in stmt.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.setdefault(stmt.name, stmt.lineno)
        elif isinstance(stmt, ast.ImportFrom):
            for a in stmt.names:
                if a.name != "*":
                    names.setdefault(a.asname or a.name, stmt.lineno)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    names.setdefault(t.id, stmt.lineno)
    keep = declared if declared is not None else [n for n in names if not n.startswith("_")]
    return [PackageExport(n, init, names.get(n, 1)) for n in keep[:MAX_EXPORTS]]


def _js_exports(root: Path, entry: str) -> list[PackageExport]:
    text = _read(root, entry)
    if text is None:
        return []
    out: dict[str, int] = {}
    for m in _EXPORT_DECL.finditer(text):
        out.setdefault(m.group(1), text.count("\n", 0, m.start()) + 1)
    for m in _EXPORT_LIST.finditer(text):
        for part in m.group(1).split(","):
            name = part.split(" as ")[-1].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name or ""):
                out.setdefault(name, text.count("\n", 0, m.start()) + 1)
    return [PackageExport(n, entry, ln) for n, ln in list(out.items())[:MAX_EXPORTS]]


def _js_entry(base: str, pkg: dict, files: set[str]) -> str | None:
    cands: list[str] = []
    exp = pkg.get("exports")
    if isinstance(exp, str):
        cands.append(exp)
    elif isinstance(exp, dict):
        dot = exp.get(".", exp)
        if isinstance(dot, str):
            cands.append(dot)
        elif isinstance(dot, dict):
            cands += [v for k in ("source", "import", "default", "require", "types") if isinstance(v := dot.get(k), str)]
    cands += [v for k in ("source", "module", "main", "types") if isinstance(v := pkg.get(k), str)]
    cands += ["src/index.ts", "src/index.tsx", "src/index.js", "index.ts", "index.js"]
    for c in cands:
        c = c.removeprefix("./")
        stem = c.rsplit(".", 1)[0]
        for alt in (c, stem + ".ts", stem + ".tsx", re.sub(r"^(dist|lib|build)/", "src/", stem) + ".ts",
                    re.sub(r"^(dist|lib|build)/", "src/", stem) + ".tsx"):
            p = f"{base}/{alt}" if base else alt
            if p in files:
                return p
    return None


def _py_package_dirs(base: str, names: list[str], files: set[str]) -> list[str]:
    out = []
    for n in names:
        d = n.replace("-", "_").replace(".", "/")
        for cand in (f"{d}/__init__.py", f"src/{d}/__init__.py"):
            p = f"{base}/{cand}" if base else cand
            if p in files:
                out.append(p)
                break
    return out


def packages(root: Path, files: set[str]) -> list[PackageInfo]:
    out: list[PackageInfo] = []
    for f in sorted(files):
        if _skipped(f):
            continue
        name = f.rsplit("/", 1)[-1]
        base = f.rsplit("/", 1)[0] if "/" in f else ""
        if name == "package.json":
            text = _read(root, f)
            try:
                pkg = json.loads(text or "")
            except json.JSONDecodeError:
                continue
            if not isinstance(pkg, dict) or not isinstance(pkg.get("name"), str):
                continue
            published = bool(pkg.get("publishConfig")) or pkg.get("private") is not True
            reason = "publishConfig" if pkg.get("publishConfig") else ("not marked private" if published else "private: true")
            entry = _js_entry(base, pkg, files)
            out.append(PackageInfo(pkg["name"], f, _line_of(text or "", '"name"'), published, reason,
                                   _js_exports(root, entry) if entry else []))
        elif name == "pyproject.toml":
            text = _read(root, f)
            try:
                data = tomllib.loads(text or "")
            except tomllib.TOMLDecodeError:
                continue
            proj, poetry = data.get("project") or {}, (data.get("tool") or {}).get("poetry") or {}
            pname = proj.get("name") or poetry.get("name")
            if not isinstance(pname, str):
                continue
            private = ("Private :: Do Not Upload" in (proj.get("classifiers") or poetry.get("classifiers") or [])
                       or poetry.get("package-mode") is False)
            tool = data.get("tool") or {}
            listed = (((tool.get("hatch") or {}).get("build") or {}).get("targets") or {}).get("wheel", {}).get("packages") \
                or (tool.get("setuptools") or {}).get("packages") or [p.get("include") for p in poetry.get("packages", [])
                                                                       if isinstance(p, dict)]
            names = [str(p).removeprefix("src/") for p in (listed if isinstance(listed, list) else []) if p] or [pname]
            inits = _py_package_dirs(base, names, files)
            out.append(PackageInfo(pname, f, _line_of(text or "", "name"), not private,
                                   "marked private" if private else "not marked private",
                                   [e for i in inits for e in _py_exports(root, i)][:MAX_EXPORTS]))
        elif name == "setup.cfg":
            text = _read(root, f)
            cp = configparser.ConfigParser()
            try:
                cp.read_string(text or "")
            except configparser.Error:
                continue
            pname = cp.get("metadata", "name", fallback=None)
            if not pname:
                continue
            private = "Private :: Do Not Upload" in cp.get("metadata", "classifiers", fallback="")
            pk = [p.strip() for p in cp.get("options", "packages", fallback="").split() if p.strip() and "find" not in p]
            inits = _py_package_dirs(base, pk or [pname], files)
            out.append(PackageInfo(pname, f, _line_of(text or "", "name"), not private,
                                   "marked private" if private else "not marked private",
                                   [e for i in inits for e in _py_exports(root, i)][:MAX_EXPORTS]))
    return out


def scan_surface(root: Path, files: set[str]) -> RepoSurface:
    return RepoSurface(read_config(root, files), web_routes(root, files), packages(root, files))


__all__ = ["parse_repo_config", "packages", "read_config", "scan_surface", "web_routes"]
