/**
 * Flow axis (spec §4 「Flow 呈現（主軸）」): a flow left → right in 3–7 segments — Claude's phases when present, otherwise
 * runs of consecutive steps in one capability (merged down to 7). Steps in shared / engineering capabilities are hidden by
 * default; with a PR, only the changed segments ±1 stay open and the rest collapse (「前面 4 段」). Pure: no React.
 */
import { deriveSteps, flowSteps, isInfra, namesOf, type Model } from "@/entities/artifact/model"
import type { ArchCheck, Finding, FlowView, OverlayStep, StepStatus, Text } from "@/entities/artifact/types"

export const MAX_SEGMENTS = 7
export const STEPS_PER_PAGE = 7

export interface Segment {
  id: string
  label: string
  /** phase label (Claude) or the capability's business name; absent = technical name */
  labelText?: Text
  source: "phase" | "capability"
  /** steps shown (infrastructure steps removed unless switched on) */
  steps: OverlayStep[]
  hiddenSteps: number
  capabilityIds: string[]
  counts: Record<StepStatus, number>
  changed: boolean
  evidence: Record<OverlayStep["evidence_status"], number>
  findings: Finding[]
  checks: ArchCheck[]
}
export interface Axis { flowId: string; source: "phases" | "capabilities"; segments: Segment[]; hiddenSteps: number; totalSteps: number }

function seg(id: string, source: Segment["source"], label: string, labelText?: Text): Segment {
  return { id, source, label, labelText, steps: [], hiddenSteps: 0, capabilityIds: [], counts: { added: 0, removed: 0, changed: 0, unchanged: 0 }, changed: false,
    evidence: { confirmed: 0, possible: 0, unknown: 0 }, findings: [], checks: [] }
}

function finish(m: Model, flowId: string, s: Segment): Segment {
  s.capabilityIds = [...new Set(s.steps.map((x) => x.capability_id))]
  for (const st of s.steps) { s.counts[st.step_change_status]++; s.evidence[st.evidence_status]++ }
  s.changed = s.steps.some((x) => x.step_change_status !== "unchanged")
  const caps = new Set(s.capabilityIds)
  s.findings = (m.overlay?.findings ?? []).filter((f) => (!f.flow_ids?.length || f.flow_ids.includes(flowId)) && f.capability_ids.some((c) => caps.has(c)))
  s.checks = m.archChecks.filter((a) => a.verdict !== "follows" && a.capability_ids.some((c) => caps.has(c)))
  return s
}

const capLabel = (m: Model, capId: string) => { const c = m.capById.get(capId); return { label: c ? namesOf(c).primary : capId, text: c?.business_name } }

