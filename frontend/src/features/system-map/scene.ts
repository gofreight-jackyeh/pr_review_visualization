/**
 * Layered map scenes (spec §4 System Hierarchy / Entry Experience). One level at a time:
 * System Overview → domains; a domain → its capabilities; a capability → its children, PR-related elements and neighbours;
 * the PR view → impacted capabilities + their strongest neighbours (≤ PR_NEIGHBOURS_PER_CAP each, never shared ones) +
 * per-domain summaries, aiming at PR_TARGET nodes; a flow → the capabilities it passes.
 * Every scene stays within NODE_BUDGET; edges are aggregated to the drawn nodes and only the top EDGES_PER_NODE per node
 * (PR_EDGES_PER_NODE in the PR view) are drawn (the rest are listed in the panel). Pure: no React.
 */
import { ownRings } from "@/features/architecture/interfaces"
import {
  capabilityOf, capPath, categoryOf, descendants, domainName, domainOf, flowSteps, isChanged, namesOf, nodeStatus, type Model,
} from "@/entities/artifact/model"
import type { Capability, Relation } from "@/entities/artifact/types"

export const NODE_BUDGET = 25
export const EDGES_PER_NODE = 3
/** PR view (on a large system, impacted capabilities touched hubs, so all 1-hop neighbours filled the 25-node budget). */
export const PR_TARGET = 15, PR_NEIGHBOURS_PER_CAP = 2, PR_EDGES_PER_NODE = 2
export const NODE_W = 200, NODE_H = 84
const DOMAIN_W = 230, DOMAIN_H = 100, SUMMARY_W = 230, SUMMARY_H = 72
/** Pseudo domains that hold the hidden-by-default categories. */
export const SHARED_BUCKET = "bucket:shared", ENGINEERING_BUCKET = "bucket:engineering"
const BUCKET_LABEL: Record<string, string> = { [SHARED_BUCKET]: "共用基礎", [ENGINEERING_BUCKET]: "工程與工具" }

export type SceneMode = "system" | "pr" | "flow" | "local"
export interface Level { domainId?: string; capId?: string }
export interface SceneOpts {
  mode: SceneMode
  level?: Level
  showShared?: boolean
  showEngineering?: boolean
  flowId?: string
  /** overflow paging per summary node key */
  pages?: Record<string, number>
}

export type SNodeKind = "domain" | "capability" | "entity" | "entry_point" | "summary" | "bucket"
export type SAction = { kind: "domain"; id: string } | { kind: "capability"; id: string } | { kind: "element"; id: string } | { kind: "page"; key: string }
export interface SNode {
  id: string
  kind: SNodeKind
  /** map element (capability / entity / entry point) or domain id */
  ref?: string
  /** capability ids this node stands for: relations from any of them aggregate onto it */
  members: string[]
  label: string
  secondary?: string
  detail?: string
  /** provenance tag, e.g. 由 Claude 歸類 / 依目錄分組 */
  tag?: string
  action: SAction
  /** a neighbour outside the current level, drawn for context */
  context?: boolean
  w: number
  h: number
}
export interface SEdge {
  id: string
  source: string
  target: string
  weight: number
  confidence: Relation["confidence"]
  kinds: Relation["kind"][]
  relationIds: string[]
  evidenceRefs: string[]
  /** architecture checks whose violating_relation this edge carries — always drawn, highlighted */
  violations: string[]
  headOnly: boolean
  hiddenReason?: "top_k" | "shared"
  /** explicit edge label (e.g. a relation sentence); default: kinds × weight */
  label?: string
  /** relation sentence shown only on hover / focus (local view keeps labels off the canvas) */
  sentence?: string
}
export interface Scene {
  key: string
  mode: SceneMode
  level: Level
  nodes: SNode[]
  /** drawn edges */
  edges: SEdge[]
  /** aggregated edges between drawn nodes that are not drawn (listed in the panel) */
  hidden: SEdge[]
  /** element id → drawn node standing for it */
  owner: Map<string, string>
}

