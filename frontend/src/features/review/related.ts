/**
 * Plan「審查重點與可信度」: checks are related only through an explicit link in the data — the same rule (an
 * architecture rule `rule_id`, or the same written rule from an instruction file cited by both) or the same check (the
 * same static finding's own evidence, the same interface). Never through shared code locations: AI findings often cite
 * the same files, which linked every card to every other one. No semantic de-duplication. Pure.
 */
import type { Model } from "@/entities/artifact/model"
import { ruleRefs } from "./layers"

export interface ReviewItem { id: string; title: string; kind: "finding" | "check"; ruleId?: string; rules: string[]; checks: string[] }
export interface Related { id: string; title: string; via: "same_rule" | "same_check" }
export const VIA_LABEL: Record<Related["via"], string> = { same_rule: "同一條規則", same_check: "同一項檢查" }

export function reviewItems(m: Model): ReviewItem[] {
  // a static finding's own evidence (producer static_findings) names that check; citing it links to it
  const own = (ids: string[]) => ids.filter((id) => m.evidence.get(id)?.producer.name === "static_findings")
  return [
    ...(m.overlay?.findings ?? []).map((f) => ({ id: f.id, title: f.title, kind: "finding" as const,
      rules: ruleRefs(m, f.evidence_refs).map((r) => r.id), checks: own(f.evidence_refs) })),
    ...m.archChecks.map((c) => ({ id: c.id, title: c.title, kind: "check" as const, ruleId: c.rule_id,
      rules: ruleRefs(m, c.evidence_refs).map((r) => r.id),
      checks: [...own(c.evidence_refs), ...(c.interface_change ? [`iface:${c.interface_change.interface_id}`] : [])] })),
  ]
}

export function relatedOf(items: ReviewItem[], id: string): Related[] {
  const me = items.find((i) => i.id === id)
  if (!me) return []
  const rules = new Set(me.rules)
  const checks = new Set(me.checks)
  const out: Related[] = []
  for (const o of items) {
    if (o.id === id) continue
    if ((me.ruleId && o.ruleId === me.ruleId) || o.rules.some((r) => rules.has(r))) out.push({ id: o.id, title: o.title, via: "same_rule" })
    else if (o.checks.some((c) => checks.has(c))) out.push({ id: o.id, title: o.title, via: "same_check" })
  }
  return out
}
