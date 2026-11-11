"""Pre-generated guided walkthrough in spec v2 §9 order (pure).

1 what the system does → 2 its capabilities → 3 flows this PR touches → 4 behavior changes → 5 what else may be
affected → 6 what needs confirmation. Static bodies are assembled from the documents; Claude's grounded bodies
(already gated) replace them. Questions outside this set are answered "insufficient data" by the UI.
"""

from __future__ import annotations

from typing import Callable

from ..model.document import (
    BehaviorChange, FlowChange, Impact, Text, WalkQuestion, WalkStep, WalkTarget, Walkthrough,
)
from ..model.finding import Finding

TITLES = {
    "en": {"system": "What this system does", "capabilities": "The business capabilities",
           "flows": "Business flows this PR touches", "behavior": "What behavior changes",
           "impact": "What else may be affected", "questions": "What needs confirmation"},
    "zh-Hant": {"system": "這個系統在做什麼", "capabilities": "系統由哪些業務能力組成", "flows": "這個 PR 相關的業務流程",
                "behavior": "這個 PR 改變了什麼業務行為", "impact": "可能受影響的其他業務功能", "questions": "需要確認的問題"},
}
QUESTIONS = {
    "en": {"purpose": "Why is this change needed?", "flows": "Which business flows does this PR change?",
           "impact": "What else might be affected?", "findings": "What should a reviewer double-check?"},
    "zh-Hant": {"purpose": "為什麼需要這次修改？", "flows": "這個 PR 改了哪些業務流程？", "impact": "還有哪些業務功能可能受影響？",
                "findings": "審查時需要確認什麼？"},
}
NONE = {"en": "none", "zh-Hant": "無"}


def _join(items: list[str], lang: str, limit: int = 8) -> str:
    sep = "、" if lang == "zh-Hant" else ", "
    more = (f" 等 {len(items)} 項" if lang == "zh-Hant" else f" and {len(items) - limit} more") if len(items) > limit else ""
    return sep.join(items[:limit]) + more if items else NONE[lang]


def build_walkthrough(
    *, lang: str, system_name: str, system_summary: Text | None, capability_ids: list[str], purpose: Text,
    behavior_changes: list[BehaviorChange], impacts: list[Impact], flow_changes: list[FlowChange], findings: list[Finding],
    name: Callable[[str], str], evidence_of: Callable[[str], list[str]], fallback_ev: list[str],
    semantic: dict[str, Text] | None = None, semantic_questions: list[dict] | None = None,
) -> Walkthrough:
    lang = lang if lang in TITLES else "en"
    zh = lang == "zh-Hant"
    sem = semantic or {}
    T = TITLES[lang]

    def ev(ids: list[str]) -> list[str]:
        out = list(dict.fromkeys(e for i in ids for e in evidence_of(i)))[:6]
        return out or list(fallback_ev)

    changed_caps = [i.target_id for i in impacts if i.target_kind == "capability" and i.impact_status == "direct"]
    impacted_caps = [i.target_id for i in impacts if i.target_kind == "capability" and i.impact_status != "direct"]
    flow_ids = [fc.flow_id for fc in flow_changes]
    steps: list[WalkStep] = []

    body = sem.get("system") or system_summary or Text(
        (f"{system_name}：由 {len(capability_ids)} 個業務能力組成（業務說明不可用，以下為技術名稱）。" if zh else
         f"{system_name}: {len(capability_ids)} capabilities (business explanation unavailable; technical names shown)."),
        "static", ev(capability_ids[:6]))
    steps.append(WalkStep("wt:system", T["system"], body, WalkTarget("system")))

    body = sem.get("capabilities") or Text(
        ("業務能力：" if zh else "Capabilities: ") + _join([name(c) for c in capability_ids], lang, 12), "static",
        ev(capability_ids[:6]))
    steps.append(WalkStep("wt:capabilities", T["capabilities"], body, WalkTarget("system", capability_ids=capability_ids[:30])))

    if flow_changes:
        status_zh = {"added": "新增", "removed": "移除", "modified": "修改"}
        items = [f"{name(fc.flow_id)}（{status_zh[fc.change_status]}）" if zh else f"{name(fc.flow_id)} ({fc.change_status})"
                 for fc in flow_changes]
        body = sem.get("flows") or Text(("這個 PR 改變了這些業務流程：" if zh else "This PR changes these flows: ")
                                        + _join(items, lang), "static", ev(flow_ids[:6]))
        steps.append(WalkStep("wt:flows", T["flows"], body, WalkTarget("flow", flow_id=flow_ids[0], capability_ids=changed_caps[:30] or None)))

    if behavior_changes or purpose:
        body = sem.get("behavior") or (behavior_changes[0].after if behavior_changes else purpose)
        target = WalkTarget("overlay", capability_ids=changed_caps[:30] or None,
                            flow_id=(behavior_changes[0].flow_ids[0] if behavior_changes and behavior_changes[0].flow_ids else None))
        steps.append(WalkStep("wt:behavior", T["behavior"], body, target))

    body = sem.get("impact") or Text(
        ("直接修改：" if zh else "Changed directly: ") + _join([name(c) for c in changed_caps], lang)
        + ("；可能受影響：" if zh else "; possibly affected: ") + _join([name(c) for c in impacted_caps], lang),
        "static", ev((changed_caps + impacted_caps)[:6]))
    steps.append(WalkStep("wt:impact", T["impact"], body, WalkTarget("overlay", capability_ids=(changed_caps + impacted_caps)[:30] or None)))

    if findings:
        body = sem.get("questions") or Text(
            (f"{len(findings)} 個待確認事項：" if zh else f"{len(findings)} item(s) to confirm: ")
            + _join([f.title for f in findings], lang, 4), "static",
            list(dict.fromkeys(e for f in findings[:4] for e in f.evidence_refs[:1])))
        steps.append(WalkStep("wt:questions", T["questions"], body, WalkTarget("findings", finding_id=findings[0].id)))

    Q = QUESTIONS[lang]
    questions = [WalkQuestion("q:purpose", Q["purpose"], purpose, WalkTarget("overlay", capability_ids=changed_caps[:30] or None))]
    if flow_changes:
        questions.append(WalkQuestion("q:flows", Q["flows"], steps[2].body if not sem.get("flows") else sem["flows"],
                                      WalkTarget("flow", flow_id=flow_ids[0])))
    questions.append(WalkQuestion("q:impact", Q["impact"], next(s.body for s in steps if s.id == "wt:impact"),
                                  WalkTarget("overlay", capability_ids=(changed_caps + impacted_caps)[:30] or None)))
    if findings:
        questions.append(WalkQuestion("q:findings", Q["findings"], steps[-1].body, WalkTarget("findings", finding_id=findings[0].id)))
    for i, q in enumerate(semantic_questions or []):
        target = WalkTarget("flow", flow_id=q["flow_id"]) if q.get("flow_id") else WalkTarget("overlay", capability_ids=q.get("capability_ids") or None)
        questions.append(WalkQuestion(f"q:semantic:{i}", q["question"], q["answer"], target))
    return Walkthrough(steps, questions)
