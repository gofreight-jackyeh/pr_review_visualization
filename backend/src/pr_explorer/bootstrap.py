"""Composition root: the only place that wires infrastructure into application ports."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .application.pipeline import Pipeline, PipelineConfig, PipelineDeps
from .application.use_cases import AnalysisStatus, AnalyzePr, CheckFreshness, GetOverlay, GetSystemMap
from .infrastructure.analyzers.generic.diff_parser import parse_unified_diff
from .infrastructure.analyzers.generic.generic_analyzer import GenericAnalyzer
from .infrastructure.analyzers.generic.languages import detect_language
from .infrastructure.analyzers.python.graph_builder import PythonAnalyzer
from .infrastructure.analyzers.typescript.ts_bridge import TypeScriptAnalyzer
from .infrastructure.knowledge.local_docs import LocalDocsKnowledge
from .infrastructure.knowledge.mcp_config import allowed_mcp_tools
from .infrastructure.persistence.document_fs import FsDocumentStore
from .infrastructure.persistence.sqlite.analysis_repository import DB_NAME, SqliteAnalysisRepository
from .infrastructure.process.runner import CommandRunner, SubprocessRunner
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
    frontend_dist: Path
    prune_worktrees: Callable[[], list[Path]]  # server start: drop worktrees left by earlier runs (regenerable)


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
    ts_path = ts_cli or Path(os.environ.get("PR_EXPLORER_TS_CLI") or PROJECT_ROOT / "tools/ts-analyzer/dist/cli.js")
    deps = PipelineDeps(
        source=source,
        diff_parser=_DiffParser(),
        generic=GenericAnalyzer(),
        analyzers=[PythonAnalyzer(), TypeScriptAnalyzer(runner, ts_path)],
        semantic=semantic or ClaudeCliSemanticAnalyzer(
            claude_runner or runner, os.environ.get("PR_EXPLORER_CLAUDE_BIN", "claude"), allowed_mcp_tools(),
            mcp_config=os.environ.get("PR_EXPLORER_MCP_CONFIG") or None, log_path=home / "logs" / "semantic-calls.jsonl"),
        knowledge=LocalDocsKnowledge(),
        store=store,
        repo=repo,
        language_of=detect_language,
        config=config or PipelineConfig(),
    )
    return Container(
        home=home,
        analyze=AnalyzePr(source, repo, Pipeline(deps)),
        status=AnalysisStatus(repo),
        get_system_map=GetSystemMap(store),
        get_overlay=GetOverlay(store),
        freshness=CheckFreshness(repo, source),
        frontend_dist=Path(os.environ.get("PR_EXPLORER_FRONTEND_DIST") or PROJECT_ROOT / "frontend/dist"),
        prune_worktrees=cache.prune_stale_worktrees,
    )
