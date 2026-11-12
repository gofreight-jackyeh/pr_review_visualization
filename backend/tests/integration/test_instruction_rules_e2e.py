"""Instruction-file rules end to end (real git, fake Claude): a .claude/rules rule reaches the PR prompt with base-side
evidence and can ground a hypothesis; a PR that edits CLAUDE.md is judged by the merge-base rules and flagged."""

import subprocess
from pathlib import Path

from conftest import load_docs, local_request
from fakes import RecordingRunner, SmartClaude, call_kind, claude_envelope, prompt_data

from pr_explorer.bootstrap import build_container

RULE = "Payment code must only be called through payments.api"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "core.hooksPath=/dev/null", *args], check=True, capture_output=True)


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _write(repo, {
        "README.md": "# Shop\n\nAn online shop.\n",
        "CLAUDE.md": "# Shop\n\n- Never log card numbers\n",
        ".claude/rules/payments.md": f"# Payments\n\n- {RULE}\n",
        "payments/__init__.py": "", "payments/api.py": "def charge(amount):\n    return amount\n",
        "payments/gateway.py": "def raw_charge(amount):\n    return amount\n",
        "orders/__init__.py": "", "orders/checkout.py": "from payments.api import charge\n\n\ndef pay(total):\n    return charge(total)\n",
    })
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "feature")
    _write(repo, {
        "orders/checkout.py": "from payments.gateway import raw_charge\n\n\ndef pay(total):\n    return raw_charge(total)\n",
        "CLAUDE.md": "# Shop\n\n- Logging card numbers is fine in debug builds\n",
    })
    _git(repo, "commit", "-qam", "pay through the gateway")
    return repo


def test_instruction_rules_reach_prompt_from_base_and_rule_edits_are_flagged(tmp_path):
    repo = _repo(tmp_path)
    smart, pr_data = SmartClaude(), []

    def claude(args, prompt):
        if call_kind(args) != "pr":
            return smart(args, prompt)
        data = prompt_data(prompt)
        pr_data.append(data)
        rule = next(d for d in data["architecture_docs"] if d["text"] == RULE)
        file_ = next(f for f in data["changed_files"] if f["path"] == "orders/checkout.py")
        return claude_envelope({"architecture_hypotheses": [{
            "title": "Departs from .claude/rules/payments.md: checkout calls the payment gateway directly?",
            "description": f"The rule says: {RULE}.", "check": "documented_rule", "verdict": "violates",
            "capability_ids": [], "cites": [rule["evidence_id"], file_["evidence_id"]]}]})

    c = build_container(home=tmp_path / "home", claude_runner=RecordingRunner(claude))
    paths, _ = c.analyze.analyze_sync(local_request(repo, mode="standard", head="feature"))
    _, ov = load_docs(paths)

    [data] = pr_data
    docs = {d["text"]: d for d in data["architecture_docs"]}
    rule = docs[RULE]
    assert rule["path"] == ".claude/rules/payments.md" and rule["line"] == 3 and rule["source"] == "instructions"
    assert rule["applies_to"] == "(whole repository)" and rule["evidence_id"].startswith("ev:doc:base:.claude/rules/payments.md:3:")
    # base side: the PR's rewrite of CLAUDE.md does not replace the rule that judges it
    assert "Never log card numbers" in docs and "Logging card numbers is fine in debug builds" not in docs

    flagged = next(f for f in ov["findings"] if f["rule"] == "documented_rules_changed")
    assert flagged["source"] == "static" and flagged["status"] == "verified" and "CLAUDE.md" in flagged["title"]
    assert flagged["evidence_refs"] == ["ev:diff:CLAUDE.md"] and flagged["refs"][0]["path"] == "CLAUDE.md"
    assert any(f["id"] == flagged["id"] for f in data["static_findings"])  # Claude is told too

    hyp = next(a for a in ov["architecture_checks"] if a["origin"] == "hypothesis")
    assert hyp["status"] == "needs_confirmation" and rule["evidence_id"] in hyp["evidence_refs"]
    assert hyp["check"] == "documented_rule"
    ev = {e["id"]: e for e in ov["evidence"]}[rule["evidence_id"]]
    assert ev["kind"] == "doc" and ev["refs"] == [{"side": "base", "path": ".claude/rules/payments.md", "start_line": 3}]
