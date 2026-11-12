"""Repo docs (README, CONTRIBUTING, docs/**/*.md, ADRs) and instruction files (CLAUDE.md / AGENTS.md at any depth,
.claude/CLAUDE.md, .claude/rules/**/*.md) → normative statements (context for Claude, never verified rules) and excerpts
that explain the system; user-provided context docs (spec §3); import-linter contracts."""

from __future__ import annotations

import re
from pathlib import Path

from ...application.ports import ContextDoc, KnowledgeResult
from ...domain.model.convention import DocExcerpt, DocRule, ImportContract, RepoSurface, instruction_scope
from ..security.repo_files import read_repo_text
from .import_linter import read_contracts
from .repo_surface import scan_surface

DOC_NAMES = re.compile(r"^(README|CONTRIBUTING|CONVENTIONS|STYLEGUIDE|ARCHITECTURE)(\.[\w]+)?$", re.I)
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
CONVENTION_HEADING = re.compile(r"convention|guideline|rule|standard|style|practice|pattern|contribut", re.I)
NORMATIVE = re.compile(r"\b(must|should|always|never|do not|don't|required?|prefer)\b", re.I)
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
MAX_RULES = 25
MAX_DOC_BYTES = 200_000
MAX_EXCERPT_CHARS = 6_000  # per document
MAX_EXCERPTS_TOTAL = 16_000


def is_doc(path: str) -> bool:
    if instruction_scope(path) is not None:
        return True
    name = path.rsplit("/", 1)[-1]
    if "node_modules/" in path or path.startswith("."):
        return False
    if DOC_NAMES.match(name) and "/" not in path:
        return True
    lower = path.lower()
    return lower.endswith((".md", ".rst")) and (lower.startswith("docs/") or "/adr/" in lower or lower.startswith("adr/")
                                                or DOC_NAMES.match(name) is not None)


def extract_rules(text: str, everywhere: bool = False) -> list[tuple[int, str]]:
    """Normative lines under convention-like headings (or any bullet saying must / should / never …). `everywhere`
    (instruction files, which are rules throughout): every bullet / numbered item, and any normative prose line."""
    rules: list[tuple[int, str]] = []
    in_section = False
    in_code = False
    lines = text.splitlines()
    # YAML front matter (.claude/rules files may carry `paths:` lists): metadata, not rules
    front = next((n for n in range(1, len(lines)) if lines[n].strip() == "---"), 0) + 1 if lines[:1] == ["---"] else 0
    for i, line in enumerate(lines, start=1):
        if i <= front:
            continue
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        h = HEADING.match(line)
        if h:
            in_section = bool(CONVENTION_HEADING.search(h.group(1)))
            continue
        b = BULLET.match(line)
        body = (b.group(1) if b else line).strip()
        if len(body) < 12:
            continue
        if everywhere and not b and body.startswith(("|", "@", "<", ">")):
            continue  # table rows, @imports, HTML / quotes: not a statement of intent
        if (in_section and b) or (NORMATIVE.search(body) and (b or in_section or everywhere)) or (everywhere and b):
            rules.append((i, body[:400]))
    return rules


def _depth(path: str) -> int:
    scope = instruction_scope(path)
    return path.count("/") if scope is None else scope.count("/")  # .claude/rules/x.md governs the root


def _rule_rank(path: str) -> tuple:
    """Rule budget: instruction files first (they are written intent), shallowest scope first; then README, docs."""
    readme = "/" not in path and path.upper().startswith("README")
    return (instruction_scope(path) is None, not readme, _depth(path), path)


def _excerpt_rank(path: str) -> tuple:
    """Excerpt budget: the root README first (system overview; the output language is detected from the first
    excerpts), then instruction files ahead of generic docs."""
    readme = "/" not in path and path.upper().startswith("README")
    return (not readme, instruction_scope(path) is None, _depth(path), path)


class LocalDocsKnowledge:
    def contracts(self, root: Path, files: set[str]) -> tuple[list[ImportContract], list[str]]:
        return read_contracts(root, files)

    def surface(self, root: Path, files: set[str]) -> RepoSurface:
        return scan_surface(root, files)

    def collect(self, root: Path, files: set[str], context_docs: list[ContextDoc]) -> KnowledgeResult:
        rules: list[DocRule] = []
        sources: list[str] = []
        excerpts: list[DocExcerpt] = []
        budget = MAX_EXCERPTS_TOTAL
        readable: list[str] = []
        for path in sorted((p for p in files if is_doc(p)), key=_rule_rank):
            text = read_repo_text(root, path, MAX_DOC_BYTES)
            if text is None:
                continue  # a symlinked doc could pull files from outside the repository into the Claude prompt
            readable.append(path)
            sources.append(f"repo:{path}")
            for line, body in extract_rules(text, everywhere=instruction_scope(path) is not None):
                if len(rules) < MAX_RULES:
                    rules.append(DocRule(path, line, body))
        for path in sorted(readable, key=_excerpt_rank):
            if budget <= 0:
                break
            text = read_repo_text(root, path, MAX_DOC_BYTES)  # re-read: only the few files the budget reaches
            if text and text.strip():
                chunk = text[: min(MAX_EXCERPT_CHARS, budget)]
                budget -= len(chunk)
                excerpts.append(DocExcerpt(path, 1, len(chunk.splitlines()) or 1, chunk))
        rules.sort(key=lambda r: (r.path, r.line))
        context_rules: list[str] = []
        for d in context_docs:
            sources.append(f"context:{d.name}")
            context_rules += [f"{d.name}:{ln}: {body}" for ln, body in extract_rules(d.content)][:MAX_RULES]
        return KnowledgeResult(rules=rules, sources=sorted(sources, key=lambda s: (s.startswith("context:"), s)),
                               context_rules=context_rules, excerpts=excerpts)
