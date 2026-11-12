import { useEffect, useRef } from "react"
import { Capped } from "./Capped"
import {
  descendants, domainName, flowOfEntry, flowSteps, isChanged, namesOf, nameOfId, nodeStatus, stepView, type Model, type StepView,
} from "@/entities/artifact/model"
import { statusOf } from "@/entities/artifact/provenance"
import type { Capability, Component, Entity, EntryPoint, Flow, MapElement } from "@/entities/artifact/types"
import { useExplorer, type Selection } from "@/app/store"
import { Chip, RefRow, SourceNote, StatusBadge, TextBlock } from "@/shared/ui/ui"
import { isStaticBehavior } from "@/features/changes/cards"
import { CHANGE, KIND_LABEL, STEP } from "@/features/system-map/style"
import { SHARED_BUCKET, ENGINEERING_BUCKET } from "@/features/system-map/scene"
import { ArchCard, RingChip } from "@/features/architecture/Architecture"
import { CHANGE_LABEL, interfacesOf, KIND_LABEL as IFACE_KIND, RING_ORDER } from "@/features/architecture/interfaces"
import { FindingCard } from "@/features/review/ReviewCard"
import { IMPACT_SENTENCE, relationSentence } from "@/features/relations/wording"
import { STEP_GLYPH } from "@/features/flows/FlowFocus"

/** Change + impact of an element in words (impact never phrased as a certain business effect). */
function StatusLine({ m, id }: { m: Model; id: string }) {
  if (!m.overlay) return null
  const s = nodeStatus(m, id)
  if (s.change === "unchanged" && s.impact === "none") return <p className="small muted">這個 PR 沒有修改或影響它。</p>
  return (
    <p className="small" data-testid="detail-status">
      {s.change !== "unchanged" && <span className="chip-tag" style={{ background: CHANGE[s.change].top, borderColor: CHANGE[s.change].stroke }}>{CHANGE[s.change].label}</span>}
      {s.impact !== "none" && <> {IMPACT_SENTENCE[s.impact]} <StatusBadge s={statusOf({ impact: s.impact })} /></>}
    </p>
  )
}

/** Inside the panel a reference replaces the panel's content (one level, never a second panel). */
function ElementChip({ id, m }: { id: string; m: Model }) {
  const ex = useExplorer()
  return <button className="chip-btn" onClick={() => ex.selectElement(id)}>{nameOfId(m, id).primary}</button>
}

export function StepCard({ c, index, total, m, segment }: { c: StepView; index: number; total: number; m: Model; segment?: string }) {
  const st = STEP[c.status]
  return (
    <section className={"current " + c.evidenceStatus} aria-label="目前步驟" data-testid="current-step">
      <p className="muted small">{segment ? `「${segment}」` : ""}第 {index + 1}/{total} 步</p>
      <p className="step-label"><b>{c.label}</b> <SourceNote source={c.fromBusiness ? "semantic" : "static"} /> {!c.fromBusiness && <Chip tone="gray" title="沒有業務說明，顯示技術名稱">技術名稱</Chip>}</p>
      {c.fromBusiness && <code className="tech">{c.technical}</code>}
      <p className="small">
        <span className="chip-tag" style={{ background: st.bg, color: st.color, borderColor: st.color }}>{STEP_GLYPH[c.status]} {st.label}</span>{" "}
        <StatusBadge s={statusOf({ step: { evidence_status: c.evidenceStatus } })} />
        {" "}<ElementChip id={c.capabilityId} m={m} />{c.entityId && <ElementChip id={c.entityId} m={m} />}
      </p>
      {c.branch && <p className="small">條件分支：<code>{c.branch}</code>（未確認，路徑待確認）</p>}
      <p className="disclaimer small">由靜態分析重建，不是實際執行紀錄</p>
    </section>
  )
}

function StepDetail({ id }: { id: string }) {
  const { player: p, model: m } = useExplorer()
  const seg = p.axis?.segments.find((s) => s.steps.some((x) => x.id === id))
  const step = seg?.steps.find((x) => x.id === id)
  if (!seg || !step) return <p className="muted">這一步不在目前的流程主軸上。</p>
  return <StepCard c={stepView(step)} index={seg.steps.indexOf(step)} total={seg.steps.length} m={m} segment={seg.label} />
}

