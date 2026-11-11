import type { ArchCheck, Finding, ImpactStatus, Relation, Step, Text } from "./types"

/**
 * Plan「審查重點與可信度」: where a statement comes from (SOURCE) is kept apart from how far it is established
 * (INTERPRETATION STATUS). A static source does not make something 已確認; only the item's own status fields do.
 */
export type Source = Text["source"]
export const SOURCE_LABEL: Record<Source, string> = { semantic: "AI 說明", pr_description: "PR 描述", static: "靜態分析" }
export const SOURCE_TITLE: Record<Source, string> = {
  semantic: "由 AI 根據證據撰寫，並通過證據檢查",
  pr_description: "直接取自 PR 標題與描述，未經 AI 說明",
  static: "由靜態分析得出",
}

export type Status = "confirmed" | "needs_confirmation" | "undetermined"
export const STATUS: Record<Status, { label: string; glyph: string; title: string }> = {
  confirmed: { label: "已確認的結構事實", glyph: "✓", title: "靜態分析已確認這個結構事實（不代表業務上一定沒問題）" },
  needs_confirmation: { label: "待確認", glyph: "?", title: "尚未證實，需要審查者確認" },
  undetermined: { label: "無法判定", glyph: "–", title: "現有資料無法判定" },
}

export type StatusInput =
  | { finding: Pick<Finding, "status"> }
  | { arch: Pick<ArchCheck, "status" | "verdict" | "interface_change"> }
  | { step: Pick<Step, "evidence_status"> }
  | { relation: Pick<Relation, "confidence"> }
  | { impact: ImpactStatus }

/** The one primary status of a card, from its actual status / compatibility / confidence field. */
export function statusOf(x: StatusInput): Status {
  if ("finding" in x) return x.finding.status === "verified" ? "confirmed" : "needs_confirmation"
  if ("arch" in x) {
    const a = x.arch
    if (a.interface_change?.compatibility === "unknown") return "undetermined"
    if (a.status === "needs_confirmation") return "needs_confirmation"
    return a.verdict === "unclear" ? "undetermined" : "confirmed"
  }
  if ("step" in x) return x.step.evidence_status === "confirmed" ? "confirmed" : x.step.evidence_status === "possible" ? "needs_confirmation" : "undetermined"
  if ("relation" in x) return x.relation.confidence === "confirmed" ? "confirmed" : "undetermined"
  return x.impact === "potential" ? "needs_confirmation" : x.impact === "unknown" || x.impact === "none" ? "undetermined" : "confirmed"
}
