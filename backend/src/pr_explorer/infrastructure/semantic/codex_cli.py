"""OpenAI Codex CLI adapter (`codex exec`): the same prompts, reply shapes, retries and call log as the Claude adapter.
Choosing it sends the PR's code and the analysis data to OpenAI under the operator's Codex login.

Isolation (spec §11), each point verified on codex-cli 0.167.2 / macOS on 2026-11-13 with harmless probes:
- a permission profile allows reading only the worktree (plus the system paths of `:minimal` and Codex's own packages);
  plain `--sandbox read-only` would let the model read the whole disk. No writes, no network, no approval prompts.
- the user's config, rules, hooks, plugins, apps, memories, skills, browser / computer use and web search are off, and
  no AGENTS.md is read (`project_doc_max_bytes=0`): repository text never becomes instructions; the cwd is an empty
  temp dir and the prompt arrives on stdin inside the untrusted-data fence.
- the binary is run by its real path: the sandbox cannot exec through the `~/.local/bin` symlink.
The profile keys are not documented (found in the binary), so a newer CLI may change them: re-run the probes then.
No `--output-schema`: it requires OpenAI strict schemas (every key required, no open maps) that the loose reply shapes
are not; the reply is parsed from the final message like a plain Claude reply (one retry when malformed).
"""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path

from ...application.ports import SemanticRequest
from ..process.runner import CommandRunner, RunResult
from .claude_cli import ClaudeCliSemanticAnalyzer
from .prompt_builder import system_prompt

DISABLED_FEATURES = ("apps", "plugins", "multi_agent", "image_generation", "goals", "browser_use", "computer_use",
                     "in_app_browser", "memories", "tool_suggest", "skill_search", "hooks")
REPLY_FILE = "reply.txt"
DEFAULT_EFFORT = "high"


def _toml(s: str) -> str:
    """A TOML basic string (JSON escapes are valid TOML; non-ASCII stays raw, so no surrogate escapes)."""
    return json.dumps(s, ensure_ascii=False)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def user_defaults() -> tuple[str | None, str | None]:
    """The operator's own model / effort from ~/.codex/config.toml (the user config itself is not loaded by the call)."""
    try:
        cfg = tomllib.loads((codex_home() / "config.toml").read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None, None
    model, effort = cfg.get("model"), cfg.get("model_reasoning_effort")
    return (model if isinstance(model, str) else None), (effort if isinstance(effort, str) else None)


class CodexCliSemanticAnalyzer(ClaudeCliSemanticAnalyzer):
    engine = "codex"

    def __init__(self, runner: CommandRunner, codex_bin: str = "codex", timeout: float = 600,
                 log_path: Path | None = None, model: str | None = None, effort: str | None = None):
        super().__init__(runner, codex_bin, timeout=timeout, log_path=log_path, json_schema=False, model=model,
                         effort=effort)

    def defaults(self) -> tuple[str | None, str | None]:
        """Model: PR_EXPLORER_CODEX_MODEL, else the operator's Codex config. Effort: PR_EXPLORER_CODEX_EFFORT, else
        DEFAULT_EFFORT (owner decision 2026-11-13: not the CLI config, whose xhigh makes an analysis much slower)."""
        return self.model or user_defaults()[0], self.effort or DEFAULT_EFFORT

    def env(self) -> dict[str, str]:
        return dict(os.environ)  # tool calls only see the "core" variables (shell_environment_policy)

    def codex_command(self, worktree: Path, kind: str, model: str | None, effort: str | None, cwd: Path) -> list[str]:
        wt = Path(worktree).resolve()
        found = shutil.which(self.claude_bin)
        cmd = [os.path.realpath(found) if found else self.claude_bin, "exec",
               "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check"]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={_toml(effort)}"]
        readable = {":minimal": "read", wt.as_posix(): "read", (codex_home() / "packages").as_posix(): "read"}
        cmd += [
            "-c", 'approval_policy="never"',
            "-c", 'default_permissions="pr"',
            "-c", "permissions.pr.filesystem={" + ", ".join(f"{_toml(k)}={_toml(v)}" for k, v in readable.items()) + "}",
            "-c", "permissions.pr.network.enabled=false",
            "-c", 'shell_environment_policy.inherit="core"',
            "-c", 'web_search="disabled"',
            "-c", "project_doc_max_bytes=0",
            "-c", f"developer_instructions={_toml(system_prompt(kind))}",
        ]
        for f in DISABLED_FEATURES:
            cmd += ["--disable", f]
        return cmd + ["--json", "-o", str(cwd / REPLY_FILE), "-"]

    def call(self, req: SemanticRequest, model: str | None, effort: str | None, prompt: str,
             cwd: Path) -> tuple[RunResult, str, str | None]:
        cmd = self.codex_command(req.worktree, req.kind, model, effort, cwd)
        res = self.runner.run(cmd, cwd=str(cwd), input=prompt, timeout=self.timeout, env=self.env())
        try:
            reply = (cwd / REPLY_FILE).read_text("utf-8")
        except OSError:
            reply = ""
        return res, reply, None

    def failure(self, res: RunResult) -> str:
        """The turn.failed / error event of the JSONL stream, else the CLI's ERROR line."""
        for line in reversed(res.stdout.splitlines()):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict) and ev.get("type") in ("turn.failed", "error"):
                err = ev.get("error") if isinstance(ev.get("error"), dict) else ev
                return str(err.get("message") or err)[:300]
        lines = [x for x in res.stderr.splitlines() if x.startswith("ERROR")]
        return (lines[-1] if lines else res.stderr.strip())[:300]
