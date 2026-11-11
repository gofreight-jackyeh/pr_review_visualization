"""What the repository itself documents: doc statements, doc excerpts and machine-checkable architecture contracts."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DocRule:
    """A normative statement found in a repo document (README, ADR, docs/). Context for Claude, never a verified rule."""

    path: str
    line: int
    text: str


@dataclass(frozen=True)
class DocExcerpt:
    """A slice of a repository document (README, docs/) used to explain the system."""

    path: str
    start_line: int
    end_line: int
    text: str


@dataclass
class ImportContract:
    """One import-linter contract (`.importlinter.ini`, `setup.cfg [importlinter]`, `pyproject [tool.importlinter]`).

    kind: layers | forbidden | independence — an unknown custom type with source/forbidden module lists (e.g. a project's own
    `custom_forbidden`) is read as forbidden. Module names are dotted, as written in the config."""

    key: str  # contract id from the header ("importlinter:contract:<key>"), or the list index in pyproject
    name: str
    kind: str
    declared_type: str
    path: str  # config file
    line: int  # line of the contract header
    layers: list[list[str]] = field(default_factory=list)  # top → bottom; one layer may hold siblings (a | b)
    layer_independent: list[bool] = field(default_factory=list)  # per layer: siblings separated by | are independent
    containers: list[str] = field(default_factory=list)
    source_modules: list[str] = field(default_factory=list)
    forbidden_modules: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)  # independence
    ignore_imports: list[str] = field(default_factory=list)  # "a.b -> c.d" (wildcards * / ** allowed)
    ignore_lines: dict[str, int] = field(default_factory=dict)  # ignore entry -> line in the config file
    root_packages: list[str] = field(default_factory=list)


# ---- client-facing surface (spec §8 interface rings): repository config + statically found outer interfaces ----

CONFIG_KEYS = ("paths", "routes", "packages")


@dataclass
class RepoConfig:
    """`.pr-explorer.yml` at the repository root (optional). Each list holds (glob, line in the file).

    outer:          declare more outer (client-facing) interfaces; config wins over detection (detected_by=config)
    exclude_outer:  drop detected outer interfaces (paths = file globs, routes = URL globs, packages = package names)"""

    path: str | None = None
    outer: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    exclude_outer: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WebRoute:
    """A page / route of a web front end found by file convention (Next.js, SvelteKit, Nuxt) or router config."""

    path: str  # file
    line: int
    route: str  # e.g. /orders/[id]
    how: str  # file_convention | router_config


@dataclass(frozen=True)
class PackageExport:
    name: str
    path: str
    line: int


@dataclass
class PackageInfo:
    """A package manifest (pyproject.toml / setup.cfg / package.json) and the top-level exports of its entry module."""

    name: str
    manifest: str
    line: int
    published: bool  # has a publish config or is not marked private
    reason: str
    exports: list[PackageExport] = field(default_factory=list)


@dataclass
class RepoSurface:
    config: RepoConfig = field(default_factory=RepoConfig)
    web_routes: list[WebRoute] = field(default_factory=list)
    packages: list[PackageInfo] = field(default_factory=list)
