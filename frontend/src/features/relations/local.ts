/**
 * Local relation view (plan「變更與影響」關係圖): the current change's capabilities plus their necessary neighbours,
 * aiming at LOCAL_TARGET nodes. What does not fit is grouped (with its total, its review-hint count and paging), never
 * dropped. Every edge carries a relation sentence; the same sentences form the equivalent text list. Only static
 * relations and impact statuses are used — nothing here claims a business impact. Pure: no React.
 */
import { capabilityOf, categoryOf, nameOfId, namesOf, nodeStatus, type Model } from "@/entities/artifact/model"
import type { ChangeStatus, ImpactStatus, Relation } from "@/entities/artifact/types"
import { capNode, prCaps, type Scene, type SEdge, type SNode } from "@/features/system-map/scene"
import { IMPACT_SENTENCE, relationSentence } from "./wording"

export const LOCAL_TARGET = 7

export interface LNode { id: string; focus: boolean; label: string; change: ChangeStatus; impact: ImpactStatus; hints: number; note: string }
export interface LEdge {
  id: string; source: string; target: string; kinds: Relation["kind"][]; weight: number; confidence: Relation["confidence"]
  headOnly: boolean; violations: string[]; sentence: string
}
export interface LocalGraph {
  /** drawn nodes (≤ LOCAL_TARGET including the group node) */
  nodes: LNode[]
  /** edges between drawn nodes */
  edges: LEdge[]
  group: { members: LNode[]; hints: number; edges: LEdge[]; page: number; pages: number } | null
  /** every node and edge: the equivalent text list */
  all: LNode[]
  allEdges: LEdge[]
}

/** Review hints that concern a capability: findings and architecture checks (each counted in its own category). */
export function reviewHints(m: Model, capId: string): { findings: number; checks: number } {
  return {
    findings: (m.overlay?.findings ?? []).filter((f) => f.capability_ids.includes(capId)).length,
    checks: m.archChecks.filter((a) => a.verdict !== "follows" && (a.capability_ids.includes(capId) || (a.interface_change && m.ifaceById.get(a.interface_change.interface_id)?.capability_id === capId))).length,
  }
}

