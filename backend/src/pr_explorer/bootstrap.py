"""Composition root: the only place that wires infrastructure into application ports."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .application.pipeline import Pipeline, PipelineConfig, PipelineDeps
from .application.use_cases import AnalysisStatus, AnalyzePr, AskQuestion, CheckFreshness, GetOverlay, GetSystemMap
from .infrastructure.analyzers.generic.diff_parser import parse_unified_diff
from .infrastructure.analyzers.generic.generic_analyzer import GenericAnalyzer
from .infrastructure.analyzers.generic.languages import detect_language
from .infrastructure.analyzers.python.graph_builder import PythonAnalyzer
from .infrastructure.analyzers.typescript.ts_bridge import TypeScriptAnalyzer
from .infrastructure.knowledge.local_docs import LocalDocsKnowledge
from .infrastructure.knowledge.mcp_config import allowed_mcp_tools
from .infrastructure.persistence.document_fs import FsDocumentStore
from .infrastructure.persistence.file_cache import CACHE_DB, DEFAULT_MAX_BYTES, FileResultCache
from .infrastructure.persistence.sqlite.analysis_repository import DB_NAME, SqliteAnalysisRepository
from .infrastructure.process.runner import CommandRunner, SubprocessRunner
from .infrastructure.security.secret_scrubber import scrub
from .infrastructure.semantic.claude_cli import ClaudeCliSemanticAnalyzer
from .infrastructure.source.git_cache import GitCache
from .infrastructure.source.providers import GitHubSourceProvider, LocalSourceProvider, SourceRouter

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def default_home() -> Path:
    return Path(os.environ.get("PR_EXPLORER_HOME") or Path.home() / ".pr-explorer").expanduser()


class _DiffParser:
    def parse(self, diff_text: str):
        return parse_unified_diff(diff_text)


@dataclass
class Container:
    home: Path
    analyze: AnalyzePr
    status: AnalysisStatus
    get_system_map: GetSystemMap
    get_overlay: GetOverlay
    freshness: CheckFreshness
    ask: AskQuestion
    frontend_dist: Path
    prune_worktrees: Callable[[], list[Path]]  # server start: drop worktrees left by earlier runs (regenerable)
    fail_unfinished_questions: Callable[[str], int]  # server start: questions an earlier run never answered


LANGUAGES = ("zh-Hant", "en", "auto")


def output_language_from_env() -> str:
    """PR_EXPLORER_LANG: language of generated texts — zh-Hant (default, matches the UI), en, or auto (follow the repo)."""
    lang = (os.environ.get("PR_EXPLORER_LANG") or "zh-Hant").strip()
    if lang not in LANGUAGES:
        raise ValueError(f"PR_EXPLORER_LANG must be one of {', '.join(LANGUAGES)}; got {lang!r}")
    return lang


EFFORTS = ("low", "medium", "high", "xhigh", "max")  # `claude --effort` levels


def claude_model_effort_from_env() -> tuple[str | None, str | None]:
    """PR_EXPLORER_CLAUDE_MODEL (an alias such as opus / sonnet, or a full model name) and PR_EXPLORER_CLAUDE_EFFORT
    (low | medium | high | xhigh | max) for every Claude call; unset = the user's own Claude settings."""
    model = (os.environ.get("PR_EXPLORER_CLAUDE_MODEL") or "").strip() or None
    effort = (os.environ.get("PR_EXPLORER_CLAUDE_EFFORT") or "").strip().lower() or None
    if model and (model.startswith("-") or any(c.isspace() for c in model)):
        raise ValueError(f"PR_EXPLORER_CLAUDE_MODEL is not a model name: {model!r}")
    if effort and effort not in EFFORTS:
        raise ValueError(f"PR_EXPLORER_CLAUDE_EFFORT must be one of {', '.join(EFFORTS)}; got {effort!r}")
    return model, effort


def incremental_from_env() -> bool:
    """PR_EXPLORER_INCREMENTAL: on (default) = per-file analysis results cached under the data home and only what a
    change can affect re-analyzed; off = every file analyzed on every run, cache neither read nor written."""
    v = (os.environ.get("PR_EXPLORER_INCREMENTAL") or "on").strip().lower()
    if v not in ("on", "off", "1", "0", "true", "false"):
        raise ValueError(f"PR_EXPLORER_INCREMENTAL must be on or off; got {v!r}")
    return v in ("on", "1", "true")


def analysis_cache_bytes() -> int:
    """PR_EXPLORER_ANALYSIS_CACHE_MB: size bound of analysis-cache.db (least recently used entries go first)."""
    mb = os.environ.get("PR_EXPLORER_ANALYSIS_CACHE_MB")
    return int(float(mb) * (1 << 20)) if mb else DEFAULT_MAX_BYTES


def build_container(
    home: Path | None = None,
    runner: CommandRunner | None = None,
    gh_runner: CommandRunner | None = None,
    claude_runner: CommandRunner | None = None,
    ts_cli: Path | None = None,
    config: PipelineConfig | None = None,
    semantic=None,
) -> Container:
    """`semantic`: a SemanticAnalyzer replacing the Claude CLI (e.g. the dry run that only writes prompts)."""
    home = home or default_home()
    home.mkdir(parents=True, exist_ok=True)
    runner = runner or SubprocessRunner()
    cache = GitCache(home, runner)
    source = SourceRouter(cache, LocalSourceProvider(cache, runner), GitHubSourceProvider(cache, gh_runner or runner))
    repo = SqliteAnalysisRepository(home / DB_NAME)
    store = FsDocumentStore(home)
    model, effort = claude_model_effort_from_env()
    semantic = semantic or ClaudeCliSemanticAnalyzer(
        claude_runner or runner, os.environ.get("PR_EXPLORER_CLAUDE_BIN", "claude"), allowed_mcp_tools(),
        mcp_config=os.environ.get("PR_EXPLORER_MCP_CONFIG") or None, log_path=home / "logs" / "semantic-calls.jsonl",
        model=model, effort=effort)
    profile = "|".join(f"{k}:{v}" for k, v in (("model", model), ("effort", effort)) if v)
    config = config or PipelineConfig(output_language=output_language_from_env(), incremental=incremental_from_env(),
                                      semantic_profile=profile)
    file_cache = FileResultCache(home / CACHE_DB, analysis_cache_bytes()) if config.incremental else None
    ts_path = ts_cli or Path(os.environ.get("PR_EXPLORER_TS_CLI") or PROJECT_ROOT / "tools/ts-analyzer/dist/cli.js")
    deps = PipelineDeps(
        source=source,
        diff_parser=_DiffParser(),
        generic=GenericAnalyzer(),
        analyzers=[PythonAnalyzer(file_cache), TypeScriptAnalyzer(runner, ts_path, cache=file_cache)],
        semantic=semantic,
        knowledge=LocalDocsKnowledge(),
        store=store,
        repo=repo,
        language_of=detect_language,
        config=config,
    )
    return Container(
        home=home,
        analyze=AnalyzePr(source, repo, Pipeline(deps)),
        status=AnalysisStatus(repo),
        get_system_map=GetSystemMap(store),
        get_overlay=GetOverlay(store),
        freshness=CheckFreshness(repo, source),
        ask=AskQuestion(repo, repo, store, source, semantic, config.language, scrub),
        frontend_dist=Path(os.environ.get("PR_EXPLORER_FRONTEND_DIST") or PROJECT_ROOT / "frontend/dist"),
        prune_worktrees=cache.prune_stale_worktrees,
        fail_unfinished_questions=repo.fail_unfinished_questions,
    )
