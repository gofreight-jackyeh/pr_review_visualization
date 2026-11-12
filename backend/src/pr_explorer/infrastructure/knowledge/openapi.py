"""Committed OpenAPI / Swagger documents (spec §8 outer ring): the cheapest reliable HTTP API contract a repository has.

Django Ninja, FastAPI, drf-spectacular and many others can write `openapi.json`; teams that commit it give us the exact
operations, parameters and request / response fields without resolving any framework code. Read statically through the
safe reader; nothing is fetched or executed. `$ref`s are resolved inside the document only (no remote refs).

Files: `openapi.json|yaml|yml` / `swagger.json|yaml|yml` at any depth (outside vendored / build directories), plus the
paths listed under `openapi:` in `.pr-explorer.yml`. YAML documents need PyYAML; without it they are reported in
`RepoSurface.notes` and skipped (ceiling; upgrade path = add PyYAML to the backend dependencies).
"""

from __future__ import annotations

import bisect
import json
import re
from pathlib import Path

from ...domain.model.convention import ApiOperation, ApiSpec
from ...domain.services.rings import glob_match
from ..security.repo_files import read_repo_text

SPEC_NAMES = {"openapi.json", "openapi.yaml", "openapi.yml", "swagger.json", "swagger.yaml", "swagger.yml"}
SKIP = {"node_modules", "dist", "build", ".next", "coverage", ".venv", "venv", "site-packages", "__pycache__", "vendor"}
METHODS = ("get", "put", "post", "delete", "patch")
MAX_BYTES = 8_000_000
MAX_SPECS = 10
MAX_OPERATIONS = 3000
MAX_REF_DEPTH = 12
SUCCESS = re.compile(r"^2(\d\d|XX)$", re.I)


def _skipped(path: str) -> bool:
    return any(p in SKIP or p.startswith(".") for p in path.split("/")[:-1])


def _load(text: str, path: str) -> tuple[object | None, str | None]:
    if path.endswith(".json"):
        try:
            return json.loads(text), None
        except json.JSONDecodeError as exc:
            return None, f"{path}: not valid JSON ({exc.msg} at line {exc.lineno}); not diffed"
    try:
        import yaml  # optional: not a declared dependency
    except ImportError:
        return None, f"{path}: YAML OpenAPI documents need PyYAML, which is not installed; not diffed"
    try:
        return yaml.safe_load(text), None
    except yaml.YAMLError as exc:  # pragma: no cover - depends on the optional parser
        return None, f"{path}: not valid YAML ({type(exc).__name__}); not diffed"


class _Doc:
    def __init__(self, doc: dict):
        self.doc = doc

    def deref(self, node: object, depth: int = 0) -> object:
        while isinstance(node, dict) and isinstance(node.get("$ref"), str) and depth < MAX_REF_DEPTH:
            ref = node["$ref"]
            if not ref.startswith("#/"):
                return None  # remote refs are never fetched: not resolvable
            cur: object = self.doc
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                cur = cur.get(part) if isinstance(cur, dict) else None
            node, depth = cur, depth + 1
        return node if depth < MAX_REF_DEPTH else None

    def type_of(self, schema: object) -> str | None:
        if not isinstance(schema, dict):
            return None
        if isinstance(schema.get("$ref"), str):
            return schema["$ref"].rsplit("/", 1)[-1]
        for key in ("anyOf", "oneOf"):
            if isinstance(schema.get(key), list):
                return "|".join(sorted(t or "?" for t in (self.type_of(x) for x in schema[key])))
        t = schema.get("type")
        if isinstance(t, list):
            t = "|".join(sorted(str(x) for x in t))
        if t == "array":
            return f"{self.type_of(schema.get('items')) or '?'}[]"
        return str(t) if t is not None else None

    def fields(self, schema: object, depth: int = 0) -> dict[str, dict] | None:
        """Top-level fields of an object schema (array: of its items); None when not resolvable statically."""
        s = self.deref(schema)
        if not isinstance(s, dict) or depth > 6:
            return None
        if isinstance(s.get("allOf"), list):
            out: dict[str, dict] = {}
            for part in s["allOf"]:
                got = self.fields(part, depth + 1)
                if got is None:
                    return None
                out.update(got)
            req = s.get("required") if isinstance(s.get("required"), list) else []
            return {k: {**v, "required": v["required"] or k in req} for k, v in out.items()}
        for key in ("anyOf", "oneOf"):  # Optional[X] = anyOf [X, null]; any other union is not resolvable
            if isinstance(s.get(key), list):
                real = [x for x in s[key] if not (isinstance(x, dict) and x.get("type") == "null")]
                return self.fields(real[0], depth + 1) if len(real) == 1 else None
        if s.get("type") == "array" or "items" in s:
            return self.fields(s.get("items"), depth + 1)
        props = s.get("properties")
        if isinstance(props, dict):
            req = set(s.get("required") or []) if isinstance(s.get("required"), list) else set()
            return {str(k): {"required": k in req, "type": self.type_of(v)} for k, v in props.items()}
        if s.get("type") == "object" or not s:
            return {}  # free-form object / empty schema: no declared fields
        return None  # a scalar body or response


_PATH_KEY = re.compile(r"""(?m)(?:"(/[^"\n]*)"|'(/[^'\n]*)'|^[ \t]*(/[^:\s]*))[ \t]*:""")


