import type {
  ArchCheck, ArchRule, Capability, ChangeStatus, CodeRef, Component, Domain, ElementKind, Entity, EntryPoint, Evidence, Flow, FlowChange, FlowView, Impact,
  ImpactStatus, Interface, MapElement, MapView, OverlayStep, PrOverlay, StepStatus, SystemMap,
} from "./types"

export const DISCLAIMER = "由靜態分析重建，不是實際執行紀錄"

export interface Model {
  map: SystemMap
  overlay: PrOverlay | null
  caps: Capability[]
  entities: Entity[]
  entryPoints: EntryPoint[]
  components: Component[]
  relations: SystemMap["relations"]
  flows: Flow[]
  /** ids (elements and relations) that exist only on the PR head */
  headOnly: Set<string>
  kindOf: Map<string, ElementKind>
  byId: Map<string, MapElement>
  evidence: Map<string, Evidence>
  impact: Map<string, Impact>
  flowChange: Map<string, FlowChange>
  /** "business descriptions available" — false shows the 業務說明不可用 banner */
  semanticAvailable: boolean
  domains: Domain[]
  capById: Map<string, Capability>
  /** split capabilities: parent id → child ids */
  children: Map<string, string[]>
  rules: Map<string, ArchRule>
  archChecks: ArchCheck[]
  /** interfaces (spec §8), base + head additions */
  interfaces: Interface[]
  ifaceById: Map<string, Interface>
}

/** Business capabilities without a domain_id land here (a backend gap, not expected). */
export const UNASSIGNED_DOMAIN = "dom:__unassigned"

export function buildModel(map: SystemMap, overlay: PrOverlay | null = null): Model {
  const add = overlay?.head_additions
  const caps = [...map.capabilities, ...(add?.capabilities ?? [])]
  const entities = [...map.entities, ...(add?.entities ?? [])]
  const entryPoints = [...map.entry_points, ...(add?.entry_points ?? [])]
  const components = [...map.components, ...(add?.components ?? [])]
  const relations = [...map.relations, ...(add?.relations ?? [])]
  const flows = [...map.flows, ...(add?.flows ?? [])]
  const headOnly = new Set<string>()
  if (add) for (const l of [add.capabilities, add.entities, add.entry_points, add.components, add.flows, add.relations]) for (const e of l) headOnly.add(e.id)
  const kindOf = new Map<string, ElementKind>(), byId = new Map<string, MapElement>()
  const reg = (list: MapElement[], k: ElementKind) => list.forEach((e) => { kindOf.set(e.id, k); byId.set(e.id, e) })
  reg(caps, "capability"); reg(entities, "entity"); reg(entryPoints, "entry_point"); reg(components, "component"); reg(flows, "flow")
  const evidence = new Map<string, Evidence>()
  for (const e of [...map.evidence, ...(overlay?.evidence ?? [])]) evidence.set(e.id, e)
  const interfaces = [...(map.interfaces ?? []), ...(add?.interfaces ?? [])]
  return {
    map, overlay, caps, entities, entryPoints, components, relations, flows, headOnly, kindOf, byId, evidence,
    impact: new Map((overlay?.impacts ?? []).map((i) => [i.target_id, i])),
    flowChange: new Map((overlay?.flow_changes ?? []).map((f) => [f.flow_id, f])),
    semanticAvailable: map.semantic.status === "done",
    domains: [...(map.domains ?? [])].sort((a, b) => a.order - b.order),
    capById: new Map(caps.map((c) => [c.id, c])),
    children: caps.reduce((acc, c) => {
      if (c.parent_capability_id) acc.set(c.parent_capability_id, [...(acc.get(c.parent_capability_id) ?? []), c.id])
      return acc
    }, new Map<string, string[]>()),
    rules: new Map((map.architecture_rules ?? []).map((r) => [r.id, r])),
    archChecks: overlay?.architecture_checks ?? [],
    interfaces, ifaceById: new Map(interfaces.map((i) => [i.id, i])),
  }
}

