/**
 * Plan「分析狀態與可用路徑」: which analysis states apply to a loaded result. Pure: derived only from the documents'
 * own status fields (semantic.status / error / decision, flow_changes, coverage). 「PR 已更新」 needs the freshness API
 * and 「完全失敗」 has no document at all, so those two live in the app shell and the input page.
 */
import { flowList, type Model } from "@/entities/artifact/model"
import { isPending } from "@/shared/api/client"

export type StateKey = "pending" | "unavailable" | "partial" | "no_flows"

export function analysisStates(m: Model): StateKey[] {
  const ov = m.overlay
  const docs = [m.map, ...(ov ? [ov] : [])]
  const out: StateKey[] = []
  if (docs.some((d) => isPending(d))) out.push("pending")
  else if (docs.some((d) => d.semantic.status === "failed")) out.push("partial")
  else if (docs.some((d) => d.semantic.status === "skipped" || d.semantic.status === "unavailable")) out.push("unavailable")
  if (ov && !hasFlows(m)) out.push("no_flows")
  return out
}

/** Flows this PR touches (flow_changes, impacted or head-only flows). */
export const relatedFlows = (m: Model) => flowList(m).filter((i) => i.related)
export const hasFlows = (m: Model) => relatedFlows(m).length > 0

/** Why Claude was not used, from the document's own decision (e.g. 「semantic_mode=off」). */
export function unavailableReason(m: Model): string | null {
  const d = (m.overlay ?? m.map).semantic
  return d.decision?.reasons.length ? d.decision.reasons.join("、") : null
}

export function failureOf(m: Model): { what: string; error?: string } | null {
  const failed = [m.map, ...(m.overlay ? [m.overlay] : [])].filter((d) => d.semantic.status === "failed")
  if (!failed.length) return null
  const what = failed.map((d) => (d.kind === "system_map" ? "系統與功能的業務說明" : "這次修改的行為差異與 AI 疑點")).join("、")
  return { what, error: failed.map((d) => d.semantic.error).find(Boolean) }
}

/** Languages the analyzer could not read: listed as an analysis gap, never as 「沒問題」. */
export function coverageGaps(m: Model): string[] {
  return (m.overlay?.coverage ?? m.map.coverage).filter((c) => c.level === "unsupported").map((c) => `${c.language}（${c.file_count} 個檔案）`)
}

/** 「本次分析未發現」 always carries its scope and what is still missing. */
export function scopeLine(m: Model): string {
  const ov = m.overlay
  const parts = [ov ? `分析範圍：這個 PR 修改的 ${ov.changes.files.length} 個檔案` : `分析範圍：${m.map.repo.name} 的系統地圖`]
  const gaps = coverageGaps(m)
  if (gaps.length) parts.push(`未支援分析：${gaps.join("、")}`)
  const st = analysisStates(m)
  if (st.includes("pending")) parts.push("未完成：AI 業務說明產生中")
  else if (st.includes("partial")) parts.push("未完成：AI 業務說明產生失敗")
  else if (st.includes("unavailable")) parts.push("未包含：AI 業務說明（業務說明不可用）")
  return parts.join("；")
}
