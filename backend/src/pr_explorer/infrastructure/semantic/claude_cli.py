"""Claude Code CLI adapter (`claude -p --output-format json`), read-only tools only. Call kinds:
system_map (business names/descriptions for one capability batch), domains (assign business capabilities to domains),
phases (cut built flows into business phases) and pr (purpose, behavior changes, ...).

Failure never breaks a document: unavailable CLI → status=unavailable; an unparseable or wrongly shaped reply is
retried once (owner decision, spec §5 Layer 7), then status=failed. A non-zero exit is not retried.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from ...application.ports import SemanticRequest
from ...domain.model.semantic import SemanticDraft
from ..process.runner import CommandRunner
from ..security.secret_scrubber import scrub
from .prompt_builder import build_user_prompt, system_prompt
from .tool_policy import DISALLOWED_TOOLS, READ_ONLY_TOOLS, allowed_tools

MAX_TURNS = {"light": 4, "standard": 8, "deep": 16}
# The only reply keys that are read; anything else (nodes, edges, timelines, new groupings...) is ignored.
REPLY_KEYS = {
    "system_map": ("system_summary", "capabilities", "entities", "entry_points", "flows", "steps"),
    "domains": ("domains",),
    "phases": ("flows",),
    "pr": ("purpose", "design_rationale", "behavior_changes", "findings", "step_labels", "head_additions", "walkthrough",
           "questions", "flow_phases", "architecture_hypotheses", "interface_notes"),
}
OBJECT_KEYS = {"system_summary", "purpose", "design_rationale", "walkthrough"}  # every other reply key is a list
MAX_ATTEMPTS = 2  # one retry on a malformed reply
# Set to 1 it makes the CLI auto-load CLAUDE.md from --add-dir directories (i.e. from the PR's own tree).
CLAUDE_MD_FROM_ADDED_DIRS = "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"
log = logging.getLogger("pr_explorer.semantic")


class CallLog:
    """Debug log of every Claude call: one JSON line per attempt (kind, prompt chars, seconds, outcome) in
    `<home>/logs/semantic-calls.jsonl`, plus the `pr_explorer.semantic` logger. Never contains prompt text."""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()

    def write(self, **rec) -> None:
        log.info("claude call %s", rec)
        if self.path is None:
            return
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}, ensure_ascii=False) + "\n")
        except OSError:  # a debug log must never fail an analysis
            pass


def parse_reply(raw: str) -> dict:
    """Extract the JSON object from the CLI envelope (`{"result": "..."}`) or plain text."""
    text = raw.strip()
    try:
        env = json.loads(text)
        if isinstance(env, dict) and (env.get("is_error") or (env.get("type") == "result" and "result" not in env)):
            raise ValueError(f"claude reported an error: {str(env.get('result') or env.get('subtype'))[:200]}")
        if isinstance(env, dict) and "result" in env:
            if env.get("is_error"):
                raise ValueError(f"claude reported an error: {str(env.get('result'))[:200]}")
            text = str(env["result"]).strip()
        elif isinstance(env, dict):
            return env
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    obj = json.loads(candidate)
    if not isinstance(obj, dict):
        raise ValueError("reply is not a JSON object")
    return obj


class ClaudeCliSemanticAnalyzer:
    def __init__(self, runner: CommandRunner, claude_bin: str = "claude", mcp_tools: list[str] | None = None,
                 timeout: float = 600, mcp_config: str | None = None, log_path: Path | None = None):
        self.runner = runner
        self.calls = CallLog(log_path)
        self.claude_bin = claude_bin
        self.mcp_tools = mcp_tools or []
        self.timeout = timeout
        # JSON string or file path with the read-only MCP servers (PR_EXPLORER_MCP_CONFIG). --strict-mcp-config
        # ignores every other MCP source, so without it the allow-listed MCP tools simply are not available.
        self.mcp_config = mcp_config

    def command(self, tier: str, worktree: Path, kind: str = "pr") -> list[str]:
        """Isolation (spec §11): repo content must never become instructions, tools are read-only, PR code never runs.

        - cwd is an empty temp dir (see analyze), so the PR's .claude/settings.json hooks, CLAUDE.md and .mcp.json
          are never discovered as project config; the worktree is reachable only via --add-dir.
        - --setting-sources user: no project/local settings; --permission-mode default: a user-level bypass
          setting cannot widen permissions; Read is allow-listed only inside the worktree.
        - not --bare: it would also disable the user's OAuth/keychain auth.
        NOTE: flags verified against the locally installed CLI's `--help`; live runs only on the synthetic fixture.
        """
        wt = Path(worktree).resolve()
        mcp = [t for t in allowed_tools(self.mcp_tools) if t not in READ_ONLY_TOOLS]
        # `//abs/path` = absolute-path rule syntax. Grep/Glob are deliberately NOT allow-listed bare: Read path rules also
        # govern them, so they stay confined to the worktree (not verified with a live run).
        allowed = [f"Read(/{wt.as_posix()}/**)", *mcp]
        cmd = [
            self.claude_bin, "-p", "--output-format", "json",
            "--system-prompt", system_prompt(kind),
            "--add-dir", str(wt),
            "--setting-sources", "user",
            "--strict-mcp-config",
        ]
        if mcp and self.mcp_config:
            cmd += ["--mcp-config", self.mcp_config]
        return cmd + [
            "--tools", ",".join(READ_ONLY_TOOLS),
            "--allowedTools", ",".join(allowed),
            "--disallowedTools", ",".join(DISALLOWED_TOOLS),
            "--permission-mode", "default",
            "--no-session-persistence",
            "--max-turns", str(MAX_TURNS.get(tier, 4)),
        ]

    def analyze(self, req: SemanticRequest) -> SemanticDraft:
        sources = ["repo:code"] if req.kind != "pr" else ["pr:metadata", "pr:diff"]
        sources = list(dict.fromkeys(sources + [f"repo:{d['path']}" for d in req.data.get("docs", []) if d.get("path")]
                                     + [f"context:{d['name']}" for d in req.data.get("context_docs", []) if d.get("name")]))
        env = {k: v for k, v in os.environ.items() if k != CLAUDE_MD_FROM_ADDED_DIRS}
        problem = ""
        chars, secs = 0, 0.0
        cmd = self.command(req.tier, req.worktree, req.kind)
        # secrets in the diff / docs never leave the machine either (they are redacted like in every document)
        prompt = scrub(build_user_prompt(req))
        size = len(prompt) + len(system_prompt(req.kind))

        def out(status: str, attempt: int, error: str | None = None, payload: dict | None = None) -> SemanticDraft:
            return SemanticDraft(status=status, error=error, payload=payload or {}, knowledge_sources=sources, calls=attempt,
                                 prompt_chars=chars, seconds=round(secs, 1))

        for attempt in range(1, MAX_ATTEMPTS + 1):
            t0 = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="pr-explorer-claude-") as cwd:  # fresh, empty, removed after
                res = self.runner.run(cmd, cwd=cwd, input=prompt, timeout=self.timeout, env=env)
            took = time.monotonic() - t0
            chars, secs = chars + size, secs + took
            self.calls.write(kind=req.kind, tier=req.tier, attempt=attempt, prompt_chars=size, seconds=round(took, 1),
                             returncode=res.returncode, reply_chars=len(res.stdout))
            if res.returncode == 127:
                return out("unavailable", attempt, "claude CLI not found")
            if res.returncode != 0:
                return out("failed", attempt, f"claude exited {res.returncode}: {res.stderr.strip()[:300]}")
            try:
                obj = parse_reply(res.stdout)
                problem = malformed(obj, req.kind)
            except (ValueError, json.JSONDecodeError) as exc:
                problem = f"unparseable reply: {exc}"
            if problem:
                continue
            allowed = REPLY_KEYS.get(req.kind, ())
            unexpected = sorted(set(obj) - set(allowed))
            notes = ([f"ignored non-text keys from reply: {', '.join(unexpected[:5])}"] if unexpected else []) + \
                (["first reply malformed, retried once"] if attempt > 1 else [])
            return out("done", attempt, "; ".join(notes) or None, {k: obj[k] for k in allowed if k in obj})
        return out("failed", MAX_ATTEMPTS, f"{problem} (after {MAX_ATTEMPTS - 1} retry)"[:500])


def malformed(obj: dict, kind: str) -> str:
    """'' when every known reply key has the container type the prompt asks for; else the problem."""
    for k in REPLY_KEYS.get(kind, ()):
        if k in obj and obj[k] is not None and not isinstance(obj[k], dict if k in OBJECT_KEYS else list):
            return f"malformed reply: '{k}' is a {type(obj[k]).__name__}"
    return ""