export function buildAxis(m: Model, flowId: string, view: FlowView, opts: { showInfra?: boolean } = {}): Axis {
  const all = deriveSteps(flowSteps(m, flowId).steps, view === "side" ? "overlay" : view)
  const hidden = (s: OverlayStep) => !opts.showInfra && isInfra(m, s.capability_id)
  const flow = m.flows.find((f) => f.id === flowId)
  const phases = m.flowChange.get(flowId)?.phases ?? flow?.phases
  let segments: Segment[]
  let source: Axis["source"]
  if (phases?.length) {
    source = "phases"
    const at = new Map<string, number>()
    phases.forEach((p, i) => p.step_ids.forEach((id) => at.set(id, i)))
    const groups = phases.map((p) => seg(p.id, "phase", p.label.text, p.label))
    const used = new Set<number>()
    let prev = 0
    for (const s of all) {
      const i = at.get(s.id) ?? prev // a step the phases do not list stays with the step before it
      prev = i
      used.add(i)
      if (hidden(s)) groups[i].hiddenSteps++
      else groups[i].steps.push(s)
    }
    segments = groups.filter((_, i) => used.has(i))
  } else {
    source = "capabilities"
    const runs: Segment[] = []
    let pendingHidden = 0
    for (const s of all) {
      const cur = runs[runs.length - 1]
      if (hidden(s)) { if (cur) cur.hiddenSteps++; else pendingHidden++; continue }
      if (cur && cur.steps[cur.steps.length - 1].capability_id === s.capability_id) { cur.steps.push(s); continue }
      const l = capLabel(m, s.capability_id)
      const r = seg(`run:${s.id}`, "capability", l.label, l.text)
      r.steps.push(s)
      runs.push(r)
    }
    if (runs[0]) runs[0].hiddenSteps += pendingHidden
    // merge the smallest adjacent pair until ≤ 7, keeping changed runs apart where possible
    const changed = (r: Segment) => r.steps.some((x) => x.step_change_status !== "unchanged")
    while (runs.length > MAX_SEGMENTS) {
      let best = 0, cost = Infinity
      for (let i = 0; i < runs.length - 1; i++) {
        const c = runs[i].steps.length + runs[i + 1].steps.length + (changed(runs[i]) || changed(runs[i + 1]) ? 1000 : 0)
        if (c < cost) { cost = c; best = i }
      }
      const a = runs[best], b = runs[best + 1]
      const labels = [...new Set([...a.label.split(" → "), ...b.label.split(" → ")])]
      runs.splice(best, 2, { ...a, label: labels.length > 2 ? `${labels[0]} → … → ${labels[labels.length - 1]}` : labels.join(" → "), labelText: undefined,
        steps: [...a.steps, ...b.steps], hiddenSteps: a.hiddenSteps + b.hiddenSteps })
    }
    segments = runs
  }
  segments = segments.map((s) => finish(m, flowId, s))
  return { flowId, source, segments, hiddenSteps: segments.reduce((n, s) => n + s.hiddenSteps, 0), totalSteps: all.length }
}

export type AxisItem = { kind: "segment"; seg: Segment; index: number } | { kind: "collapsed"; segments: Segment[]; label: string; from: number }

/** PR focus: changed segments ±1 stay; contiguous others collapse. `keep` (expanded / playing segment) always stays. */
export function focusItems(axis: Axis, prFocus: boolean, keep: (string | null | undefined)[] = []): AxisItem[] {
  const segs = axis.segments
  const changed = segs.map((s, i) => (s.changed ? i : -1)).filter((i) => i >= 0)
  if (!prFocus || !changed.length) return segs.map((seg, index) => ({ kind: "segment", seg, index }))
  const kept = new Set<number>()
  for (const i of changed) for (const j of [i - 1, i, i + 1]) if (j >= 0 && j < segs.length) kept.add(j)
  segs.forEach((s, i) => { if (keep.includes(s.id)) kept.add(i) })
  const out: AxisItem[] = []
  const first = Math.min(...kept), last = Math.max(...kept)
  for (let i = 0; i < segs.length;) {
    if (kept.has(i)) { out.push({ kind: "segment", seg: segs[i], index: i }); i++; continue }
    const from = i
    while (i < segs.length && !kept.has(i)) i++
    const n = i - from
    out.push({ kind: "collapsed", segments: segs.slice(from, i), from, label: from < first ? `前面 ${n} 段` : from > last ? `後面 ${n} 段` : `中間 ${n} 段` })
  }
  return out
}

/** At most 7 steps of a segment at a time; the rest behind 「再顯示 N 步」. */
export function stepPage(s: Segment, page: number): { shown: OverlayStep[]; remaining: number; page: number; pages: number } {
  const pages = Math.max(1, Math.ceil(s.steps.length / STEPS_PER_PAGE)), p = Math.min(Math.max(0, page), pages - 1)
  const shown = s.steps.slice(p * STEPS_PER_PAGE, (p + 1) * STEPS_PER_PAGE)
  return { shown, remaining: Math.max(0, s.steps.length - (p + 1) * STEPS_PER_PAGE), page: p, pages }
}

/** Worst evidence status of a segment → how the map highlights it. */
export const segmentState = (s: Segment): "active" | "possible" | "unknown" => (s.evidence.unknown ? "unknown" : s.evidence.possible ? "possible" : "active")
