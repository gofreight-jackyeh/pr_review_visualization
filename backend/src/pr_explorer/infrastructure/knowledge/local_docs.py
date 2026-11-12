"""Repo docs (README, CONTRIBUTING, docs/**/*.md, ADRs) and instruction files (CLAUDE.md / AGENTS.md at any depth,
.claude/CLAUDE.md, .claude/rules/**/*.md) → normative statements (context for Claude, never verified rules) and excerpts
that explain the system; user-provided context docs (spec §3); import-linter contracts.

Instruction rules have their own budget and, for a PR (`changed` given), are chosen by relevance: a nested CLAUDE.md /
AGENTS.md counts only when the PR changes a file under its directory (then all its rules are kept), a .claude/rules
file with `paths:` / `globs:` front matter only when a changed path matches (then kept), and every other rule is ranked
by how many words of the changed paths and touched capability names appear in its rule file path, its headings and its
text. What the budget drops is reported in `KnowledgeResult.notes`."""

from __future__ import annotations

import fnmatch
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
MAX_RULES = 25  # README / docs statements (instruction files have their own budget below)
MAX_INSTRUCTION_RULES = 40  # ranked instruction rules
MAX_INSTRUCTION_CHARS = 12_000
MAX_SCOPED_RULES = 60  # rules scoped to the PR's changed paths: always kept, up to this hard cap
MIN_RANKED, MIN_RANKED_CHARS = 10, 3_000
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