export function localGraph(m: Model, focusIds: string[], page = 0): LocalGraph {
  const focus = [...new Set(focusIds.map((id) => capabilityOf(m, id)).filter((id): id is string => !!id && m.capById.has(id)))]
  const inFocus = new Set(focus)
  const agg = new Map<string, LEdge>()
  const edge = (s: string, t: string) => {
    const key = `${s}>${t}`
    let e = agg.get(key)
    if (!e) agg.set(key, (e = { id: `l:${key}`, source: s, target: t, kinds: [], weight: 0, confidence: "unknown", headOnly: true, violations: [], sentence: "" }))
    return e
  }
  const usable = (id: string) => inFocus.has(id) || categoryOf(m, id) !== "engineering"
  for (const r of m.relations) {
    const s = capabilityOf(m, r.source), t = capabilityOf(m, r.target)
    if (!s || !t || s === t || (!inFocus.has(s) && !inFocus.has(t)) || !usable(s) || !usable(t)) continue
    const e = edge(s, t)
    e.weight += r.weight
    if (!e.kinds.includes(r.kind)) e.kinds.push(r.kind)
    if (r.confidence === "confirmed") e.confidence = "confirmed"
    if (!m.headOnly.has(r.id)) e.headOnly = false
  }
  for (const a of m.archChecks) {
    const v = a.violating_relation
    if (!v || a.verdict === "follows" || (!inFocus.has(v.source) && !inFocus.has(v.target)) || !m.capById.has(v.source) || !m.capById.has(v.target)) continue
    const e = edge(v.source, v.target)
    if (!e.kinds.length) { e.kinds.push("depends_on"); e.weight = Math.max(e.weight, 1); e.confidence = "confirmed" }
    e.violations.push(a.id)
  }
  const pr = prCaps(m)
  const node = (id: string): LNode => {
    const st = nodeStatus(m, id), h = reviewHints(m, id)
    const isFocus = inFocus.has(id)
    const note = isFocus ? `這項修改涉及的功能${st.change !== "unchanged" ? `（${st.change === "added" ? "新增" : st.change === "removed" ? "移除" : "修改"}）` : ""}`
      : st.impact !== "none" ? IMPACT_SENTENCE[st.impact] : "與修改的功能有程式依賴；影響分析未列出這個功能"
    return { id, focus: isFocus, label: nameOfId(m, id).primary, change: st.change, impact: st.impact, hints: h.findings + h.checks, note }
  }
  const neighbourScore = new Map<string, number>()
  for (const e of agg.values()) for (const [a, b] of [[e.source, e.target], [e.target, e.source]] as const) {
    if (!inFocus.has(a) || inFocus.has(b)) continue
    neighbourScore.set(b, (neighbourScore.get(b) ?? 0) + e.weight + (e.violations.length ? 1e7 : 0))
  }
  const neighbours = [...neighbourScore.keys()].map(node).sort((a, b) => score(b) - score(a) || a.label.localeCompare(b.label))
  function score(n: LNode) {
    // review hints and impact first: overflow must never be where the important items hide
    return (neighbourScore.get(n.id) ?? 0) + n.hints * 1e5 + (pr.has(n.id) ? (3 - pr.get(n.id)!) * 1e4 : 0) - (categoryOf(m, n.id) === "shared" ? 5e3 : 0)
  }
  const all = [...focus.map(node), ...neighbours]
  for (const e of agg.values()) {
    const base = relationSentence(nameOfId(m, e.source).primary, nameOfId(m, e.target).primary, e.kinds)
    const tags = [
      e.violations.length ? `違反架構規則：${e.violations.map((id) => m.archChecks.find((a) => a.id === id)?.title ?? id).join("、")}` : "",
      e.confidence === "unknown" ? "未確認，可能經由動態呼叫" : "",
      e.headOnly ? "這個 PR 新增" : "",
      `${e.weight} 項靜態依據`,
    ].filter(Boolean)
    e.sentence = `${base}（${tags.join("；")}）`
  }
  const allEdges = [...agg.values()].sort((a, b) => b.violations.length - a.violations.length || b.weight - a.weight || a.id.localeCompare(b.id))
  let drawn: LNode[], rest: LNode[], pg = 0, pages = 1
  if (all.length <= LOCAL_TARGET) { drawn = all; rest = [] }
  else {
    const room = LOCAL_TARGET - 1
    if (focus.length >= room) { drawn = all.slice(0, room); rest = all.slice(room) }
    else {
      const size = room - focus.length
      pages = Math.ceil(neighbours.length / size)
      pg = ((page % pages) + pages) % pages
      const shown = neighbours.slice(pg * size, pg * size + size)
      drawn = [...all.slice(0, focus.length), ...shown]
      rest = neighbours.filter((n) => !shown.includes(n))
    }
  }
  const ids = new Set(drawn.map((n) => n.id))
  const restIds = new Set(rest.map((n) => n.id))
  return {
    nodes: drawn, all, allEdges,
    edges: allEdges.filter((e) => ids.has(e.source) && ids.has(e.target)),
    group: rest.length ? { members: rest, hints: rest.reduce((s, n) => s + n.hints, 0), edges: allEdges.filter((e) => restIds.has(e.source) || restIds.has(e.target)), page: pg, pages } : null,
  }
}

export const GROUP_ID = "local:group"

/** The local graph as a map scene (drawn by the shared MapView; edge sentences shown on hover / focus, not on the canvas). */
export function localScene(m: Model, g: LocalGraph, pageKey: string): Scene {
  const nodes: SNode[] = g.nodes.map((n) => ({ ...capNode(m, m.capById.get(n.id)!), members: [n.id], context: !n.focus, detail: n.hints ? `審查提示 ${n.hints} 項` : n.focus ? "這項修改涉及" : "相關功能" }))
  if (g.group) {
    const hint = g.group.hints ? `，含 ${g.group.hints} 項審查提示` : ""
    nodes.push({ id: GROUP_ID, kind: "summary", members: g.group.members.map((x) => x.id), label: `其他 ${g.group.members.length} 個相關功能${hint}`,
      detail: g.group.pages > 1 ? `點擊顯示下一批（第 ${g.group.page + 1}/${g.group.pages} 批）` : "完整清單見下方文字", action: { kind: "page", key: pageKey }, w: 230, h: 72 })
  }
  const edges: SEdge[] = g.edges.map((e) => ({
    id: e.id, source: e.source, target: e.target, weight: e.weight, confidence: e.confidence, kinds: e.kinds, relationIds: [], evidenceRefs: [], violations: e.violations,
    headOnly: e.headOnly, sentence: e.sentence.replace(/（.*$/, "") + (e.violations.length ? "（違反規則）" : e.confidence === "unknown" ? "（未確認）" : ""),
  }))
  const owner = new Map<string, string>(nodes.map((n) => [n.ref ?? n.id, n.id]))
  for (const mem of g.group?.members ?? []) owner.set(mem.id, GROUP_ID)
  return { key: `local|${nodes.map((n) => n.id).join(",")}|${edges.map((e) => e.id).join(",")}`, mode: "local", level: {}, nodes, edges, hidden: [], owner }
}

export const nodeName = (m: Model, id: string) => namesOf(m.capById.get(id)!).primary