function DomainDetail({ id }: { id: string }) {
  const m = useExplorer().model
  const d = m.domains.find((x) => x.id === id)
  const isBucket = id === SHARED_BUCKET || id === ENGINEERING_BUCKET
  const caps = m.caps.filter((c) => (isBucket ? c.category === (id === SHARED_BUCKET ? "shared" : "engineering") : c.category === "business" && (c.domain_id === id)) && !c.parent_capability_id)
  return (
    <div>
      <p className="muted small">{isBucket ? "預設隱藏的類別" : "業務領域"}{d && <> · {d.source === "semantic" ? "由 AI 歸類（只把既有功能放進領域，不拆分或新增）" : "依頂層目錄分組（靜態分析）"}</>}</p>
      {d && <TextBlock t={d.description} fallback="沒有業務說明。" />}
      <h4>功能（{caps.length}）</h4>
      <ul className="plain small">
        <Capped id={`dom:${id}`} items={caps} limit={20} render={(c: Capability) => (
          <li key={c.id}><ElementChip id={c.id} m={m} /> {isChanged(m, c.id) && <span className="muted">（與這個 PR 有關）</span>}
            {(m.children.get(c.id) ?? []).length > 0 && <span className="muted"> 子功能：{(m.children.get(c.id) ?? []).map((k) => <ElementChip key={k} id={k} m={m} />)}</span>}</li>
        )} />
      </ul>
    </div>
  )
}

/** Interfaces of a capability, grouped 外圈 → 中圈, with the change this PR makes to each. */
function CapInterfaces({ id }: { id: string }) {
  const m = useExplorer().model
  const list = interfacesOf(m, id)
  if (!list.length) return null
  const change = (ifid: string) => m.archChecks.find((a) => a.interface_change?.interface_id === ifid)
  return (
    <div data-testid="cap-interfaces">
      <h4>介面（{list.length}）</h4>
      {RING_ORDER.map((r) => {
        const items = list.filter((i) => i.ring === r)
        if (!items.length) return null
        return (
          <div key={r} className="small"><RingChip ring={r} />
            <ul className="plain">{items.map((i) => {
              const c = change(i.id)
              return <li key={i.id}><b>{namesOf(i).primary}</b> <code className="tech">{namesOf(i).secondary ?? i.technical_name}</code> <Chip tone="blue">{IFACE_KIND[i.kind]}</Chip>
                {c?.interface_change && <> <Chip tone={c.interface_change.compatibility === "breaking" ? "red" : "amber"}>PR {CHANGE_LABEL[c.interface_change.change]}</Chip></>}</li>
            })}</ul></div>
        )
      })}
    </div>
  )
}

