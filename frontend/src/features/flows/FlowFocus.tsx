import { useMemo } from "react"
import { DISCLAIMER, branchDelta, nameOfId, namesOf, stepView, type Model } from "@/entities/artifact/model"
import { statusOf } from "@/entities/artifact/provenance"
import type { FlowView, OverlayStep } from "@/entities/artifact/types"
import { highlightOf, useExplorer } from "@/app/store"
import { Chip, StatusBadge } from "@/shared/ui/ui"
import { MapArea } from "@/features/system-map/MapArea"
import { buildScene } from "@/features/system-map/scene"
import { STEP } from "@/features/system-map/style"
import { FlowHeading } from "@/features/changes/FlowName"
import { focusItems, stepPage, type Axis, type Segment } from "./axis"

const VIEW_LABEL: Record<FlowView, string> = { overlay: "疊加", before: "修改前", after: "修改後", side: "並排" }
type Side = "before" | "after" | undefined
const inSide = (s: OverlayStep, side: Side) => !side || (side === "before" ? s.step_change_status !== "added" : s.step_change_status !== "removed")
/** added / removed / changed are labelled by glyph and word, not colour alone. */
export const STEP_GLYPH: Record<OverlayStep["step_change_status"], string> = { added: "+", removed: "−", changed: "~", unchanged: "" }

function StepRow({ s, i, m, prev }: { s: OverlayStep; i: number; m: Model; prev?: OverlayStep }) {
  const ex = useExplorer()
  const p = ex.player
  const v = stepView(s)
  const st = STEP[s.step_change_status]
  const now = p.stepId === s.id
  const delta = branchDelta(v.branch, prev?.branch?.condition)
  return (
    <li className={`step st-${s.step_change_status} ev-${s.evidence_status}${now ? " now" : ""}`} data-testid={`step-${s.id}`} data-status={s.step_change_status}>
      <button onClick={(e) => { p.setStepId(s.id); ex.openDetail({ kind: "step", id: s.id }, e.currentTarget) }} aria-current={now ? "step" : undefined}>
        <span className="n">{i + 1}</span>
        <span className="grow">
          <span className="lbl">{v.label}</span>
          {v.fromBusiness && <code className="tech small"> {v.technical}</code>}
          <span className="muted small"> · {nameOfId(m, s.capability_id).primary}{s.entity_id ? ` / ${nameOfId(m, s.entity_id).primary}` : ""}</span>
          {v.branch && <span className="small branch" title={v.branch}> 條件：{delta || "同上"}</span>}
        </span>
        {s.step_change_status !== "unchanged" && <span className="chip-tag" style={{ background: st.bg, color: st.color, borderColor: st.color }}>{STEP_GLYPH[s.step_change_status]} {st.label}</span>}
        {s.evidence_status !== "confirmed" && <StatusBadge s={statusOf({ step: s })} />}
      </button>
    </li>
  )
}

/** Above the axis: what changed and what review items concern this segment. */
function Above({ s, side }: { s: Segment; side: Side }) {
  const steps = s.steps.filter((x) => inSide(x, side))
  const counts = { added: 0, removed: 0, changed: 0 }
  for (const x of steps) if (x.step_change_status !== "unchanged") counts[x.step_change_status]++
  const any = counts.added + counts.removed + counts.changed + s.findings.length + s.checks.length
  return (
    <div className="ax-above" data-testid={`above-${s.id}`}>
      {(["added", "removed", "changed"] as const).map((k) => counts[k] ? <span key={k} className="chip-tag" style={{ background: STEP[k].bg, color: STEP[k].color, borderColor: STEP[k].color }}>{STEP_GLYPH[k]} {STEP[k].label} {counts[k]}</span> : null)}
      {s.findings.length > 0 && <span className="ax-risk" title={s.findings.map((f) => f.title).join("\n")}><Chip tone="red">待確認事項 {s.findings.length}</Chip></span>}
      {s.checks.length > 0 && <span className="ax-risk" title={s.checks.map((c) => c.title).join("\n")}><Chip tone="purple">架構檢查 {s.checks.length}</Chip></span>}
      {!any && <span className="muted tiny">沒有改動</span>}
    </div>
  )
}