// ---------- helpers ----------
const caps = (m: Model) => m.caps
const isTop = (m: Model, c: Capability) => !c.parent_capability_id || !m.capById.has(c.parent_capability_id)

/** Capabilities the PR touches, with a rank (0 = changed/direct, 1 = dependency, 2 = potential/unknown). */
export function prCaps(m: Model): Map<string, number> {
  const out = new Map<string, number>()
  if (!m.overlay) return out
  const put = (id: string | undefined, rank: number) => { if (id && m.capById.has(id)) out.set(id, Math.min(out.get(id) ?? 9, rank)) }
  for (const i of m.overlay.impacts) {
    if (i.target_kind === "flow") continue
    const rank = i.change_status !== "unchanged" || i.impact_status === "direct" ? 0 : i.impact_status === "confirmed_dependency" || i.impact_status === "evidence_supported" ? 1 : 2
    put(capabilityOf(m, i.target_id), rank)
  }
  for (const id of m.headOnly) put(capabilityOf(m, id), 0)
  for (const f of m.overlay.changes.files) put(f.capability_id ?? undefined, 0)
  for (const [id] of ownRings(m)) put(id, 0) // a capability whose interface changed is always drawn
  return out
}

function weightIndex(m: Model): Map<string, number> {
  const w = new Map<string, number>()
  for (const r of m.relations) for (const id of [r.source, r.target]) w.set(id, (w.get(id) ?? 0) + r.weight)
  return w
}

export function capNode(m: Model, c: Capability, opts: { drawChildren?: boolean; context?: boolean } = {}): SNode {
  const n = namesOf(c)
  const kids = m.children.get(c.id) ?? []
  const eps = descendants(m, c.id).reduce((s, id) => s + (m.capById.get(id)?.entry_point_ids.length ?? 0), 0)
  const parent = c.parent_capability_id ? m.capById.get(c.parent_capability_id) : undefined
  return {
    id: c.id, kind: "capability", ref: c.id, members: opts.drawChildren ? [c.id] : descendants(m, c.id),
    label: n.primary, secondary: parent ? `${namesOf(parent).primary} › ${c.technical_name}` : n.secondary,
    detail: kids.length ? `${kids.length} 個子能力 · ${eps} 入口` : `${c.entry_point_ids.length} 入口 · ${c.entity_ids.length} 資料`,
    action: { kind: "capability", id: c.id }, context: opts.context, w: NODE_W, h: NODE_H,
  }
}

function domainNode(m: Model, domId: string, members: string[], context = false): SNode {
  if (domId === SHARED_BUCKET || domId === ENGINEERING_BUCKET) {
    return { id: domId, kind: "bucket", ref: domId, members, label: BUCKET_LABEL[domId], detail: `${members.length} 個模組（預設隱藏）`, tag: "依目錄分類",
      action: { kind: "domain", id: domId }, context, w: DOMAIN_W, h: DOMAIN_H }
  }
  const d = m.domains.find((x) => x.id === domId)
  const top = members.filter((id) => { const c = m.capById.get(id); return c && isTop(m, c) }).length
  return {
    id: domId, kind: "domain", ref: domId, members, label: domainName(m, domId).primary, secondary: d && d.business_name ? d.technical_name : undefined,
    detail: `${top} 個能力`, tag: !d ? "未歸類" : d.source === "semantic" ? "由 Claude 歸類" : "依目錄分組",
    action: { kind: "domain", id: domId }, context, w: DOMAIN_W, h: DOMAIN_H,
  }
}

/** Business capabilities per domain, in domain order (unassigned last). */
function domainMembers(m: Model, includeHeadOnly: boolean): Map<string, string[]> {
  const out = new Map<string, string[]>(m.domains.map((d) => [d.id, []]))
  for (const c of caps(m)) {
    if (c.category !== "business" || (!includeHeadOnly && m.headOnly.has(c.id))) continue
    const d = domainOf(m, c.id)
    out.set(d, [...(out.get(d) ?? []), c.id])
  }
  for (const [k, v] of out) if (!v.length) out.delete(k)
  return out
}