/** Flows / entities / entry points / components / relations of a capability, PR-related first (spec §4). */
function CapabilityLists({ c }: { c: Capability }) {
  const ex = useExplorer()
  const m = ex.model
  const prFirst = <T extends { id: string }>(xs: T[]) => [...xs].sort((a, b) => Number(isChanged(m, b.id)) - Number(isChanged(m, a.id)))
  const flows = prFirst(m.flows.filter((f) => f.capability_ids.includes(c.id)))
  const ents = prFirst(m.entities.filter((e) => e.capability_id === c.id))
  const eps = prFirst(m.entryPoints.filter((e) => e.capability_id === c.id))
  const comps = m.components.filter((x) => x.capability_id === c.id)
  const byKind = new Map<string, EntryPoint[]>()
  for (const e of eps) byKind.set(e.kind, [...(byKind.get(e.kind) ?? []), e])
  const kids = m.children.get(c.id) ?? []
  const rels = m.relations.filter((r) => r.source === c.id || r.target === c.id).sort((a, b) => b.weight - a.weight)
  const flowBtn = (f: Flow) => <button key={f.id} className="chip-btn" onClick={() => ex.go({ nav: "changes", flowId: f.id, card: null, cardView: null }, "flow-focus")}>{namesOf(f).primary}{isChanged(m, f.id) ? "（與 PR 有關）" : ""}</button>
  return (
    <div className="cap-lists">
      {kids.length > 0 && <><h4>子能力（{kids.length}）<span className="muted small"> 入口過多，依子目錄拆開</span></h4><p>{kids.map((k) => <ElementChip key={k} id={k} m={m} />)}</p></>}
      <CapInterfaces id={c.id} />
      <h4>業務流程（{flows.length}）</h4>
      <p>{flows.length ? <Capped id={`flows:${c.id}`} items={flows} render={flowBtn} /> : <span className="muted small">沒有已建立的流程</span>}</p>
      <h4>核心資料（{ents.length}）</h4>
      <p>{ents.length ? <Capped id={`ents:${c.id}`} items={ents} render={(e: Entity) => <ElementChip key={e.id} id={e.id} m={m} />} /> : <span className="muted small">—</span>}</p>
      <h4>入口（{eps.length}）</h4>
      {eps.length === 0 && <p className="muted small">—</p>}
      {[...byKind].map(([k, list]) => (
        <div key={k} className="small"><span className="muted">{k}（{list.length}）</span> <Capped id={`eps:${c.id}:${k}`} items={list} limit={8} render={(e: EntryPoint) => <ElementChip key={e.id} id={e.id} m={m} />} /></div>
      ))}
      <h4>技術元件（{comps.length || c.component_count || 0}）</h4>
      {comps.length ? <ul className="plain small"><Capped id={`comps:${c.id}`} items={comps} limit={8} render={(x: Component) => (
        <li key={x.id}><code className="tech">{x.technical_name}</code> {x.description?.text} {x.refs[0] ? <RefRow r={x.refs[0]} ov={m.overlay} /> : <span className="muted">外部相依</span>}</li>
      )} /></ul> : <p className="muted small">{c.component_count ? "這份分析沒有列出元件明細。" : "—"}</p>}
      <p className="muted small">技術路徑：<code>{c.path}</code></p>
      <h4>依賴關係（{rels.length}）<span className="muted small"> 靜態程式依賴，不代表執行順序</span></h4>
      <ul className="plain small"><Capped id={`rels:${c.id}`} items={rels} limit={10} render={(r: Model["relations"][number]) => (
        <li key={r.id}>{relationSentence(nameOfId(m, r.source).primary, nameOfId(m, r.target).primary, [r.kind])}<span className="muted">（{r.weight} 項依據{r.confidence === "unknown" ? "；未確認" : ""}）</span></li>
      )} /></ul>
    </div>
  )
}

function ElementDetail({ el, kind }: { el: MapElement; kind: "capability" | "entity" | "entry_point" | "component" | "flow" }) {
  const ex = useExplorer()
  const m = ex.model
  const n = namesOf(el)
  const imp = m.impact.get(el.id)
  const capId = kind === "capability" ? el.id : (el as Entity).capability_id
  const fl = kind === "entry_point" ? flowOfEntry(m, el.id) : undefined
  const flow: Flow | undefined = fl ?? (kind === "flow" ? (el as Flow) : undefined)
  const cat = kind === "capability" ? (el as Capability).category : null
  const checks = kind === "capability" ? m.archChecks.filter((a) => a.capability_ids.some((x) => descendants(m, el.id).includes(x))) : []
  const bcs = (m.overlay?.behavior_changes ?? []).filter((b) => !isStaticBehavior(b)).filter((b) => b.capability_ids.includes(el.id) || b.flow_ids.includes(el.id) || (!!fl && b.flow_ids.includes(fl.id)))
  return (
    <div>
      <p className="muted small">{KIND_LABEL[kind]}{cat && cat !== "business" ? `（${cat === "shared" ? "共用基礎" : "工程與工具"}）` : ""}{kind !== "capability" && capId ? <> · 所屬 <ElementChip id={capId} m={m} /></> : null}
        {kind === "capability" && (el as Capability).parent_capability_id ? <> · 上層 <ElementChip id={(el as Capability).parent_capability_id!} m={m} /></> : null}
        {" · "}名稱：{n.business ? <SourceNote source={el.business_name!.source} prefix="" /> : "技術名稱"}</p>
      {n.secondary && <code className="tech">{n.secondary}</code>}
      <StatusLine m={m} id={el.id} />
      {imp?.impact_depth ? <p className="small muted">影響分析的距離：{imp.impact_depth} 層（只代表程式依賴的層數）</p> : null}
      <TextBlock t={el.description} fallback="沒有業務說明（業務說明不可用或被證據檢查丟棄）。" />
      {flow && <p><button className="primary" onClick={() => ex.go({ nav: "changes", flowId: flow.id, card: null, cardView: null }, "flow-focus")}>查看流程：{namesOf(flow).primary}</button></p>}
      {kind === "flow" && <p className="small">經過：{(el as Flow).capability_ids.map((id) => <ElementChip key={id} id={id} m={m} />)}</p>}
      {bcs.length > 0 && <div><h4>這個 PR 的行為差異</h4>{bcs.map((b) => (
        <div key={b.id} className="ba"><div><span className="muted small">修改前</span><TextBlock t={b.before} fallback="（無對應的修改前說明）" /></div><div><span className="muted small">修改後</span><TextBlock t={b.after} /></div></div>
      ))}</div>}
      {kind === "entry_point" && <p className="small">入口類型：<code>{(el as EntryPoint).kind}</code></p>}
      {checks.length > 0 && <div><h4>審查重點（{checks.length}）</h4><ul className="plain small">{checks.map((a) => (
        <li key={a.id}><StatusBadge s={statusOf({ arch: a })} /> <button className="link" onClick={() => ex.go({ nav: "review", card: a.id }, `review-${a.id.replace(/[^\w-]/g, "_")}`)}>{a.title}</button></li>
      ))}</ul></div>}
      {kind === "capability" && <CapabilityLists c={el as Capability} />}
    </div>
  )
}