/** Below the axis: how it is done, and how far each step is established. */
function Below({ s, side, m }: { s: Segment; side: Side; m: Model }) {
  const steps = s.steps.filter((x) => inSide(x, side))
  const how = steps.slice(0, 2).map((x) => stepView(x).label).join(" → ") + (steps.length > 2 ? " …" : "")
  const capsLine = s.source === "phase" ? s.capabilityIds.slice(0, 3).map((id) => nameOfId(m, id).primary).join("、") + (s.capabilityIds.length > 3 ? "…" : "") : ""
  return (
    <div className="ax-below" data-testid={`below-${s.id}`}>
      <span className="small">{how || <span className="muted">（沒有可顯示的步驟）</span>}</span>
      {capsLine && <span className="muted tiny">{capsLine}</span>}
      <span className="tiny">{steps.length} 步
        {s.evidence.confirmed > 0 && <> · ✓{s.evidence.confirmed}</>}
        {s.evidence.possible > 0 && <> · ?待確認 {s.evidence.possible}</>}
        {s.evidence.unknown > 0 && <> · –無法判定 {s.evidence.unknown}</>}
        {s.hiddenSteps > 0 && <span className="muted">　＋{s.hiddenSteps} 個基礎步驟（隱藏）</span>}
      </span>
    </div>
  )
}

function AxisView({ axis, side, title }: { axis: Axis; side?: "before" | "after"; title?: string }) {
  const ex = useExplorer()
  const m = ex.model, p = ex.player
  const items = focusItems(axis, ex.prFocus && !!m.overlay, [ex.expandedSeg, p.segment?.id])
  return (
    <div className="axis-wrap">
      {title && <h5>{title}</h5>}
      <div className="axis" role="list" aria-label="流程主軸" data-testid={`axis${side ? "-" + side : ""}`}>
        {items.map((it) => it.kind === "collapsed" ? (
          <button key={`c${it.from}`} role="listitem" className="ax-collapsed" data-testid="axis-collapsed" onClick={() => ex.setExpandedSeg(it.segments[0].id)}
            title={it.segments.map((s) => s.label).join("、")}>{it.label}</button>
        ) : (
          <div key={it.seg.id} role="listitem" className={"ax-col" + (it.seg.changed ? " changed" : "")} data-testid={`seg-${it.seg.id}`}>
            <Above s={it.seg} side={side} />
            <button className={"ax-seg" + (ex.expandedSeg === it.seg.id ? " open" : "") + (p.segment?.id === it.seg.id ? " now" : "")} aria-expanded={ex.expandedSeg === it.seg.id}
              onClick={() => (ex.expandedSeg === it.seg.id ? ex.setExpandedSeg(null) : ex.setExpandedSeg(it.seg.id))}>
              <span className="n">{it.index + 1}</span> {it.seg.label}
            </button>
            <Below s={it.seg} side={side} m={m} />
          </div>
        ))}
      </div>
    </div>
  )
}

function Expanded({ axis }: { axis: Axis }) {
  const ex = useExplorer()
  const s = axis.segments.find((x) => x.id === ex.expandedSeg)
  if (!s) return <p className="muted small">點一段主軸，看這一段的步驟（一次一段、最多 7 步）。</p>
  const pg = stepPage(s, ex.segPage)
  const biz = s.steps.filter((x) => x.business_label).length, tech = s.steps.length - biz
  return (
    <div className="seg-detail" data-testid="seg-detail">
      <div className="row wrap"><b>{s.label}</b><span className="muted small">第 {pg.page * 7 + 1}–{pg.page * 7 + pg.shown.length} 步，共 {s.steps.length} 步</span>
        <span className="source-note">{s.source === "phase" ? "分段：AI 說明" : "分段：依功能（靜態分析）"}；步驟名稱：{biz && tech ? `AI 說明 ${biz} 步、靜態分析（技術名稱）${tech} 步` : biz ? "AI 說明" : "靜態分析（技術名稱）"}</span></div>
      <ol className="events">{pg.shown.map((x, i) => <StepRow key={x.id} s={x} i={pg.page * 7 + i} m={ex.model} prev={s.steps[pg.page * 7 + i - 1]} />)}</ol>
      <div className="row">
        {pg.page > 0 && <button className="mini" onClick={() => ex.setSegPage(pg.page - 1)}>上一頁</button>}
        {pg.remaining > 0 && <button className="mini" data-testid="more-steps" onClick={() => ex.setSegPage(pg.page + 1)}>再顯示 {Math.min(7, pg.remaining)} 步（還有 {pg.remaining} 步）</button>}
      </div>
    </div>
  )
}

/** The flow map with the animation highlight: optional, opened on demand. */
function FlowMap() {
  const ex = useExplorer()
  const m = ex.model, p = ex.player
  const scene = useMemo(() => buildScene(m, { mode: "flow", flowId: ex.flowId ?? undefined, showShared: ex.showShared, showEngineering: ex.showEngineering }),
    [m, ex.flowId, ex.showShared, ex.showEngineering])
  const single = !m.overlay ? ("system" as const) : ex.flowView === "side" ? ("overlay" as const) : ex.flowView
  const views = ex.flowView === "side" && m.overlay
    ? [{ view: "before" as const, title: "修改前", highlight: highlightOf(p, "before") }, { view: "after" as const, title: "修改後", highlight: highlightOf(p, "after") }]
    : [{ view: single, highlight: highlightOf(p) }]
  return <div className="flow-map"><MapArea scene={scene} views={views} /></div>
}

