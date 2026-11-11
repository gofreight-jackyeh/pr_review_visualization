"""Rules + complexity score → semantic tier (spec §5 Layer 6, Adaptive Invocation)."""

from __future__ import annotations

from dataclasses import dataclass

from ..model.document import Decision
from ..model.changes import ChangedFile
from ..model.graph import Graph

DOC_LANGUAGES = {"markdown", "text", "restructuredtext"}
DOC_NAMES = {"LICENSE", "CHANGELOG", "AUTHORS", "CODEOWNERS", "NOTICE"}
MODES = ("auto", "off", "light", "standard", "deep")
SYMBOL_KINDS = {"function", "method", "view", "class", "component", "serializer", "form", "task"}


@dataclass
class ComplexityResult:
    score: float
    reasons: list[str]
    tier: str  # none | light | standard | deep


def _is_doc(f: ChangedFile) -> bool:
    name = f.path.rsplit("/", 1)[-1]
    return f.language in DOC_LANGUAGES or f.path.startswith("docs/") or name.split(".")[0] in DOC_NAMES


def tier_for(score: float) -> str:
    if score <= 0:
        return "none"
    if score <= 3:
        return "light"
    if score <= 11:
        return "standard"
    return "deep"


def score_change(files: list[ChangedFile], graph: Graph | None) -> ComplexityResult:
    if not files:
        return ComplexityResult(0, ["no changed files"], "none")
    if all(_is_doc(f) for f in files):
        return ComplexityResult(0, ["documentation-only change: semantic analysis skipped"], "none")

    reasons: list[str] = []
    score = 0.0
    nodes = list(graph.nodes.values()) if graph else []
    changed = [n for n in nodes if n.change_status != "unchanged" and n.kind != "external"]

    def add(points: float, reason: str) -> None:
        nonlocal score
        score += points
        reasons.append(f"+{points:g} {reason}")

    api = [n for n in changed if n.kind in {"route", "view"}]
    if api:
        add(4, f"API / entry-point change ({len(api)}: {', '.join(n.label for n in api[:3])})")
    models = [n for n in changed if n.kind == "model"]
    if models:
        add(min(4 + 2 * (len(models) - 1), 8), f"data-model change ({', '.join(n.label for n in models[:3])})")
    migrations = [f for f in files if "/migrations/" in f"/{f.path}"]
    if migrations:
        add(2, f"{len(migrations)} migration file(s) changed")
    removed = [n for n in changed if n.change_status == "removed" and n.kind in SYMBOL_KINDS | {"model", "route"}]
    if removed:
        add(min(2 * len(removed), 6), f"{len(removed)} symbol(s) removed")
    behaviour = [n for n in changed if n.kind in SYMBOL_KINDS and n.change_status in {"modified", "added"}]
    if behaviour:
        add(min(len(behaviour), 6), f"behavior change in {len(behaviour)} function(s)/class(es)")
    new_modules = [n for n in changed if n.kind == "module" and n.change_status == "added"]
    if new_modules:
        add(min(len(new_modules), 3), f"architecture: {len(new_modules)} new module(s)")
    if graph:
        def pkg(nid: str) -> str:
            ref = graph.nodes[nid].code_refs[0].path if graph.nodes[nid].code_refs else ""
            return ref.split("/", 1)[0]
        cross = [
            e for e in graph.edges.values()
            if e.change_status in {"added", "removed"} and e.kind == "imports" and pkg(e.source) != pkg(e.target)
        ]
        if cross:
            add(2, f"architecture: {len(cross)} cross-package dependency change(s)")
        dyn = [e for e in graph.edges.values() if e.change_status == "added" and e.confidence == "unknown"]
        if dyn:
            add(1, f"{len(dyn)} new dynamic (unresolved) call edge(s)")
    churn = sum(f.additions + f.deletions for f in files)
    if churn >= 100:
        add(min(churn // 100, 5), f"{churn} changed lines")
    if score == 0:
        has_structural = bool(changed) or any(f.status in {"added", "removed", "renamed"} for f in files)
        reasons.append(
            "no structural change detected (formatting-only or unanalyzed files)" if not has_structural
            else "only minor structural changes"
        )
        if has_structural:
            score = 1
    return ComplexityResult(score, reasons, tier_for(score))


def decide(mode: str, result: ComplexityResult) -> tuple[Decision, str]:
    """Returns (decision, tier). A manual mode always wins over the auto tier."""
    if mode not in MODES:
        raise ValueError(f"unknown semantic_mode {mode!r}")
    reasons = list(result.reasons)
    if mode == "off":
        reasons.append("manual override: semantic analysis off")
        return Decision(result.score, reasons, mode), "none"
    if mode == "auto":
        reasons.append(f"auto tier: {result.tier}")
        return Decision(result.score, reasons, mode), result.tier
    reasons.append(f"manual override: tier {mode} (auto would be {result.tier})")
    return Decision(result.score, reasons, mode), mode
