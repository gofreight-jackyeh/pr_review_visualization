"""Ports (interfaces). Implementations live in infrastructure/; bootstrap.py wires them."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ...domain.model.changes import ChangedFile
from ...domain.model.convention import DocExcerpt, DocRule, ImportContract, RepoSurface
from ...domain.model.graph import SideGraph
from ...domain.model.semantic import AiChoice, SemanticDraft

# Bump on ANY change that alters analysis output (prompts, caps, rules, detection): it is part of every cache key,
# so forgetting it serves stale results. Claude texts still carry over across versions via map reuse.
ANALYZER_VERSION = "2.6.0"


class SourceError(RuntimeError):
    """Acquisition failed (bad repo/ref/URL, git/gh failure)."""


class AnalyzerUnavailable(RuntimeError):
    """A deep analyzer could not run; the pipeline degrades to generic analysis and records a note."""


@dataclass
class LocalSpec:
    repo_path: str
    base: str
    head: str
    title: str | None = None
    body: str | None = None


@dataclass
class ContextDoc:
    name: str
    content: str


@dataclass
class AnalysisRequest:
    pr_url: str | None = None
    local: LocalSpec | None = None
    context_docs: list[ContextDoc] = field(default_factory=list)
    semantic_mode: str = "auto"
    ai: AiChoice | None = None  # who writes the PR texts and answers its questions; None = the server default


@dataclass
class ResolvedPr:
    """PR identity bound to exact commits (spec §5 L1)."""

    source: str  # github | local
    repo_key: str  # cache directory key
    repo_name: str
    repo_owner: str | None
    base_ref: str
    head_ref: str
    base_sha: str
    head_sha: str
    merge_base_sha: str
    title: str
    body: str
    url: str | None = None
    number: int | None = None
    author: str | None = None
    descriptor: dict[str, Any] = field(default_factory=dict)  # how to re-resolve the head (freshness)


@dataclass
class Workspace:
    base_root: Path
    head_root: Path
    diff_text: str
    base_files: set[str]
    head_files: set[str]


class SourceProvider(Protocol):
    def resolve(self, request: AnalysisRequest) -> ResolvedPr: ...
    def materialize(self, pr: ResolvedPr) -> Workspace: ...  # holds the worktrees until release(pr)
    def retain(self, pr: ResolvedPr) -> None: ...  # one more hold (background work outliving the analysis)
    def release(self, pr: ResolvedPr) -> None: ...  # last hold removes the worktrees
    def current_head_sha(self, descriptor: dict[str, Any]) -> str: ...
    def blobs(self, pr: ResolvedPr, sha: str) -> dict[str, str]: ...  # path -> git blob SHA (from the cache)
    def is_ancestor(self, pr: ResolvedPr, ancestor: str, sha: str) -> bool: ...
    def diff(self, pr: ResolvedPr, a: str, b: str) -> str: ...  # zero-context unified diff a..b, renames detected


class DiffParser(Protocol):
    def parse(self, diff_text: str) -> list[ChangedFile]: ...


class LanguageAnalyzer(Protocol):
    """Deep analyzer for one language family. `claims(path)` decides which files it owns."""

    name: str
    language: str

    def available(self) -> bool: ...
    def claims(self, path: str) -> bool: ...
    def analyze(self, root: Path, files: set[str], side: str) -> SideGraph: ...


class GenericAnalyzer(Protocol):
    def analyze(self, root: Path, paths: list[str], side: str, files: set[str]) -> SideGraph: ...


@dataclass
class SemanticRequest:
    """One Claude call. kind: system_map (call a, one per capability batch) | pr (call b).

    `data` is everything taken from the repository / PR / user; it is only ever placed inside the untrusted-data
    fence of the prompt. `lang` is decided statically (zh-Hant when the repo docs / PR are Chinese, else en)."""

    kind: str
    tier: str
    lang: str
    data: dict[str, Any]
    worktree: Path
    ai: AiChoice | None = None  # None: the server default (system-map calls always use it)


class SemanticAnalyzer(Protocol):
    def analyze(self, req: SemanticRequest) -> SemanticDraft: ...


@dataclass
class KnowledgeResult:
    rules: list[DocRule]
    sources: list[str]  # human-readable source names (repo doc paths, context:<name>)
    context_rules: list[str] = field(default_factory=list)  # normative lines from user-provided docs
    excerpts: list[DocExcerpt] = field(default_factory=list)  # README / docs text explaining the system
    notes: list[str] = field(default_factory=list)  # what the rule budget left out (shown in the stage message)


class KnowledgeSource(Protocol):
    # changed: the PR's changed paths (None outside a PR); topics: names of the capabilities it touches. Both only
    # select which instruction rules (CLAUDE.md, AGENTS.md, .claude/rules) fit the budget.
    def collect(self, root: Path, files: set[str], context_docs: list[ContextDoc], changed: set[str] | None = None,
                topics: list[str] = ()) -> KnowledgeResult: ...
    # machine-checkable architecture contracts (import-linter) + parse notes
    def contracts(self, root: Path, files: set[str]) -> tuple[list[ImportContract], list[str]]: ...
    # client-facing surface: .pr-explorer.yml, web app routes, packages with their exports (spec §8 outer ring)
    def surface(self, root: Path, files: set[str]) -> RepoSurface: ...


class DocumentStore(Protocol):
    """Serialize, scrub secrets, validate (schema + referential integrity), write. Raises ValueError on invalid."""

    def save_system_map(self, smap: Any, files: set[str]) -> Path: ...
    def save_overlay(self, overlay: Any, system_map: dict, files: dict[str, set[str]]) -> Path: ...
    def load_system_map(self, system_map_id: str) -> dict | None: ...
    def load_overlay(self, overlay_id: str) -> dict | None: ...


class AnalysisRepository(Protocol):
    def create(self, analysis_id: str, request: dict, stage_names: list[str], status: str = "queued",
               cache_key: str | None = None, title: str = "", repo: str = "", head_sha: str = "",
               overlay_id: str | None = None, system_map_id: str | None = None) -> None: ...
    def set_status(self, analysis_id: str, status: str, error: str | None = None, overlay_id: str | None = None,
                   system_map_id: str | None = None) -> None: ...
    def set_stage(self, analysis_id: str, name: str, status: str, message: str = "") -> None: ...
    def get(self, analysis_id: str) -> dict | None: ...
    def list(self, limit: int = 50) -> list[dict]: ...
    def index_system_map(self, system_map_id: str, cache_key: str, path: str, repo_key: str, commit_sha: str,
                         semantic_status: str) -> None: ...
    def find_system_map(self, cache_key: str) -> dict | None: ...
    def list_system_maps(self, repo_key: str, limit: int = 50) -> list[dict]: ...  # newest first
    def get_system_map(self, system_map_id: str) -> dict | None: ...
    def index_overlay(self, overlay_id: str, cache_key: str, path: str, analysis_id: str, pr: ResolvedPr,
                      system_map_id: str) -> None: ...
    def find_overlay(self, cache_key: str) -> dict | None: ...
    def get_overlay_index(self, overlay_id: str) -> dict | None: ...


class QuestionRepository(Protocol):
    """Follow-up questions about an overlay (status queued | running | done | failed; answer = gated reply JSON)."""

    def create_question(self, question_id: str, overlay_id: str, question: str, selected_id: str | None) -> dict: ...
    def update_question(self, question_id: str, status: str, answer: dict | None = None, error: str | None = None) -> None: ...
    def get_question(self, question_id: str) -> dict | None: ...
    def list_questions(self, overlay_id: str) -> list[dict]: ...  # oldest first
    def fail_unfinished_questions(self, error: str) -> int: ...  # server start: nothing from an earlier run is still running