/**
 * One flow, inline where it was asked for (plan「變更與影響」): why it is related, then the 3–7 segment axis with
 * ≤ 7 steps per expanded segment; animation and the map are optional.
 */
export function FlowFocus({ why }: { why?: string }) {
  const ex = useExplorer()
  const m = ex.model, p = ex.player
  const flow = ex.flowId ? m.flows.find((f) => f.id === ex.flowId) : undefined
  if (!flow || !p.axis) return null
  const views: FlowView[] = m.overlay ? ["overlay", "before", "after", "side"] : ["overlay"]
  const animated = p.mode === "animation"
  const n = p.axis.segments.length
  const atEnd = p.index >= n - 1
  const mapOpen = ex.expanded.has(`flowmap:${flow.id}`)
  return (
    <section id="flow-focus" tabIndex={-1} className="flow-focus" data-testid="flow-focus" aria-labelledby="flow-title">
      <h3 id="flow-title"><FlowHeading n={namesOf(flow)} /> <span className="kind-note">流程：可辨識的步驟順序</span></h3>
      {why && <p className="why" data-testid="flow-why"><b>為什麼相關：</b>{why}</p>}
      <div className="row wrap">
        {m.overlay && (
          <div className="seg" role="group" aria-label="檢視方式">
            {views.map((v) => <button key={v} className={ex.flowView === v ? "on" : ""} aria-pressed={ex.flowView === v} onClick={() => ex.setFlowView(v)}>{VIEW_LABEL[v]}</button>)}
          </div>
        )}
        <span className="small">
          {p.axis.source === "phases" ? <Chip tone="purple" title="AI 只切分既有步驟的順序">由 Claude 分段</Chip> : <Chip tone="gray" title="沒有 AI 分段時，同一功能內的連續步驟合併成一段">依能力分段</Chip>}
          {" "}{n} 段 · {p.axis.totalSteps} 步
        </span>
        {m.overlay && <label className="small"><input type="checkbox" checked={ex.prFocus} onChange={(e) => ex.setPrFocus(e.target.checked)} />聚焦 PR 改到的段落</label>}
        <label className="small" title="權限檢查、記錄、工具函式等落在共用基礎與工程與工具的步驟"><input type="checkbox" checked={ex.showInfraSteps} onChange={(e) => ex.setShowInfraSteps(e.target.checked)} />
          顯示基礎步驟{!ex.showInfraSteps && p.axis.hiddenSteps ? `（已隱藏 ${p.axis.hiddenSteps}）` : ""}</label>
      </div>
      <p className="disclaimer small" role="note">⚠ {DISCLAIMER}。標示「待確認」或「無法判定」的步驟尚未被證實。</p>
      {ex.flowView === "side" ? (
        <div className="compare-axes"><AxisView axis={p.axis} side="before" title="修改前" /><AxisView axis={p.axis} side="after" title="修改後" /></div>
      ) : <AxisView axis={p.axis} />}
      <Expanded axis={p.axis} />
      <div className="row wrap">
        <button aria-expanded={mapOpen} onClick={() => ex.toggle(`flowmap:${flow.id}`)}>{mapOpen ? "收起流程地圖" : "在地圖上看這條流程（含動畫）"}</button>
        {mapOpen && (animated ? (
          <span className="controls row" role="group" aria-label="動畫控制">
            <button onClick={() => p.setIndex(Math.max(0, p.index - 1))} disabled={p.index === 0 || !n} aria-label="上一段">◀</button>
            <button className="primary" onClick={() => (atEnd && p.active && !p.playing ? p.replay() : p.setPlaying(!p.playing))} disabled={!n}>{p.playing ? "暫停" : atEnd && p.active ? "重播" : "逐段播放"}</button>
            <button onClick={() => p.setIndex(Math.min(n - 1, p.index + 1))} disabled={atEnd && p.active} aria-label="下一段">▶</button>
            <label className="small">速度 <select value={p.speed} onChange={(e) => p.setSpeed(Number(e.target.value))}>{[0.5, 1, 2, 4].map((s) => <option key={s} value={s}>{s}×</option>)}</select></label>
          </span>
        ) : <Chip tone="gray" title={p.reason}>靜態流程圖，無動畫{p.reason ? `：${p.reason}` : ""}</Chip>)}
      </div>
      {mapOpen && <FlowMap />}
      {p.segment && !p.step && <p className="small muted" data-testid="current-segment">目前段落 {p.index + 1}/{n}：{p.segment.label}</p>}
    </section>
  )
}
