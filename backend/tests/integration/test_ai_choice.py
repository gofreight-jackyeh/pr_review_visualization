"""Choosing the AI per analysis: engine / model / effort reach the right CLI, the system map keeps the server default,
results are cached per choice, and the overlay (and its questions) record which AI wrote them."""

import json
import os
from pathlib import Path

import pytest
from conftest import load_docs, local_request
from fakes import RecordingRunner, SmartClaude

from pr_explorer.application.use_cases.errors import InvalidInput
from pr_explorer.bootstrap import build_container, engines_from_env
from pr_explorer.domain.model.semantic import AiChoice
from pr_explorer.infrastructure.process.runner import RunResult
from pr_explorer.infrastructure.semantic.codex_cli import CodexCliSemanticAnalyzer, user_defaults


class TwoEngines:
    """Claude calls answered by SmartClaude; codex calls too, translated: the system prompt is the codex developer
    instructions and the reply goes to the -o file."""

    def __init__(self):
        self.smart = SmartClaude()
        self.log: list[tuple[str, str]] = []  # (engine, kind)

    def __call__(self, args, prompt):
        if args[1] == "exec":
            sp = next(a for a in args if a.startswith("developer_instructions="))[len("developer_instructions="):]
            claude_like = ["claude", "--system-prompt", json.loads(sp)]
            env = json.loads(self.smart(claude_like, prompt))
            Path(args[args.index("-o") + 1]).write_text(env["result"])
            self.log.append(("codex", self.smart.calls[-1]))
            return RunResult(0, '{"type":"turn.completed"}\n', "")
        out = self.smart(args, prompt)
        self.log.append(("claude", self.smart.calls[-1]))
        return out


@pytest.fixture()
def both(monkeypatch):
    monkeypatch.setenv("PR_EXPLORER_ENGINES", "claude,codex")
    for k in ("PR_EXPLORER_ENGINE", "PR_EXPLORER_CLAUDE_MODEL", "PR_EXPLORER_CLAUDE_EFFORT", "PR_EXPLORER_CODEX_MODEL",
              "PR_EXPLORER_CODEX_EFFORT"):
        monkeypatch.delenv(k, raising=False)


def test_codex_writes_the_pr_texts_while_claude_keeps_the_map(shop_repo, home, both, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))  # no user config: no default model / effort
    fake = TwoEngines()
    c = build_container(home=home, claude_runner=RecordingRunner(fake))
    paths, rec = c.analyze.analyze_sync(local_request(shop_repo, mode="standard",
                                                     ai=AiChoice("codex", "gpt-x", "medium")))
    _, ov = load_docs(paths)
    assert {k for e, k in fake.log if e == "codex"} == {"pr"}
    assert "pr" not in {k for e, k in fake.log if e == "claude"} and "system_map" in {k for e, k in fake.log}
    assert ov["semantic"]["status"] == "done" and ov["semantic"]["ai"] == {"engine": "codex", "model": "gpt-x", "effort": "medium"}
    # the default run is a different overlay on the same system map
    _, rec2 = c.analyze.analyze_sync(local_request(shop_repo, mode="standard"))
    assert rec2["overlay_id"] != rec["overlay_id"] and rec2["system_map_id"] == rec["system_map_id"]
    # regenerate repeats the stored choice
    aid, _, _ = c.analyze.regenerate(rec["overlay_id"])
    assert c.analyze._pending[aid][0].ai == AiChoice("codex", "gpt-x", "medium")


def test_disabled_engine_and_bad_names_are_rejected(shop_repo, home, monkeypatch):
    monkeypatch.setenv("PR_EXPLORER_ENGINES", "claude")
    c = build_container(home=home, claude_runner=RecordingRunner(lambda a, i: ""))
    with pytest.raises(InvalidInput):
        c.analyze.start(local_request(shop_repo, ai=AiChoice("codex")))
    with pytest.raises(InvalidInput):
        c.analyze.start(local_request(shop_repo, ai=AiChoice("claude", "--dangerously-skip-permissions")))
    with pytest.raises(InvalidInput):
        c.analyze.start(local_request(shop_repo, ai=AiChoice("claude", effort="turbo")))
    assert [e["engine"] for e in c.ai_options["engines"]] == ["claude"]


def test_engines_default_to_installed_clis(monkeypatch):
    monkeypatch.delenv("PR_EXPLORER_ENGINES", raising=False)
    monkeypatch.delenv("PR_EXPLORER_ENGINE", raising=False)
    assert engines_from_env(lambda b: False)[0] == ("claude",)
    assert engines_from_env(lambda b: True)[0] == ("claude", "codex")
    monkeypatch.setenv("PR_EXPLORER_ENGINE", "codex")
    monkeypatch.setenv("PR_EXPLORER_CODEX_EFFORT", "low")
    assert engines_from_env(lambda b: True)[1] == AiChoice("codex", None, "low")
    with pytest.raises(ValueError):
        engines_from_env(lambda b: False)  # default engine not enabled


def test_codex_command_is_confined(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-user"\nmodel_reasoning_effort = "xhigh"\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert user_defaults() == ("gpt-user", "xhigh")
    wt = tmp_path / "wt"
    wt.mkdir()

    def answer(args, prompt):
        Path(args[args.index("-o") + 1]).write_text('{"purpose": {"text": "p", "cites": ["pr"]}}')
        return RunResult(0, "", "")

    runner = RecordingRunner(answer)
    from pr_explorer.application.ports import SemanticRequest
    d = CodexCliSemanticAnalyzer(runner, "codex-not-on-path").analyze(SemanticRequest("pr", "standard", "en", {"pr": {"title": "t"}}, wt))
    argv = runner.calls[0]["args"]
    assert d.status == "done" and d.payload["purpose"]["text"] == "p" and d.model == "gpt-user"
    assert argv[1:3] == ["exec", "--ignore-user-config"] and argv[-1] == "-" and "--ephemeral" in argv
    assert argv[argv.index("-m") + 1] == "gpt-user" and 'model_reasoning_effort="xhigh"' in argv
    fs = next(a for a in argv if a.startswith("permissions.pr.filesystem="))
    assert f'"{wt.resolve().as_posix()}"="read"' in fs and "write" not in fs
    assert "permissions.pr.network.enabled=false" in argv and "project_doc_max_bytes=0" in argv
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert {"browser_use", "computer_use", "apps", "plugins", "hooks", "memories"} <= disabled
    assert "--add-dir" not in argv and "--dangerously-bypass-approvals-and-sandbox" not in argv  # --add-dir grants writes
    assert runner.calls[0]["cwd"] != str(wt) and not os.path.exists(runner.calls[0]["cwd"])  # empty temp cwd, removed


def test_codex_failure_message_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    ev = json.dumps({"type": "turn.failed", "error": {"message": "model not supported"}})
    runner = RecordingRunner(lambda a, i: RunResult(1, ev + "\n", "ERROR: boom"))
    from pr_explorer.application.ports import SemanticRequest
    d = CodexCliSemanticAnalyzer(runner, "codex-not-on-path").analyze(SemanticRequest("pr", "light", "en", {}, tmp_path))
    assert d.status == "failed" and "model not supported" in d.error
