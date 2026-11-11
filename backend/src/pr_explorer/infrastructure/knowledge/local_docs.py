"""Repo docs (README, CONTRIBUTING, docs/**/*.md, ADRs) → normative statements (architecture context for Claude, never
verified rules) and excerpts that explain the system; user-provided context docs (spec §3); import-linter contracts."""

from __future__ import annotations

import re
from pathlib import Path

from ...application.ports import ContextDoc, KnowledgeResult
from ...domain.model.convention import DocExcerpt, DocRule, ImportContract, RepoSurface
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
    name = path.rsplit("/", 1)[-1]
    if "node_modules/" in path or path.startswith("."):
        return False
    if DOC_NAMES.match(name) and "/" not in path:
        return True
    lower = path.lower()
    return lower.endswith((".md", ".rst")) and (lower.startswith("docs/") or "/adr/" in lower or lower.startswith("adr/")
                                                or DOC_NAMES.match(name) is not None)


def extract_rules(text: str) -> list[tuple[int, str]]:
    rules: list[tuple[int, str]] = []
    in_section = False
    in_code = False
    for i, line in enumerate(text.splitlines(), start=1):
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
        if (in_section and b) or (NORMATIVE.search(body) and (b or in_section)):
            rules.append((i, body[:400]))
    return rules


def _excerpt_rank(path: str) -> tuple:
    top = "/" not in path
    return (not (top and path.upper().startswith("README")), not top, path.count("/"), path)


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
        for path in sorted((p for p in files if is_doc(p)), key=_excerpt_rank):
            text = read_repo_text(root, path, MAX_DOC_BYTES)
            if text is None:
                continue  # a symlinked doc could pull files from outside the repository into the Claude prompt
            found = extract_rules(text)
            sources.append(f"repo:{path}")
            for line, body in found:
                if len(rules) < MAX_RULES:
                    rules.append(DocRule(path, line, body))
            if budget > 0 and text.strip():
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
