"""Instruction files (CLAUDE.md, AGENTS.md, .claude/rules) are documented-rule sources, read like any repo doc."""

from pr_explorer.domain.model.convention import instruction_scope
from pr_explorer.infrastructure.knowledge.local_docs import LocalDocsKnowledge, is_doc


def test_instruction_paths_and_scope():
    assert instruction_scope("CLAUDE.md") == "" and instruction_scope("AGENTS.md") == ""
    assert instruction_scope("backend/api/CLAUDE.md") == "backend/api/"
    assert instruction_scope(".claude/CLAUDE.md") == "" and instruction_scope(".claude/rules/db/migrations.md") == ""
    for other in (".github/AGENTS.md", ".claude/settings.json", ".claude/agents/x.md", ".claude/rules/.hidden/x.md",
                  "web/node_modules/pkg/CLAUDE.md", "docs/claude.md", ".cursor/rules/x.md", "a/.claude/rules/x.md"):
        assert instruction_scope(other) is None, other
    assert is_doc(".claude/rules/x.md") and is_doc("svc/AGENTS.md")
    assert not is_doc(".github/workflows/ci.md") and not is_doc(".cursor/rules/x.md")


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_rules_extracted_regardless_of_heading_with_file_line(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("- Leaked rule from outside the repository root\n")
    repo = tmp_path / "repo"
    _write(repo, "README.md", "# Shop\n\nAn online shop.\n")
    _write(repo, "CLAUDE.md", "# Shop\n\n## Overview\n- Every write goes through orders.services\n")
    _write(repo, "backend/CLAUDE.md", "# Backend\n\nNever import views from tasks.\n")
    _write(repo, "AGENTS.md", "# Agents\n\n1. Add a regression test for every bug fix\n")
    _write(repo, ".claude/rules/api.md", "---\npaths:\n  - api/**/*.py\n---\n# API\n\n- Public endpoints need a changelog entry\n")
    _write(repo, ".github/AGENTS.md", "- A rule hidden in another dot directory\n")
    (repo / ".claude/rules/evil.md").symlink_to(outside)
    files = {"README.md", "CLAUDE.md", "backend/CLAUDE.md", "AGENTS.md", ".claude/rules/api.md", ".claude/rules/evil.md",
             ".github/AGENTS.md"}
    k = LocalDocsKnowledge().collect(repo, files, [])
    got = {(r.path, r.line, r.text) for r in k.rules}
    assert got == {
        ("CLAUDE.md", 4, "Every write goes through orders.services"),  # "Overview" is no convention heading
        ("backend/CLAUDE.md", 3, "Never import views from tasks."),  # prose line, nested file keeps its path (scope)
        ("AGENTS.md", 3, "Add a regression test for every bug fix"),
        (".claude/rules/api.md", 7, "Public endpoints need a changelog entry"),  # front matter is not a rule
    }
    assert "repo:.github/AGENTS.md" not in k.sources and "repo:.claude/rules/evil.md" not in k.sources  # symlink refused
    # excerpts: README first (overview, language), then instruction files ahead of generic docs
    assert [e.path for e in k.excerpts][:2] == ["README.md", ".claude/rules/api.md"]
    assert "Leaked rule" not in repr(k)