function bucketMembers(m: Model, bucket: string, includeHeadOnly: boolean): string[] {
  const cat = bucket === SHARED_BUCKET ? "shared" : "engineering"
  return caps(m).filter((c) => c.category === cat && (includeHeadOnly || !m.headOnly.has(c.id))).map((c) => c.id)
}

/** Overflow: show one page of `items`, the rest behind a summary node that pages on click. */
function paged<T>(items: T[], room: number, page: number): { shown: T[]; rest: number; page: number } {
  if (items.length <= room) return { shown: items, rest: 0, page: 0 }
  const size = Math.max(1, room - 1), pages = Math.ceil(items.length / size), p = ((page % pages) + pages) % pages
  const shown = items.slice(p * size, p * size + size)
  return { shown, rest: items.length - shown.length, page: p }
}
function pageNode(key: string, rest: number, noun: string, members: string[]): SNode {
  return { id: `summary:${key}`, kind: "summary", members, label: `其餘 ${rest} 個${noun}`, detail: "點擊顯示下一批", action: { kind: "page", key }, w: SUMMARY_W, h: SUMMARY_H }
}

// ---------- scenes ----------
function overview(m: Model, o: SceneOpts, inclHead: boolean): SNode[] {
  const doms = domainMembers(m, inclHead)
  const items: SNode[] = [...doms].map(([d, mem]) => domainNode(m, d, mem))
  const buckets: SNode[] = []
  if (o.showShared) { const mem = bucketMembers(m, SHARED_BUCKET, inclHead); if (mem.length) buckets.push(domainNode(m, SHARED_BUCKET, mem)) }
  if (o.showEngineering) { const mem = bucketMembers(m, ENGINEERING_BUCKET, inclHead); if (mem.length) buckets.push(domainNode(m, ENGINEERING_BUCKET, mem)) }
  const pg = paged(items, NODE_BUDGET - buckets.length, o.pages?.domains ?? 0)
  const restMembers = items.filter((n) => !pg.shown.includes(n)).flatMap((n) => n.members)
  return [...pg.shown, ...buckets, ...(pg.rest ? [pageNode("domains", pg.rest, "領域", restMembers)] : [])]
}

function rankCaps(m: Model, ids: string[]): string[] {
  const pr = prCaps(m), w = weightIndex(m)
  const score = (id: string) => (pr.has(id) ? 1e9 - pr.get(id)! * 1e8 : 0) + descendants(m, id).reduce((s, d) => s + (w.get(d) ?? 0) + (m.capById.get(d)?.entry_point_ids.length ?? 0), 0)
  return [...ids].sort((a, b) => score(b) - score(a) || a.localeCompare(b))
}

/** Total relation weight between two capability sets (both directions). */
function linkWeight(m: Model, a: Set<string>, b: Set<string>): number {
  let w = 0
  for (const r of m.relations) if ((a.has(r.source) && b.has(r.target)) || (b.has(r.source) && a.has(r.target))) w += r.weight
  return w
}

function domainScene(m: Model, o: SceneOpts, domId: string, inclHead: boolean): SNode[] {
  const isBucket = domId === SHARED_BUCKET || domId === ENGINEERING_BUCKET
  const doms = domainMembers(m, inclHead)
  const all = isBucket ? bucketMembers(m, domId, inclHead) : doms.get(domId) ?? []
  const tops = rankCaps(m, all.filter((id) => isTop(m, m.capById.get(id)!)))
  const inside = new Set(all)
  // context: the other domains this one talks to most (max 4), and the hidden-by-default buckets when switched on
  const ctx: SNode[] = [...doms].filter(([d]) => d !== domId).map(([d, mem]) => ({ d, mem, w: linkWeight(m, inside, new Set(mem)) }))
    .filter((x) => x.w > 0).sort((a, b) => b.w - a.w).slice(0, 4).map((x) => domainNode(m, x.d, x.mem, true))
  for (const [b, on] of [[SHARED_BUCKET, o.showShared], [ENGINEERING_BUCKET, o.showEngineering]] as const) {
    if (!on || b === domId) continue
    const mem = bucketMembers(m, b, inclHead)
    if (mem.length) ctx.push(domainNode(m, b, mem, true))
  }
  const pg = paged(tops, NODE_BUDGET - ctx.length, o.pages?.[domId] ?? 0)
  const rest = tops.filter((id) => !pg.shown.includes(id)).flatMap((id) => descendants(m, id))
  return [...pg.shown.map((id) => capNode(m, m.capById.get(id)!)), ...ctx, ...(pg.rest ? [pageNode(domId, pg.rest, "能力", rest)] : [])]
}