// ---------- hierarchy (spec §4 System Hierarchy) ----------
export const capabilityOf = (m: Model, id: string): string | undefined => {
  const k = m.kindOf.get(id)
  if (k === "capability") return id
  const e = m.byId.get(id) as { capability_id?: string } | undefined
  return e?.capability_id
}
export const categoryOf = (m: Model, capId: string): Capability["category"] => m.capById.get(capId)?.category ?? "business"
/** Shared foundations and engineering/tooling are hidden by default. */
export const isInfra = (m: Model, capId: string) => categoryOf(m, capId) !== "business"
/** Domain of a business capability (a split child inherits its parent's domain when it has none). */
export function domainOf(m: Model, capId: string): string {
  const c = m.capById.get(capId)
  if (!c) return UNASSIGNED_DOMAIN
  if (c.domain_id) return c.domain_id
  return c.parent_capability_id ? domainOf(m, c.parent_capability_id) : UNASSIGNED_DOMAIN
}
/** The capability itself plus every split descendant. */
export function descendants(m: Model, capId: string): string[] {
  const out = [capId]
  for (const ch of m.children.get(capId) ?? []) out.push(...descendants(m, ch))
  return out
}
/** Parent chain from the top-level capability down to capId. */
export function capPath(m: Model, capId: string): string[] {
  const out: string[] = []
  for (let c = m.capById.get(capId); c; c = c.parent_capability_id ? m.capById.get(c.parent_capability_id) : undefined) out.unshift(c.id)
  return out
}
export const domainName = (m: Model, domId: string): Names => {
  const d = m.domains.find((x) => x.id === domId)
  return d ? namesOf(d) : { primary: domId === UNASSIGNED_DOMAIN ? "未歸類" : domId, business: false }
}

// ---------- names ----------
export interface Names { primary: string; secondary?: string; business: boolean }
/** Business name is primary; the technical name is secondary (only shown when it differs). */
export function namesOf(e: Pick<MapElement, "technical_name" | "business_name">): Names {
  const b = e.business_name?.text
  return b ? { primary: b, secondary: e.technical_name, business: true } : { primary: e.technical_name, business: false }
}
export const nameOfId = (m: Model, id: string): Names => {
  const e = m.byId.get(id)
  return e ? namesOf(e) : { primary: id, business: false }
}

// ---------- status ----------
export function changeOf(m: Model, id: string): ChangeStatus {
  const i = m.impact.get(id)
  if (i) return i.change_status
  return m.headOnly.has(id) ? "added" : "unchanged"
}
export function impactOf(m: Model, id: string): ImpactStatus {
  const i = m.impact.get(id)
  if (i) return i.impact_status
  return m.headOnly.has(id) ? "direct" : "none"
}
/** Entry points inherit the status of the flow they start (flows are not drawn as nodes). */
export function flowOfEntry(m: Model, epId: string): Flow | undefined { return m.flows.find((f) => f.entry_point_id === epId) }
export function nodeStatus(m: Model, id: string): { change: ChangeStatus; impact: ImpactStatus } {
  let change = changeOf(m, id), impact = impactOf(m, id)
  if (m.kindOf.get(id) === "entry_point" && !m.impact.has(id) && !m.headOnly.has(id)) {
    const f = flowOfEntry(m, id)
    if (f) { change = changeOf(m, f.id); impact = impactOf(m, f.id) }
  }
  return { change, impact }
}
export function isChanged(m: Model, id: string): boolean {
  const s = nodeStatus(m, id)
  return s.change !== "unchanged" || s.impact !== "none"
}

