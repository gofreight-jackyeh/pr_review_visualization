import type { ChangeStatus, ElementKind, ImpactStatus, Relation, StepStatus } from "@/entities/artifact/types"

export const CHANGE: Record<ChangeStatus, { face: string; side: string; top: string; stroke: string; label: string }> = {
  added: { face: "#c9ecd6", side: "#8fcfa7", top: "#e4f6ea", stroke: "#2f9e5b", label: "新增" },
  modified: { face: "#fce3b0", side: "#e9bd6a", top: "#fef1d4", stroke: "#c98a14", label: "修改" },
  removed: { face: "#f6cfcd", side: "#e39a97", top: "#fbe5e4", stroke: "#cc4b48", label: "移除" },
  unchanged: { face: "#e6ebf0", side: "#c3ccd6", top: "#f3f6f9", stroke: "#8794a3", label: "未變更" },
}
/** System Overview shows a neutral skin: no change/impact information yet. */
export const NEUTRAL = { face: "#dfe9f6", side: "#b8c9e0", top: "#eef4fb", stroke: "#6f8db5", label: "" }

export const IMPACT: Record<Exclude<ImpactStatus, "none">, { label: string; short: string; color: string; dash?: string }> = {
  direct: { label: "直接修改", short: "直接", color: "#c98a14" },
  confirmed_dependency: { label: "已確認依賴", short: "依賴", color: "#2f6fdb" },
  evidence_supported: { label: "有證據的影響", short: "影響", color: "#0e8f8f" },
  potential: { label: "潛在影響", short: "潛在", color: "#8a4fd6", dash: "5 3" },
  unknown: { label: "未知", short: "未知", color: "#6b7785", dash: "2 3" },
}

export const STEP: Record<StepStatus, { label: string; color: string; bg: string }> = {
  added: { label: "新增", color: "#1b6b3c", bg: "#dff3e6" },
  removed: { label: "移除", color: "#9b2a27", bg: "#fbe3e2" },
  changed: { label: "改變", color: "#7a5200", bg: "#fdf0cf" },
  unchanged: { label: "未變", color: "#52606d", bg: "#eef1f5" },
}

export const KIND_LABEL: Record<ElementKind, string> = { capability: "業務能力", entity: "核心資料", entry_point: "入口", component: "技術元件", flow: "業務流程" }
export const RELATION_LABEL: Record<Relation["kind"], string> = { depends_on: "依賴", calls: "呼叫", uses_entity: "使用資料" }
export const EVIDENCE_STATUS_LABEL = { confirmed: "已確認", possible: "可能", unknown: "未知" } as const
export const ACTIVE = "#1d4ed8", POSSIBLE = "#ea580c"
/** Architecture-rule violations drawn on the map (spec §8 呈現). */
export const VIOLATION = "#d61f69"