function hiddenCategory(m: Model, o: SceneOpts, id: string) {
  const cat = categoryOf(m, id)
  return (cat === "shared" && !o.showShared) || (cat === "engineering" && !o.showEngineering)
}

function capabilityScene(m: Model, o: SceneOpts, capId: string, inclHead: boolean): SNode[] {
  const c = m.capById.get(capId)!
  const pr = prCaps(m)
  const kids = rankCaps(m, (m.children.get(capId) ?? []).filter((id) => inclHead || !m.headOnly.has(id)))
  const self = new Set(descendants(m, capId))
  // only PR-related entities / entry points are drawn; the full lists live in the detail panel
  const prItems: SNode[] = []
  if (m.overlay) {
    for (const e of [...m.entities, ...m.entryPoints]) {
      if (e.capability_id !== capId || !isChanged(m, e.id) || (!inclHead && m.headOnly.has(e.id))) continue
      const kind = m.kindOf.get(e.id) as "entity" | "entry_point"
      const n = namesOf(e)
      prItems.push({ id: e.id, kind, ref: e.id, members: [], label: n.primary, secondary: n.secondary, detail: "kind" in e ? `入口 · ${e.kind}` : "核心資料",
        action: { kind: "element", id: e.id }, w: NODE_W, h: NODE_H })
    }
  }
  const nb = new Map<string, number>()
  for (const r of m.relations) {
    const s = capabilityOf(m, r.source), t = capabilityOf(m, r.target)
    if (!s || !t) continue
    const other = self.has(s) && !self.has(t) ? t : self.has(t) && !self.has(s) ? s : null
    if (!other || hiddenCategory(m, o, other) || (!inclHead && m.headOnly.has(other))) continue
    nb.set(other, (nb.get(other) ?? 0) + r.weight + (pr.has(other) ? 1e6 : 0))
  }
  const neighbours = [...nb].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([id]) => id)
  const fixed = 1 + prItems.slice(0, 8).length
  const kidPage = paged(kids, Math.max(2, NODE_BUDGET - fixed - Math.min(6, neighbours.length) - 1), o.pages?.[capId] ?? 0)
  const nodes: SNode[] = [capNode(m, c, { drawChildren: kids.length > 0 }), ...kidPage.shown.map((id) => capNode(m, m.capById.get(id)!)), ...prItems.slice(0, 8)]
  if (kidPage.rest) nodes.push(pageNode(capId, kidPage.rest, "子能力", kids.filter((id) => !kidPage.shown.includes(id)).flatMap((id) => descendants(m, id))))
  const room = NODE_BUDGET - nodes.length
  const nbPage = paged(neighbours, room, o.pages?.[`${capId}:nb`] ?? 0)
  nodes.push(...nbPage.shown.map((id) => capNode(m, m.capById.get(id)!, { context: true })))
  if (nbPage.rest) {
    const n = pageNode(`${capId}:nb`, nbPage.rest, "相關能力", neighbours.filter((id) => !nbPage.shown.includes(id)))
    nodes.push({ ...n, label: `其他 ${nbPage.rest} 個相關能力` })
  }
  return nodes
}

/**
 * Draw `candidates` (already ranked) while every business capability not drawn is folded into a per-domain summary
 * ("會計財務：12 個能力，未受影響"); the total stays within the budget.
 */
