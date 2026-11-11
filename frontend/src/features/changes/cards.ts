/**
 * Change cards (plan「變更與影響」): the concrete changes a reader starts from. Pure, built only from the overlay:
 *   behavior — one per behavior_changes item, in the artifact's order (that order is not an importance ranking);
 *   flow     — a changed flow no behavior change covers (e.g. without Claude): its step counts are static facts;
 *   tech     — changed files of a capability no flow card covers, or of no capability at all (CI, config).
 */
import { flowSteps, namesOf, type Model } from "@/entities/artifact/model"
import type { ArchCheck, BehaviorChange, ChangedFile, FlowChange, StepStatus } from "@/entities/artifact/types"
import { interfaceChanges } from "@/features/architecture/interfaces"

export type ChangeCard =
  | { kind: "behavior"; id: string; bc: BehaviorChange; flowIds: string[]; capIds: string[] }
  | { kind: "flow"; id: string; flowId: string; fc: FlowChange; counts: Record<StepStatus, number>; capIds: string[]; flowIds: string[] }
  | { kind: "tech"; id: string; capId: string | null; files: ChangedFile[]; interfaces: ArchCheck[]; capIds: string[]; flowIds: string[] }

export const cardDomId = (id: string) => `card-${id.replace(/[^\w-]/g, "_")}`

export function stepCounts(steps: { step_change_status: StepStatus }[]): Record<StepStatus, number> {
  const c: Record<StepStatus, number> = { added: 0, removed: 0, changed: 0, unchanged: 0 }
  for (const s of steps) c[s.step_change_status]++
  return c
}

export function changeCards(m: Model): ChangeCard[] {
  const ov = m.overlay
  if (!ov) return []
  const flowExists = (id: string) => m.flows.some((f) => f.id === id)
  const out: ChangeCard[] = ov.behavior_changes.map((bc) => ({ kind: "behavior", id: bc.id, bc, flowIds: bc.flow_ids.filter(flowExists), capIds: bc.capability_ids }))
  const inBehavior = new Set(ov.behavior_changes.flatMap((b) => b.flow_ids))
  for (const fc of ov.flow_changes) {
    if (inBehavior.has(fc.flow_id) || !flowExists(fc.flow_id)) continue
    const steps = flowSteps(m, fc.flow_id).steps
    out.push({ kind: "flow", id: `fc:${fc.flow_id}`, flowId: fc.flow_id, fc, counts: stepCounts(steps), capIds: [...new Set(steps.map((s) => s.capability_id))], flowIds: [fc.flow_id] })
  }
  const covered = new Set(out.flatMap((c) => c.kind === "behavior" ? [...c.capIds, ...c.flowIds.flatMap((f) => flowSteps(m, f).steps.map((s) => s.capability_id))] : c.capIds))
  const ifaces = interfaceChanges(m.archChecks)
  const byCap = new Map<string | null, ChangedFile[]>()
  for (const f of ov.changes.files) {
    const cap = f.capability_id && m.capById.has(f.capability_id) ? f.capability_id : null
    if (cap && covered.has(cap)) continue
    byCap.set(cap, [...(byCap.get(cap) ?? []), f])
  }
  const keys = [...byCap.keys()].sort((a, b) => (a === null ? 1 : b === null ? -1 : namesOf(m.capById.get(a)!).primary.localeCompare(namesOf(m.capById.get(b)!).primary)))
  for (const cap of keys) {
    const ownIfaces = cap ? ifaces.filter((c) => m.ifaceById.get(c.interface_change!.interface_id)?.capability_id === cap) : []
    out.push({ kind: "tech", id: `tech:${cap ?? "none"}`, capId: cap, files: byCap.get(cap)!, interfaces: ownIfaces, capIds: cap ? [cap] : [], flowIds: [] })
  }
  return out
}

/** Why a behavior change has no 修改前: a structural fact when all its flows are new, otherwise undetermined. */
export function missingBefore(m: Model, bc: BehaviorChange): "new_flow" | "undetermined" | null {
  if (bc.before) return null
  const fcs = bc.flow_ids.map((id) => m.flowChange.get(id))
  return fcs.length > 0 && fcs.every((f) => f?.change_status === "added") ? "new_flow" : "undetermined"
}

export const STEP_COUNT_LABEL: Record<Exclude<StepStatus, "unchanged">, string> = { added: "新增", removed: "移除", changed: "改變" }
export const FILE_STATUS: Record<ChangedFile["status"], string> = { added: "新增", modified: "修改", removed: "移除", renamed: "改名" }

/** 「結帳」→「結帳流程」, but 「結帳流程」 stays as is. */
export const flowTitle = (name: string) => (name.endsWith("流程") ? name : `${name}流程`)
