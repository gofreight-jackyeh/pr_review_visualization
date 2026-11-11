import { domainName, flowList, namesOf, type Model } from "@/entities/artifact/model"
import { crumbsFor, type Level, type SceneMode } from "@/features/system-map/scene"

/** Spec §4 「每頁一個焦點問題」: the one question each view answers, shown at its top. */
export type FocusCtx =
  | { view: "map"; mode: SceneMode; level: Level }
  | { view: "flow"; flowId: string | null }
  | { view: "explain"; selectionId?: string | null; phase: "system" | "overlay" }
  | { view: "architecture" | "findings" | "guide" }

export function focusQuestion(m: Model, ctx: FocusCtx): string {
  switch (ctx.view) {
    case "map": {
      const { level, mode } = ctx
      if (level.capId && m.capById.has(level.capId)) return `「${namesOf(m.capById.get(level.capId)!).primary}」負責什麼、和誰往來？`
      if (level.domainId) {
        const label = crumbsFor(m, mode, level)[1]?.label ?? domainName(m, level.domainId).primary
        return mode === "pr" ? `這個 PR 動到「${label}」的哪些能力？` : `「${label}」由哪些業務能力組成？`
      }
      return mode === "pr" ? "這個 PR 動到系統的哪裡？" : "這個系統由哪些業務領域組成？"
    }
    case "flow": {
      const f = ctx.flowId ? m.flows.find((x) => x.id === ctx.flowId) : undefined
      if (!f) return "系統有哪些主要業務流程？"
      const related = flowList(m).find((i) => i.flow.id === f.id)?.related
      return m.overlay && related ? `這個 PR 讓「${namesOf(f).primary}」有什麼不同？` : `「${namesOf(f).primary}」怎麼運作？`
    }
    case "explain": {
      const el = ctx.selectionId ? m.byId.get(ctx.selectionId) : undefined
      if (el) return `「${namesOf(el).primary}」是什麼？`
      if (ctx.selectionId && (m.domains.some((d) => d.id === ctx.selectionId) || ctx.selectionId.startsWith("bucket:")))
        return `「${crumbsFor(m, "system", { domainId: ctx.selectionId })[1].label}」包含哪些能力？`
      return ctx.phase === "overlay" && m.overlay ? "為什麼要做這次修改、改變了什麼？" : "這個系統是做什麼的？"
    }
    case "architecture": return "這個 PR 動到哪些對外介面？會不會影響客戶端？"
    case "findings": return "有哪些問題需要進一步確認？"
    case "guide": return "要依什麼順序看懂這個 PR？"
  }
}