function withDomainSummaries(m: Model, o: SceneOpts, candidates: string[], inclHead: boolean, noun: (affected: number) => string,
  budget = NODE_BUDGET, minDrawn = 0): SNode[] {
  const doms = domainMembers(m, inclHead)
  const pr = prCaps(m)
  const extra: SNode[] = []
  for (const [b, on] of [[SHARED_BUCKET, o.showShared], [ENGINEERING_BUCKET, o.showEngineering]] as const) {
    if (!on) continue
    const mem = bucketMembers(m, b, inclHead).filter((id) => !candidates.includes(id))
    if (mem.length) extra.push(domainNode(m, b, mem, true))
  }
  for (let k = Math.min(candidates.length, budget); k >= minDrawn; k--) {
    const drawn = new Set(candidates.slice(0, k))
    // a drawn capability also stands for its undrawn split descendants
    const covered = new Set([...drawn].flatMap((id) => descendants(m, id)))
    const summaries: SNode[] = []
    for (const [d, mem] of doms) {
      const rest = mem.filter((id) => !covered.has(id))
      if (!rest.length) continue
      const restTop = rest.filter((id) => { const c = m.capById.get(id)!; return isTop(m, c) || covered.has(c.parent_capability_id!) }).length || rest.length
      const affected = rest.filter((id) => pr.has(id)).length
      const anyDrawn = mem.some((id) => drawn.has(id))
      const name = domainName(m, d).primary
      summaries.push({ id: `summary:${d}`, kind: "summary", ref: d, members: rest, label: `${name}：${anyDrawn ? "其餘 " : ""}${restTop} 個能力，${noun(affected)}`,
        detail: "點擊進入這個領域", action: { kind: "domain", id: d }, w: SUMMARY_W, h: SUMMARY_H })
    }
    if (k + summaries.length + extra.length <= budget) {
      const nodes = [...drawn].map((id) => capNode(m, m.capById.get(id)!))
      return [...nodes, ...summaries, ...extra]
    }
  }
  return []
}

function prScene(m: Model, o: SceneOpts, inclHead: boolean): SNode[] {
  const pr = prCaps(m)
  const core = new Set([...pr.keys()].filter((id) => categoryOf(m, id) !== "engineering" || o.showEngineering))
  for (const a of m.archChecks) if (a.violating_relation) for (const id of [a.violating_relation.source, a.violating_relation.target]) if (m.capById.has(id)) core.add(id)
  const coreRanked = [...core].sort((a, b) => (pr.get(a) ?? 1) - (pr.get(b) ?? 1) || a.localeCompare(b))
  // context: per impacted capability its PR_NEIGHBOURS_PER_CAP strongest neighbours by relation weight, never a shared one
  const byCore = new Map<string, Map<string, number>>()
  for (const r of m.relations) {
    const s = capabilityOf(m, r.source), t = capabilityOf(m, r.target)
    if (!s || !t || s === t) continue
    for (const [c, other] of [[s, t], [t, s]] as const) {
      if (!core.has(c) || core.has(other) || categoryOf(m, other) === "shared" || hiddenCategory(m, o, other)) continue
      const w = byCore.get(c) ?? new Map<string, number>()
      w.set(other, (w.get(other) ?? 0) + r.weight)
      byCore.set(c, w)
    }
  }
  const nbRanked: string[] = []
  for (const c of coreRanked) {
    const top = [...(byCore.get(c) ?? [])].filter(([id]) => !nbRanked.includes(id)).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    nbRanked.push(...top.slice(0, PR_NEIGHBOURS_PER_CAP).map(([id]) => id))
  }
  const noun = (n: number) => (n ? `${n} 個受影響` : "未受影響")
  const candidates = [...coreRanked, ...nbRanked]
  // aim at PR_TARGET with every impacted capability drawn; a PR touching many capabilities gets the full budget instead
  const target = withDomainSummaries(m, o, candidates, inclHead, noun, PR_TARGET, coreRanked.length)
  return target.length ? target : withDomainSummaries(m, o, candidates, inclHead, noun)
}

