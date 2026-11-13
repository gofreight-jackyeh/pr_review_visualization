"""Composition root: the only place that wires infrastructure into application ports."""

from __future__ import annotations

import json
import os
import shutil
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .application.pipeline import Pipeline, PipelineConfig, PipelineDeps
from .application.use_cases import AnalysisStatus, AnalyzePr, AskQuestion, CheckFreshness, GetOverlay, GetSystemMap
from .application.use_cases.analyze_pr import validate_ai
from .application.use_cases.errors import InvalidInput
from .domain.model.semantic import EFFORTS, ENGINES, AiChoice
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
from .infrastructure.semantic.codex_cli import CodexCliSemanticAnalyzer
from .infrastructure.semantic.codex_cli import user_defaults as codex_user_defaults
from .infrastructure.semantic.router import EngineRouter
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
    ai_options: dict  # GET /api/config: engines a request may choose, their efforts / model suggestions, the default


LANGUAGES = ("zh-Hant", "en", "auto")


def output_language_from_env() -> str:
    """PR_EXPLORER_LANG: language of generated texts — zh-Hant (default, matches the UI), en, or auto (follow the repo)."""
    lang = (os.environ.get("PR_EXPLORER_LANG") or "zh-Hant").strip()
    if lang not in LANGUAGES:
        raise ValueError(f"PR_EXPLORER_LANG must be one of {', '.join(LANGUAGES)}; got {lang!r}")
    return lang


def model_effort_from_env(engine: str) -> tuple[str | None, str | None]:
    """PR_EXPLORER_<ENGINE>_MODEL (e.g. opus / sonnet, or a full model name) and PR_EXPLORER_<ENGINE>_EFFORT
    (low | medium | high | xhigh | max): that engine's default; unset = the operator's own CLI settings."""
    name = f"PR_EXPLORER_{engine.upper()}"
    model = (os.environ.get(f"{name}_MODEL") or "").strip() or None
    effort = (os.environ.get(f"{name}_EFFORT") or "").strip().lower() or None
    try:
        validate_ai(AiChoice(engine, model, effort))
    except InvalidInput as exc:
        raise ValueError(f"{name}_MODEL / {name}_EFFORT: {exc.message}") from exc
    return model, effort


def claude_model_effort_from_env() -> tuple[str | None, str | None]:
    return model_effort_from_env("claude")


def model_suggestions(engine: str) -> list[str]:
    """Names offered in the model box (any other name may still be typed): Claude's aliases; Codex's own model list
    when its CLI cached one (best effort: the file is Codex's, its format may change)."""
    if engine == "claude":
        return ["opus", "sonnet", "haiku"]
    try:
        cached = json.loads((Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json").read_text())
        models = cached.get("models") if isinstance(cached, dict) else None
        return [m["slug"] for m in models or [] if isinstance(m, dict) and isinstance(m.get("slug"), str)
                and m.get("visibility") == "list"][:20]
    except (OSError, ValueError, AttributeError):
        return []


def cli_defaults(engine: str) -> dict[str, str]:
    """What「引擎預設」means for this engine: the server's PR_EXPLORER_<ENGINE>_* setting, else the CLI user config
    (~/.claude/settings.json model / effortLevel, ~/.codex/config.toml); best effort, shown as a hint only."""
    model, effort = model_effort_from_env(engine)
    if engine == "codex":
        um, ue = codex_user_defaults()
    else:
        try:
            s = json.loads((Path.home() / ".claude" / "settings.json").read_text())
            um, ue = s.get("model"), s.get("effortLevel")
        except (OSError, ValueError, AttributeError):
            um, ue = None, None
    out = {"model": model or um, "effort": effort or ue}
    return {k: v for k, v in out.items() if isinstance(v, str) and v}


def engines_from_env(installed: Callable[[str], bool]) -> tuple[tuple[str, ...], AiChoice]:
    """PR_EXPLORER_ENGINES: engines a request may choose (default: claude, plus codex when its CLI is installed —
    choosing codex sends the PR's code to OpenAI). PR_EXPLORER_ENGINE: the default one (claude)."""
    raw = os.environ.get("PR_EXPLORER_ENGINES")
    names = [n.strip().lower() for n in raw.split(",") if n.strip()] if raw else \
        ["claude"] + (["codex"] if installed(os.environ.get("PR_EXPLORER_CODEX_BIN", "codex")) else [])
    bad = [n for n in names if n not in ENGINES]
    if bad or not names:
        raise ValueError(f"PR_EXPLORER_ENGINES must list some of {', '.join(ENGINES)}; got {raw!r}")
    default = (os.environ.get("PR_EXPLORER_ENGINE") or names[0]).strip().lower()
    if default not in names:
        raise ValueError(f"PR_EXPLORER_ENGINE={default!r} is not in PR_EXPLORER_ENGINES ({', '.join(names)})")
    return tuple(dict.fromkeys(names)), AiChoice(default, *model_effort_from_env(default))


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
    engines, ai_default = engines_from_env(lambda b: shutil.which(b) is not None)
    if semantic is None:
        log = home / "logs" / "semantic-calls.jsonl"
        built: dict = {
            "claude": lambda: ClaudeCliSemanticAnalyzer(
                claude_runner or runner, os.environ.get("PR_EXPLORER_CLAUDE_BIN", "claude"), allowed_mcp_tools(),
                mcp_config=os.environ.get("PR_EXPLORER_MCP_CONFIG") or None, log_path=log,
                model=model_effort_from_env("claude")[0], effort=model_effort_from_env("claude")[1]),
            "codex": lambda: CodexCliSemanticAnalyzer(
                claude_runner or runner, os.environ.get("PR_EXPLORER_CODEX_BIN", "codex"), log_path=log,
                model=model_effort_from_env("codex")[0], effort=model_effort_from_env("codex")[1]),
        }
        semantic = EngineRouter({e: built[e]() for e in engines}, ai_default)
    else:
        engines = ENGINES  # an injected analyzer (tests, dry run) stands in for every engine
    config = config or PipelineConfig(output_language=output_language_from_env(), incremental=incremental_from_env(),
                                      ai_default=ai_default, ai_engines=engines)
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
        ai_options={
            "default": {k: v for k, v in dataclasses.asdict(config.ai_default).items() if v},
            "engines": [{"engine": e, "efforts": list(EFFORTS), "models": model_suggestions(e), "default": cli_defaults(e)}
                        for e in config.ai_engines],
        },
    )