_WORD = re.compile(r"[0-9A-Za-z]+|[㐀-鿿]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# words that say nothing about which part of the system a rule is about
_STOP = frozenset("""src lib libs app apps main index test tests spec specs py ts tsx js jsx md mdx json yml yaml txt the
and for with use uses using from this that these those must should never always rule rules claude agents readme docs doc
init utils util common core file files code new all not are you your when any each only into make sure can will have has
via per our its also more most other some such than then them they what which while who why how base head src""".split())


def topic_words(text: str) -> set[str]:
    """Lower-case words of identifiers / paths / prose (camelCase and snake_case split; 2-character pieces of CJK runs),
    without generic words; a trailing plural s is also tried without it."""
    out: set[str] = set()
    for w in _WORD.findall(_CAMEL.sub(" ", text)):
        if "㐀" <= w[0] <= "鿿":
            out |= {w[i:i + 2] for i in range(len(w) - 1)} if len(w) > 1 else set()
            continue
        w = w.lower()
        if len(w) < 3 or w in _STOP or w.isdigit():
            continue
        out.add(w)
        if len(w) > 4 and w.endswith("s"):
            out.add(w[:-1])
    return out


def front_matter_globs(text: str) -> list[str]:
    """`paths:` / `globs:` of a YAML front matter block (inline `a, b`, `["a", "b"]` or a `- item` list)."""
    lines = text.splitlines()
    if lines[:1] != ["---"]:
        return []
    out: list[str] = []
    key = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^(\w+)\s*:\s*(.*)$", line)
        if m:
            key = m.group(1).lower() in ("paths", "globs", "files", "applies_to")
            if key and m.group(2).strip():
                out += [x.strip().strip("'\"") for x in m.group(2).strip().strip("[]").split(",")]
            continue
        item = re.match(r"^\s*-\s*(.+)$", line)
        if key and item:
            out.append(item.group(1).strip().strip("'\""))
    return [g for g in out if g]


def glob_matches(path: str, pattern: str) -> bool:
    pat = pattern.strip().removeprefix("./").lstrip("/")
    return any(fnmatch.fnmatchcase(path, p) for p in {pat, pat.replace("**/", ""), pat.rstrip("/") + "/**"})


def _headings(lines: list[str]) -> list[str]:
    """For each line (0-based), the text of the heading chain above it."""
    chain: list[tuple[int, str]] = []
    out: list[str] = []
    in_code = False
    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
        h = None if in_code else re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", line)
        if h:
            level = len(h.group(1))
            chain = [c for c in chain if c[0] < level] + [(level, h.group(2))]
        out.append(" ".join(t for _, t in chain))
    return out


def _rule_file_words(path: str) -> set[str]:
    """.claude/rules/billing/refunds.md → {billing, refunds, refund}; nested CLAUDE.md → its directory names."""
    parts = path.split("/")
    parts = parts[2:] if parts[:2] == [".claude", "rules"] else parts[:-1]
    return topic_words(" ".join(parts).rsplit(".", 1)[0] if parts else "")


class LocalDocsKnowledge:
    def contracts(self, root: Path, files: set[str]) -> tuple[list[ImportContract], list[str]]:
        return read_contracts(root, files)

    def surface(self, root: Path, files: set[str]) -> RepoSurface:
        return scan_surface(root, files)

    def collect(self, root: Path, files: set[str], context_docs: list[ContextDoc], changed: set[str] | None = None,
                topics: list[str] = ()) -> KnowledgeResult:
        """`changed`: the PR's changed paths (None = no PR: every instruction file counts, shallowest first);
        `topics`: names of the capabilities the PR touches (relevance words besides the changed paths)."""
        doc_rules: list[DocRule] = []
        sources: list[str] = []
        excerpts: list[DocExcerpt] = []
        budget = MAX_EXCERPTS_TOTAL
        readable: list[str] = []
        words = set().union(*(topic_words(p) for p in (changed or ()))) | set().union(*(topic_words(t) for t in topics))
        scoped: list[DocRule] = []
        ranked: list[tuple[int, DocRule]] = []
        out_of_scope: list[str] = []
        for path in sorted((p for p in files if is_doc(p)), key=_rule_rank):
            text = read_repo_text(root, path, MAX_DOC_BYTES)
            if text is None:
                continue  # a symlinked doc could pull files from outside the repository into the Claude prompt
            scope = instruction_scope(path)
            found = extract_rules(text, everywhere=scope is not None)
            if scope is None:
                readable.append(path)
                sources.append(f"repo:{path}")
                doc_rules += [DocRule(path, line, body) for line, body in found[: max(0, MAX_RULES - len(doc_rules))]]
                continue
            globs = front_matter_globs(text)
            in_scope = None if changed is None else (
                any(c.startswith(scope) for c in changed) if scope else
                any(glob_matches(c, g) for c in changed for g in globs) if globs else None)
            if in_scope is False:
                out_of_scope.append(path)
                continue  # governs other directories / paths than this PR changes
            readable.append(path)
            sources.append(f"repo:{path}")
            if in_scope:
                scoped += [DocRule(path, line, body) for line, body in found]
                continue
            heads = _headings(text.splitlines())
            file_words = _rule_file_words(path)
            for line, body in found:
                score = (3 * len(file_words & words) + 2 * len(topic_words(heads[line - 1]) & words)
                         + len(topic_words(body) & words)) if words else 0
                ranked.append((score, DocRule(path, line, body)))
        rules = scoped[:MAX_SCOPED_RULES]
        over: list[DocRule] = scoped[MAX_SCOPED_RULES:]
        # ranked rules fill what the scoped ones leave (never fewer than MIN_RANKED: repository-wide rules matter too);
        # sorted() is stable, so equal scores keep the shallowest-scope-first file order and line order
        room = max(MAX_INSTRUCTION_RULES - len(rules), MIN_RANKED)
        char_room = max(MAX_INSTRUCTION_CHARS - sum(len(r.text) for r in rules), MIN_RANKED_CHARS)
        n = chars = 0
        for _, r in sorted(ranked, key=lambda x: -x[0]):
            if n < room and chars + len(r.text) <= char_room:
                rules.append(r)
                n, chars = n + 1, chars + len(r.text)
            else:
                over.append(r)
        notes: list[str] = []
        if out_of_scope:
            notes.append(f"instruction files outside the PR's paths not used: {len(out_of_scope)} "
                         f"({', '.join(out_of_scope[:5])}{', …' if len(out_of_scope) > 5 else ''})")
        if over:
            files_over = list(dict.fromkeys(r.path for r in over))
            notes.append(f"instruction rules over budget, least relevant dropped: {len(over)} of {len(over) + len(rules)} "
                         f"(from {', '.join(files_over[:5])}{', …' if len(files_over) > 5 else ''})")
        for path in sorted(readable, key=_excerpt_rank):
            if budget <= 0:
                break
            text = read_repo_text(root, path, MAX_DOC_BYTES)  # re-read: only the few files the budget reaches
            if text and text.strip():
                chunk = text[: min(MAX_EXCERPT_CHARS, budget)]
                budget -= len(chunk)
                excerpts.append(DocExcerpt(path, 1, len(chunk.splitlines()) or 1, chunk))
        rules = sorted(rules + doc_rules, key=lambda r: (r.path, r.line))
        context_rules: list[str] = []
        for d in context_docs:
            sources.append(f"context:{d.name}")
            context_rules += [f"{d.name}:{ln}: {body}" for ln, body in extract_rules(d.content)][:MAX_RULES]
        return KnowledgeResult(rules=rules, sources=sorted(sources, key=lambda s: (s.startswith("context:"), s)),
                               context_rules=context_rules, excerpts=excerpts, notes=notes)