function flowScene(m: Model, o: SceneOpts, flowId: string): SNode[] {
  const steps = flowSteps(m, flowId).steps
  const count = new Map<string, number>()
  for (const s of steps) count.set(s.capability_id, (count.get(s.capability_id) ?? 0) + (s.step_change_status !== "unchanged" ? 1000 : 1))
  const f = m.flows.find((x) => x.id === flowId)
  for (const id of f?.capability_ids ?? []) if (!count.has(id)) count.set(id, 0)
  const ranked = [...count].filter(([id]) => m.capById.has(id) && !hiddenCategory(m, o, id)).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([id]) => id)
  // the flow map shows only this flow's capabilities; the rest of the system is one summary per domain only when needed
  if (ranked.length <= NODE_BUDGET) return ranked.map((id) => capNode(m, m.capById.get(id)!))
  return withDomainSummaries(m, { ...o, showShared: false, showEngineering: false }, ranked, true, () => "不在畫面上").filter((n) => n.kind !== "summary" || n.members.some((id) => count.has(id)))
}

// ---------- edges ----------
function buildOwner(m: Model, nodes: SNode[]): Map<string, string> {
  const owner = new Map<string, string>()
  // general first, specific last: domain / summary < capability (ancestors before split descendants) < entity / entry point
  const rank = (n: SNode) => (n.kind === "capability" ? 10 + capPath(m, n.ref!).length : n.kind === "entity" || n.kind === "entry_point" ? 100 : 0)
  for (const n of [...nodes].sort((a, b) => rank(a) - rank(b))) {
    for (const id of n.members) owner.set(id, n.id)
    if (n.ref && n.kind !== "domain" && n.kind !== "bucket" && n.kind !== "summary") owner.set(n.ref, n.id)
  }
  return owner
}

/** Drawn node that stands for any element id (entities / entry points / components resolve via their capability). */
export function resolveNode(m: Model, owner: Map<string, string>, id: string): string | undefined {
  if (owner.has(id)) return owner.get(id)
  if (m.kindOf.get(id) === "flow") { const f = m.flows.find((x) => x.id === id); return f ? resolveNode(m, owner, f.entry_point_id) : undefined }
  const cap = capabilityOf(m, id)
  return cap ? owner.get(cap) : undefined
}

function buildEdges(m: Model, o: SceneOpts, nodes: SNode[], owner: Map<string, string>): { edges: SEdge[]; hidden: SEdge[] } {
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const agg = new Map<string, SEdge>()
  const edgeFor = (s: string, t: string) => {
    const key = `${s}>${t}`
    let e = agg.get(key)
    if (!e) agg.set(key, (e = { id: `e:${key}`, source: s, target: t, weight: 0, confidence: "unknown", kinds: [], relationIds: [], evidenceRefs: [], violations: [], headOnly: true }))
    return e
  }
  for (const r of m.relations) {
    const s = resolveNode(m, owner, r.source), t = resolveNode(m, owner, r.target)
    if (!s || !t || s === t) continue
    const e = edgeFor(s, t)
    e.weight += r.weight
    e.relationIds.push(r.id)
    e.evidenceRefs.push(...r.evidence_refs)
    if (!e.kinds.includes(r.kind)) e.kinds.push(r.kind)
    if (r.confidence === "confirmed") e.confidence = "confirmed"
    if (!m.headOnly.has(r.id)) e.headOnly = false
  }
  if (o.mode !== "system") for (const a of m.archChecks) {
    const v = a.violating_relation
    if (!v || a.verdict === "follows") continue
    const s = resolveNode(m, owner, v.source), t = resolveNode(m, owner, v.target)
    if (!s || !t || s === t) continue
    const e = edgeFor(s, t)
    if (!e.weight) { e.weight = 1; e.confidence = "confirmed" }
    e.violations.push(a.id)
  }
  const isSharedNode = (id: string) => { const n = byId.get(id); return !!n && (n.id === SHARED_BUCKET || (n.kind === "capability" && categoryOf(m, n.ref!) === "shared")) }
  const sharedScene = o.level?.domainId === SHARED_BUCKET
  const all = [...agg.values()]
  const edges: SEdge[] = [], hidden: SEdge[] = []
  const deg = new Map<string, number>()
  const perNode = o.mode === "pr" && !o.level?.domainId && !o.level?.capId ? PR_EDGES_PER_NODE : EDGES_PER_NODE
  const take = (e: SEdge) => { edges.push(e); deg.set(e.source, (deg.get(e.source) ?? 0) + 1); deg.set(e.target, (deg.get(e.target) ?? 0) + 1) }
  for (const e of all) if (e.violations.length) take(e)
  const rest = all.filter((e) => !e.violations.length).sort((a, b) => Number(b.headOnly) - Number(a.headOnly) || b.weight - a.weight || a.id.localeCompare(b.id))
  for (const e of rest) {
    if (!o.showShared && !sharedScene && (isSharedNode(e.source) || isSharedNode(e.target))) { hidden.push({ ...e, hiddenReason: "shared" }); continue }
    if ((deg.get(e.source) ?? 0) < perNode && (deg.get(e.target) ?? 0) < perNode) take(e)
    else hidden.push({ ...e, hiddenReason: "top_k" })
  }
  return { edges, hidden }
}