/** Is an element drawn in a given map view? Hidden elements keep their layout slot. */
export function visibleInView(m: Model, id: string, view: MapView): boolean {
  const added = m.headOnly.has(id) || m.impact.get(id)?.change_status === "added"
  const removed = m.impact.get(id)?.change_status === "removed"
  switch (view) {
    case "system": return !m.headOnly.has(id)
    case "before": return !added
    case "after": return !removed
    default: return true
  }
}

// ---------- flows & steps ----------
export interface FlowSteps { steps: OverlayStep[]; mode: "animation" | "static_flow"; reason?: string }

/** PR flow_changes win; otherwise the system map's own timeline, every step unchanged. */
export function flowSteps(m: Model, flowId: string): FlowSteps {
  const fc = m.flowChange.get(flowId)
  if (fc) return { steps: [...fc.steps].sort((a, b) => a.order - b.order), mode: fc.mode, reason: fc.mode_reason }
  const f = m.flows.find((x) => x.id === flowId)
  if (!f) return { steps: [], mode: "static_flow" }
  return {
    steps: [...f.timeline.steps].sort((a, b) => a.order - b.order).map((s) => ({ ...s, step_change_status: "unchanged" as const })),
    mode: f.timeline.mode, reason: f.timeline.mode_reason,
  }
}

/** Before view = all but added; after view = all but removed; overlay/side keep everything. */
export function deriveSteps(steps: OverlayStep[], view: FlowView): OverlayStep[] {
  if (view === "before") return steps.filter((s) => s.step_change_status !== "added")
  if (view === "after") return steps.filter((s) => s.step_change_status !== "removed")
  return steps
}
export const stepInView = (s: OverlayStep, view: "before" | "after" | "overlay") => deriveSteps([s], view).length > 0

export interface StepView {
  capabilityId: string
  entityId?: string
  label: string
  fromBusiness: boolean
  technical: string
  status: StepStatus
  evidenceStatus: OverlayStep["evidence_status"]
  branch?: string
  evidenceRefs: string[]
}
/** Which capability/entity a step lights up, and which label is shown (business first, technical fallback). */
export function stepView(s: OverlayStep): StepView {
  const biz = s.business_label?.text
  return {
    capabilityId: s.capability_id, entityId: s.entity_id, label: biz ?? s.technical_label, fromBusiness: !!biz, technical: s.technical_label,
    status: s.step_change_status, evidenceStatus: s.evidence_status, branch: s.branch?.condition, evidenceRefs: s.evidence_refs,
  }
}

export interface FlowItem { flow: Flow; change: ChangeStatus; impact: ImpactStatus; related: boolean }
/** PR-related flows first (spec §4 Flows tab), then alphabetical by displayed name. */
export function flowList(m: Model): FlowItem[] {
  const items = m.flows.map((flow) => {
    const related = m.flowChange.has(flow.id) || m.impact.has(flow.id) || m.headOnly.has(flow.id)
    return { flow, change: changeOf(m, flow.id), impact: impactOf(m, flow.id), related }
  })
  const rank = (i: FlowItem) => (i.change !== "unchanged" ? 0 : i.related ? 1 : 2)
  return items.sort((a, b) => rank(a) - rank(b) || namesOf(a.flow).primary.localeCompare(namesOf(b.flow).primary))
}

// ---------- evidence ----------
export const refRange = (r: CodeRef) => (r.end_line && r.end_line !== r.start_line ? `${r.start_line}-${r.end_line}` : `${r.start_line}`)
export const refText = (r: CodeRef) => `${r.path}:${refRange(r)}`

/** GitHub blob link: base side resolves at merge_base_sha, head side at head_sha (spec §4 Evidence). */
export function githubEvidenceUrl(pr: PrOverlay["pr"] | null | undefined, r: CodeRef): string | null {
  if (!pr || pr.source !== "github" || !pr.repo.owner) return null
  const sha = r.side === "base" ? pr.merge_base_sha : pr.head_sha
  return `https://github.com/${pr.repo.owner}/${pr.repo.name}/blob/${sha}/${r.path}#L${r.start_line}`
}