/** Title of the panel = the current selection. */
export function selectionTitle(m: Model, s: Selection): string {
  if (s.kind === "finding") return m.overlay?.findings.find((f) => f.id === s.id)?.title ?? s.id
  if (s.kind === "arch") return m.archChecks.find((a) => a.id === s.id)?.title ?? s.id
  if (s.kind === "domain") return s.id === SHARED_BUCKET ? "共用基礎" : s.id === ENGINEERING_BUCKET ? "工程與工具" : domainName(m, s.id).primary
  if (s.kind === "step") {
    for (const f of m.flows) { const st = flowSteps(m, f.id).steps.find((x) => x.id === s.id); if (st) return stepView(st).label }
    return "流程步驟"
  }
  return nameOfId(m, s.id).primary
}

/**
 * On-demand detail panel (plan「詳細說明與返回操作」): beside the content on wide screens, a closable overlay on
 * narrower laptops. One level only; Esc / 關閉 restores focus to the trigger and the reading position.
 */
export function DetailPanel() {
  const ex = useExplorer()
  const m = ex.model
  const sel = ex.selection
  const head = useRef<HTMLHeadingElement>(null)
  const open = ex.panel && !!sel
  useEffect(() => { if (open) head.current?.focus({ preventScroll: true }) }, [open, sel?.kind, sel?.id])
  if (!open || !sel) return null
  const el = m.byId.get(sel.id)
  const finding = sel.kind === "finding" ? m.overlay?.findings.find((f) => f.id === sel.id) : undefined
  const arch = sel.kind === "arch" ? m.archChecks.find((a) => a.id === sel.id) : undefined
  const kindLabel = sel.kind === "finding" ? "待確認事項" : sel.kind === "arch" ? "審查重點" : sel.kind === "domain" ? "業務領域" : sel.kind === "step" ? "流程步驟" : KIND_LABEL[sel.kind]
  return (
    <aside className="detail-panel" aria-labelledby="detail-title" data-testid="detail-panel" onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); ex.closeDetail() } }}>
      <header className="dp-head">
        <div><p className="muted small dp-kind">{kindLabel}</p><h2 id="detail-title" ref={head} tabIndex={-1}>{selectionTitle(m, sel)}</h2></div>
        <button className="dp-close" onClick={ex.closeDetail} aria-label="關閉說明面板">關閉 ✕</button>
      </header>
      <div className="dp-body">
        {sel.kind === "step" ? <StepDetail id={sel.id} />
          : el && m.kindOf.has(sel.id) ? <ElementDetail el={el} kind={m.kindOf.get(sel.id)!} />
          : finding ? <FindingCard f={finding} embedded />
          : arch ? <ArchCard c={arch} embedded />
          : sel.kind === "domain" ? <DomainDetail id={sel.id} />
          : <p className="muted">找不到這個項目。</p>}
      </div>
    </aside>
  )
}