def _line_finder(text: str):
    """Line of `<route>:` → `<method>:` in the document text. One pass over the path keys, then a method search bounded
    by the next path key (linear in the document size)."""
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    keys = [(m.start(), m.group(1) or m.group(2) or m.group(3)) for m in _PATH_KEY.finditer(text)]
    first: dict[str, int] = {}
    for i, (_, route) in enumerate(keys):
        first.setdefault(route, i)
    method_rx = {m: re.compile(rf"""(?m)(?:"{m}"|'{m}'|^\s*{m})\s*:""") for m in METHODS}

    def line_at(i: int) -> int:
        return bisect.bisect_right(starts, i)

    def find(route: str, method: str) -> int:
        k = first.get(route)
        if k is None:
            return 1
        pos, end = keys[k][0], keys[k + 1][0] if k + 1 < len(keys) else len(text)
        mm = method_rx[method].search(text, pos, end)
        return line_at(mm.start() if mm else pos)

    return find


def parse_spec(text: str, path: str) -> tuple[ApiSpec | None, str | None]:
    raw, err = _load(text, path)
    if err:
        return None, err
    if not isinstance(raw, dict) or not isinstance(raw.get("paths"), dict):
        return None, f"{path}: no `paths` object; not an OpenAPI document"
    d = _Doc(raw)
    v2 = "swagger" in raw
    find = _line_finder(text)
    title = str((raw.get("info") or {}).get("title") or path) if isinstance(raw.get("info"), dict) else path
    spec = ApiSpec(path, title[:200])
    for route, item in sorted(raw["paths"].items()):
        item = d.deref(item)
        if not isinstance(item, dict) or not isinstance(route, str):
            continue
        shared = item.get("parameters") if isinstance(item.get("parameters"), list) else []
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict) or len(spec.operations) >= MAX_OPERATIONS:
                continue
            params: dict[str, dict] = {}
            request: dict[str, dict] | None = {}  # {} = no request body
            own = op.get("parameters") if isinstance(op.get("parameters"), list) else []
            for p in [*shared, *own]:
                p = d.deref(p)
                if not isinstance(p, dict) or not isinstance(p.get("name"), str):
                    continue
                where = str(p.get("in") or "query")
                if where == "body":  # Swagger 2 body parameter
                    request = d.fields(p.get("schema"))
                    continue
                if where == "formData":
                    request = {**(request or {}), p["name"]: {"required": bool(p.get("required")), "type": p.get("type")}}
                    continue
                params[p["name"]] = {"in": where, "required": bool(p.get("required")) or where == "path",
                                     "type": d.type_of(p.get("schema")) if not v2 else p.get("type")}
            body = d.deref(op.get("requestBody"))
            if isinstance(body, dict) and isinstance(body.get("content"), dict) and body["content"]:
                content = body["content"]
                media = content.get("application/json") or next(iter(content.values()))
                request = d.fields(media.get("schema")) if isinstance(media, dict) else None
            spec.operations.append(ApiOperation(method.upper(), route, find(route, method), op.get("operationId"),
                                                params, request, _response(d, op, v2)))
    return spec, None


def _response(d: _Doc, op: dict, v2: bool) -> dict[str, dict] | None:
    responses = op.get("responses")
    if not isinstance(responses, dict):
        return None
    codes = sorted(str(c) for c in responses if SUCCESS.match(str(c)))
    resp = d.deref(responses.get(codes[0]) if codes else responses.get("default"))
    if not isinstance(resp, dict):
        return None
    if v2:
        return d.fields(resp["schema"]) if "schema" in resp else {}
    content = resp.get("content")
    if not isinstance(content, dict) or not content:
        return {}  # no body (e.g. 204)
    media = content.get("application/json") or next(iter(content.values()))
    return d.fields(media.get("schema")) if isinstance(media, dict) else None


def read_api_specs(root: Path, files: set[str], configured: list[tuple[str, int]]) -> tuple[list[ApiSpec], list[str]]:
    wanted: dict[str, int | None] = {}
    for f in sorted(files):
        hit = next((line for g, line in configured if glob_match(g, f)), None)
        if hit is not None:
            wanted[f] = hit
        elif f.rsplit("/", 1)[-1].lower() in SPEC_NAMES and not _skipped(f):
            wanted.setdefault(f, None)
    specs: list[ApiSpec] = []
    notes: list[str] = []
    for g, line in configured:
        if not any(glob_match(g, f) for f in files):
            notes.append(f".pr-explorer.yml:{line}: openapi document '{g}' not found")
    for f, line in list(wanted.items())[:MAX_SPECS]:
        text = read_repo_text(root, f, MAX_BYTES + 1)
        if text is None:
            continue
        if len(text) > MAX_BYTES:
            notes.append(f"{f}: larger than {MAX_BYTES // 1_000_000} MB; not diffed")
            continue
        spec, err = parse_spec(text, f)
        if err:
            if line is not None or f.endswith((".yaml", ".yml")) or "not valid" in err:
                notes.append(err)
            continue
        assert spec is not None
        spec.configured_line = line
        specs.append(spec)
    if len(wanted) > MAX_SPECS:
        notes.append(f"{len(wanted)} OpenAPI documents found; only the first {MAX_SPECS} are diffed")
    return specs, notes


__all__ = ["parse_spec", "read_api_specs"]
