/**
 * Plan「審查重點與可信度」: checks are related only through an explicit link in the data — the same architecture rule
 * (`rule_id`) or the same code location (side + path + line). No semantic de-duplication. Pure.
 */
import type { Model } from "@/entities/artifact/model"
import type { CodeRef } from "@/entities/artifact/types"

export interface ReviewItem { id: string; title: string; kind: "finding" | "check"; ruleId?: string; refs: CodeRef[] }
export interface Related { id: string; title: string; via: "same_rule" | "same_location" }
export const VIA_LABEL: Record<Related["via"], string> = { same_rule: "同一條架構規則", same_location: "同一個程式位置" }

export function reviewItems(m: Model): ReviewItem[] {
  return [
    ...(m.overlay?.findings ?? []).map((f) => ({ id: f.id, title: f.title, kind: "finding" as const, refs: f.refs })),
    ...m.archChecks.map((c) => ({ id: c.id, title: c.title, kind: "check" as const, ruleId: c.rule_id, refs: c.pr_refs })),
  ]
}

const loc = (r: CodeRef) => `${r.side}:${r.path}:${r.start_line}`

export function relatedOf(items: ReviewItem[], id: string): Related[] {
  const me = items.find((i) => i.id === id)
  if (!me) return []
  const mine = new Set(me.refs.map(loc))
  const out: Related[] = []
  for (const o of items) {
    if (o.id === id) continue
    if (me.ruleId && o.ruleId === me.ruleId) out.push({ id: o.id, title: o.title, via: "same_rule" })
    else if (o.refs.some((r) => mine.has(loc(r)))) out.push({ id: o.id, title: o.title, via: "same_location" })
  }
  return out
}