// ---------- entry ----------
export function buildScene(m: Model, o: SceneOpts): Scene {
  const level = o.level ?? {}
  // the system overview is the base commit only; PR views include what the head adds
  const inclHead = o.mode !== "system"
  let nodes: SNode[]
  if (o.mode === "flow" && o.flowId) nodes = flowScene(m, o, o.flowId)
  else if (level.capId && m.capById.has(level.capId)) nodes = capabilityScene(m, o, level.capId, inclHead)
  else if (level.domainId) nodes = domainScene(m, o, level.domainId, inclHead)
  else if (o.mode === "pr" && m.overlay) nodes = prScene(m, o, inclHead)
  else nodes = overview(m, o, inclHead)
  const owner = buildOwner(m, nodes)
  const { edges, hidden } = buildEdges(m, o, nodes, owner)
  const key = `${o.mode}|${level.domainId ?? ""}|${level.capId ?? ""}|${o.flowId ?? ""}|${nodes.map((n) => n.id).join(",")}|${edges.map((e) => e.id).join(",")}`
  return { key, mode: o.mode, level, nodes, edges, hidden, owner }
}

/** Status of a drawn node: element nodes use their own status; domains / summaries aggregate their members. */
export function sceneNodeStatus(m: Model, n: SNode): ReturnType<typeof nodeStatus> {
  if (n.kind === "capability" || n.kind === "entity" || n.kind === "entry_point") {
    const own = nodeStatus(m, n.ref!)
    if (own.change !== "unchanged" || own.impact !== "none" || n.kind !== "capability") return own
  }
  const order = ["direct", "confirmed_dependency", "evidence_supported", "potential", "unknown"] as const
  let change: ReturnType<typeof nodeStatus>["change"] = "unchanged", best = 99
  for (const id of n.members) {
    const s = nodeStatus(m, id)
    if (s.change !== "unchanged") change = "modified"
    if (s.impact !== "none") best = Math.min(best, order.indexOf(s.impact))
  }
  return { change, impact: best === 99 ? "none" : order[best] }
}

export interface Crumb { label: string; level: Level }
/** 系統 › 領域 › 能力 (› 子能力). */
export function crumbsFor(m: Model, mode: SceneMode, level: Level): Crumb[] {
  const out: Crumb[] = [{ label: mode === "pr" ? "系統（PR 影響範圍）" : "系統", level: {} }]
  const dom = level.domainId ?? (level.capId ? (bucketOf(m, level.capId) ?? domainOf(m, level.capId)) : undefined)
  if (dom) out.push({ label: BUCKET_LABEL[dom] ?? domainName(m, dom).primary, level: { domainId: dom } })
  if (level.capId) {
    for (const id of capPath(m, level.capId)) out.push({ label: namesOf(m.capById.get(id)!).primary, level: { domainId: dom, capId: id } })
  }
  return out
}
function bucketOf(m: Model, capId: string): string | undefined {
  const cat = categoryOf(m, capId)
  return cat === "shared" ? SHARED_BUCKET : cat === "engineering" ? ENGINEERING_BUCKET : undefined
}
