"""import-linter configuration → ImportContract (spec v2 §8 Documented Rule). Reads only; never runs import-linter.

Sources (repository root): `.importlinter`, `.importlinter.ini`, `setup.cfg` ([importlinter] sections) and
`pyproject.toml` ([tool.importlinter]). Contract types layers / forbidden / independence are read as such; an unknown
custom type (e.g. `custom_forbidden`) is read as forbidden when it lists source and forbidden modules, as layers when it
lists layers, as independence when it lists modules, and skipped otherwise.
"""

from __future__ import annotations

import configparser
import re
import tomllib
from pathlib import Path

from ...domain.model.convention import ImportContract
from ..security.repo_files import read_repo_text

INI_FILES = (".importlinter", ".importlinter.ini", "setup.cfg")
TOML_FILE = "pyproject.toml"
KNOWN = {"layers", "forbidden", "independence"}
MAX_BYTES = 500_000
_INI_HEADER = re.compile(r"^\s*\[importlinter:contract:([^\]]+)\]\s*$")
_TOML_HEADER = re.compile(r"^\s*\[\[\s*tool\.importlinter\.contracts\s*\]\]\s*$")


def _lines(value: object) -> list[str]:
    if isinstance(value, list):
        items = [str(v) for v in value]
    else:
        items = str(value or "").splitlines()
    out = []
    for raw in items:
        v = raw.split("#", 1)[0].strip()
        if v:
            out.append(v)
    return out


def _kind(declared: str, fields: dict) -> str | None:
    if declared in KNOWN:
        return declared
    if fields.get("source_modules") and fields.get("forbidden_modules"):
        return "forbidden"
    if fields.get("layers"):
        return "layers"
    if fields.get("modules"):
        return "independence"
    return None


def _layers(lines: list[str]) -> tuple[list[list[str]], list[bool]]:
    layers, independent = [], []
    for ln in lines:
        sep = "|" if "|" in ln else (":" if ":" in ln else None)
        parts = [p.strip().strip("()").strip() for p in (ln.split(sep) if sep else [ln])]
        layers.append([p for p in parts if p])
        independent.append(sep == "|")
    return layers, independent


def _contract(key: str, fields: dict, path: str, line: int, roots: list[str], text_lines: list[str]) -> ImportContract | None:
    declared = str(fields.get("type") or "").strip()
    lists = {k: _lines(fields.get(k)) for k in ("source_modules", "forbidden_modules", "layers", "containers", "modules",
                                                 "ignore_imports")}
    kind = _kind(declared, lists)
    if kind is None:
        return None
    layers, independent = _layers(lists["layers"])
    ignore_lines: dict[str, int] = {}
    for entry in lists["ignore_imports"]:
        for i in range(line, len(text_lines) + 1):  # first occurrence after the contract header
            if entry in text_lines[i - 1]:
                ignore_lines[entry] = i
                break
    return ImportContract(
        key=key, name=str(fields.get("name") or "").strip() or f"{declared or kind} contract {key}", kind=kind,
        declared_type=declared or kind, path=path, line=line, layers=layers, layer_independent=independent,
        containers=lists["containers"], source_modules=lists["source_modules"],
        forbidden_modules=lists["forbidden_modules"], modules=lists["modules"], ignore_imports=lists["ignore_imports"],
        ignore_lines=ignore_lines, root_packages=roots,
    )


def _read(root: Path, rel: str) -> str | None:
    return read_repo_text(root, rel, MAX_BYTES)


def parse_ini(text: str, path: str) -> list[ImportContract]:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str  # keep key case
    cp.read_string(text)
    if not cp.has_section("importlinter") and not any(s.startswith("importlinter:contract:") for s in cp.sections()):
        return []
    roots = _lines(cp.get("importlinter", "root_packages", fallback="")) + \
        _lines(cp.get("importlinter", "root_package", fallback=""))
    text_lines = text.splitlines()
    headers = {m.group(1).strip(): i for i, ln in enumerate(text_lines, 1) if (m := _INI_HEADER.match(ln))}
    out = []
    for section in cp.sections():
        if not section.startswith("importlinter:contract:"):
            continue
        key = section.removeprefix("importlinter:contract:").strip()
        c = _contract(key, dict(cp.items(section)), path, headers.get(key, 1), roots, text_lines)
        if c is not None:
            out.append(c)
    return out


def parse_pyproject(text: str, path: str = TOML_FILE) -> list[ImportContract]:
    cfg = (tomllib.loads(text).get("tool") or {}).get("importlinter")
    if not isinstance(cfg, dict):
        return []
    roots = _lines(cfg.get("root_packages") or []) + _lines(cfg.get("root_package") or "")
    text_lines = text.splitlines()
    headers = [i for i, ln in enumerate(text_lines, 1) if _TOML_HEADER.match(ln)]
    out = []
    for n, fields in enumerate(c for c in cfg.get("contracts") or [] if isinstance(c, dict)):
        key = str(fields.get("id") or n + 1)
        c = _contract(key, fields, path, headers[n] if n < len(headers) else 1, roots, text_lines)
        if c is not None:
            out.append(c)
    return out


def read_contracts(root: Path, files: set[str]) -> tuple[list[ImportContract], list[str]]:
    """(contracts, notes). A config that cannot be parsed is reported in notes, never fatal."""
    contracts: list[ImportContract] = []
    notes: list[str] = []
    for rel in (*INI_FILES, TOML_FILE):
        if rel not in files:
            continue
        text = _read(root, rel)
        if text is None:
            continue
        try:
            found = parse_pyproject(text, rel) if rel == TOML_FILE else parse_ini(text, rel)
        except (configparser.Error, tomllib.TOMLDecodeError, ValueError) as exc:
            notes.append(f"could not parse {rel}: {type(exc).__name__}")
            continue
        contracts += found
    return contracts, notes
