import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react"
import { capabilityOf, type Model } from "@/entities/artifact/model"
import type { ElementKind, FlowView, OverlayStep, WalkTarget } from "@/entities/artifact/types"
import { buildScene, type Level, type Scene } from "@/features/system-map/scene"
import { buildAxis, segmentState, type Axis, type Segment } from "@/features/flows/axis"
import { changeCards } from "@/features/changes/cards"

export type Phase = "system" | "overlay"
/** The one main navigation (plan「主導覽與閱讀順序」). */
export type Nav = "summary" | "changes" | "review" | "context"
export const NAV: { key: Nav; label: string; question: string }[] = [
  { key: "summary", label: "PR 摘要", question: "這次為什麼改，主要改變是什麼？" },
  { key: "changes", label: "變更與影響", question: "哪些行為改變，與哪些功能有關？" },
  { key: "review", label: "審查重點", question: "我需要進一步確認哪些問題？" },
  { key: "context", label: "專案背景", question: "這個系統與各項功能如何運作？" },
]
const isNav = (s: string): s is Nav => NAV.some((n) => n.key === s)
export type Selection = { kind: ElementKind | "finding" | "arch" | "domain" | "step"; id: string }
/** What a change card shows under itself: one of its flows, or the local relation view. */
export type CardView = "flow" | "relations" | null
export type Layers = { findings: boolean }
/** What the map lights up for the animation: the current segment (or one step of it) and what was passed. */
export interface Highlight { current: { ids: string[]; state: "active" | "possible" | "unknown" } | null; visited: string[] }

export interface Player {
  axis: Axis | null
  mode: "animation" | "static_flow"
  reason?: string
  /** index into axis.segments */
  index: number; setIndex: (i: number) => void
  playing: boolean; setPlaying: (b: boolean) => void
  speed: number; setSpeed: (n: number) => void
  active: boolean
  segment: Segment | null
  /** a single step picked inside the expanded segment */
  stepId: string | null; setStepId: (id: string | null) => void
  step: OverlayStep | null
  replay: () => void
}

/** What browser back / forward restores (plan「詳細說明與返回操作」): entry, source card, expanded blocks, scroll. */
export interface ViewSnap {
  nav: Nav
  /** selected change card (behavior change / flow change / capability) */
  card: string | null
  cardView: CardView
  flowId: string | null
  /** expanded flow segment */
  seg: string | null
  /** ids of expanded disclosure blocks */
  expanded: string[]
  selection: Selection | null
  /** the on-demand detail panel is open (showing `selection`) */
  panel: boolean
  guideStep: number
  phase: Phase
  level: Level
  scroll: number
}
type HistState = { prx?: { id: string; snap: ViewSnap } }

export interface Explorer {
  model: Model
  fixture: boolean
  nav: Nav
  /** explicit navigation: a new history entry; `anchor` is the element id to bring into view and focus */
  go: (to: Partial<ViewSnap> & { nav: Nav }, anchor?: string) => void
  /** the main scroll container (its offset is saved per history entry) */
  scrollRef: RefObject<HTMLElement | null>
  card: string | null; setCard: (id: string | null) => void
  cardView: CardView
  /** the on-demand detail panel: one level only; closing restores focus to `trigger` and the reading position */
  panel: boolean
  openDetail: (s: Selection, trigger?: HTMLElement | null, card?: string) => void
  closeDetail: () => void
  /** the 「問 AI」 panel: shares the detail panel's place (one layer: opening one closes the other), focus and reading position */
  ask: boolean
  openAsk: (trigger?: HTMLElement | null) => void
  closeAsk: () => void
  expanded: Set<string>; toggle: (id: string, open?: boolean) => void
  phase: Phase; setPhase: (p: Phase) => void
  selection: Selection | null; select: (s: Selection | null) => void
  selectElement: (id: string) => void
  /** map level: 系統 › 領域 › 能力 */
  level: Level; setLevel: (l: Level) => void
  showShared: boolean; setShowShared: (b: boolean) => void
  showEngineering: boolean; setShowEngineering: (b: boolean) => void
  pages: Record<string, number>; nextPage: (key: string) => void
  scene: Scene
  layers: Layers; setLayers: (l: Layers) => void
  emphasis: boolean; setEmphasis: (b: boolean) => void
  query: string; setQuery: (q: string) => void
  spotlight: Set<string> | null; setSpotlight: (s: Set<string> | null) => void
  showArch: (checkId: string) => void
  /** jump to a capability on the 專案背景 map and show it in the detail panel */
  focusCapability: (capId: string) => void
  flowId: string | null; setFlowId: (id: string | null) => void
  flowView: FlowView; setFlowView: (v: FlowView) => void
  prFocus: boolean; setPrFocus: (b: boolean) => void
  showInfraSteps: boolean; setShowInfraSteps: (b: boolean) => void
  expandedSeg: string | null; setExpandedSeg: (id: string | null) => void
  segPage: number; setSegPage: (n: number) => void
  player: Player
  guideStep: number; setGuideStep: (n: number) => void
  navigate: (t: WalkTarget) => void
}

const Ctx = createContext<Explorer | null>(null)
export const useExplorer = () => {
  const v = useContext(Ctx)
  if (!v) throw new Error("ExplorerProvider missing")
  return v
}

/** Map highlight for a segment / step; `side` keeps only what exists on that side of the PR. */
export function highlightOf(p: Player, side?: "before" | "after"): Highlight {
  const inSide = (s: OverlayStep) => !side || (side === "before" ? s.step_change_status !== "added" : s.step_change_status !== "removed")
  const idsOf = (steps: OverlayStep[]) => steps.filter(inSide).flatMap((s) => [s.capability_id, ...(s.entity_id ? [s.entity_id] : [])])
  const segs = p.axis?.segments ?? []
  const visited = p.active ? segs.slice(0, p.index).flatMap((s) => idsOf(s.steps)) : []
  if (!p.active) return { current: null, visited }
  if (p.step) {
    if (!inSide(p.step)) return { current: null, visited }
    return { current: { ids: idsOf([p.step]), state: p.step.evidence_status === "confirmed" ? "active" : p.step.evidence_status }, visited }
  }
  const ids = p.segment ? idsOf(p.segment.steps) : []
  return { current: ids.length && p.segment ? { ids, state: segmentState(p.segment) } : null, visited }
}

const navFromHash = (): Nav | null => { const h = window.location.hash.slice(1); return isNav(h) ? h : null }
const newEntryId = (() => { let n = 0; return () => `${Date.now().toString(36)}-${n++}` })()
const sameSel = (a: Selection | null, b: Selection | null) => a?.kind === b?.kind && a?.id === b?.id

export function ExplorerProvider({ model, fixture, children }: { model: Model; fixture: boolean; children: ReactNode }) {
  const initial = useMemo(() => (history.state as HistState | null)?.prx?.snap, [])
  const [nav, setNav] = useState<Nav>(() => initial?.nav ?? navFromHash() ?? (model.overlay ? "summary" : "context"))
  const [card, setCard] = useState<string | null>(initial?.card ?? null)
  const [cardView, setCardView] = useState<CardView>(initial?.cardView ?? null)
  const [panel, setPanel] = useState(initial?.panel ?? false)
  const [ask, setAsk] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(initial?.expanded ?? []))
  const [phase, setPhaseRaw] = useState<Phase>(initial?.phase ?? "system")
  const [selection, select] = useState<Selection | null>(initial?.selection ?? null)
  const [level, setLevelRaw] = useState<Level>(initial?.level ?? {})
  const [showShared, setShowShared] = useState(false)
  const [showEngineering, setShowEngineering] = useState(false)
  const [pages, setPages] = useState<Record<string, number>>({})
  const [layers, setLayers] = useState<Layers>({ findings: false })
  const [emphasis, setEmphasis] = useState(true)
  const [query, setQuery] = useState("")
  const [spotlight, setSpotlight] = useState<Set<string> | null>(null)
  const [flowId, setFlowIdRaw] = useState<string | null>(initial?.flowId ?? null)
  const [flowView, setFlowViewRaw] = useState<FlowView>("overlay")
  const [prFocus, setPrFocus] = useState(true)
  const [showInfraSteps, setShowInfraSteps] = useState(false)
  const [expandedSeg, setExpandedSegRaw] = useState<string | null>(null)
  const [segPage, setSegPage] = useState(0)
  const [guideStep, setGuideStep] = useState(initial?.guideStep ?? -1)

  const setLevel = useCallback((l: Level) => { setLevelRaw(l); setSpotlight(null) }, [])
  const setPhase = useCallback((p: Phase) => { setPhaseRaw(p); setLevelRaw({}); setSpotlight(null) }, [])
  const nextPage = useCallback((key: string) => setPages((p) => ({ ...p, [key]: (p[key] ?? 0) + 1 })), [])
  const toggle = useCallback((id: string, open?: boolean) => setExpanded((s) => {
    const want = open ?? !s.has(id)
    if (want === s.has(id)) return s
    const n = new Set(s)
    if (want) n.add(id); else n.delete(id)
    return n
  }), [])
  const scene = useMemo(() => buildScene(model, { mode: phase === "overlay" && model.overlay ? "pr" : "system", level, showShared, showEngineering, pages }),
    [model, phase, level, showShared, showEngineering, pages])

  // ---- player over the axis segments of the focused flow, derived for the chosen view
  const [index, setIndex] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const [active, setActive] = useState(false)
  const [stepId, setStepIdRaw] = useState<string | null>(null)
  const axis = useMemo(() => (flowId ? buildAxis(model, flowId, flowView, { showInfra: showInfraSteps }) : null), [model, flowId, flowView, showInfraSteps])
  const fc = flowId ? model.flowChange.get(flowId) : undefined
  const flow = flowId ? model.flows.find((f) => f.id === flowId) : undefined
  const mode = fc?.mode ?? flow?.timeline.mode ?? "static_flow"
  const reason = fc?.mode_reason ?? flow?.timeline.mode_reason
  const segs = axis?.segments ?? []
  const animated = mode === "animation"

  const resetPlayer = useCallback(() => { setIndex(0); setPlaying(false); setActive(false); setStepIdRaw(null); setExpandedSegRaw(null); setSegPage(0) }, [])
  const setFlowId = useCallback((id: string | null) => { setFlowIdRaw(id); resetPlayer(); setSpotlight(null) }, [resetPlayer])
  const setFlowView = useCallback((v: FlowView) => { setFlowViewRaw(v); resetPlayer() }, [resetPlayer])
  const setShowInfra = useCallback((b: boolean) => { setShowInfraSteps(b); resetPlayer() }, [resetPlayer])
  // picking a segment = make it current, expand it (one at a time), show its first page
  const goSegment = useCallback((i: number) => {
    setActive(true); setIndex(i); setStepIdRaw(null); setSegPage(0)
    const s = segs[i]
    setExpandedSegRaw(s ? s.id : null)
  }, [segs])
  const setExpandedSeg = useCallback((id: string | null) => {
    if (!id) { setExpandedSegRaw(null); return }
    const i = segs.findIndex((s) => s.id === id)
    if (i >= 0) goSegment(i)
  }, [segs, goSegment])
  const setPlay = useCallback((b: boolean) => { if (b && !active) goSegment(index); setPlaying(b) }, [active, index, goSegment])
  const replay = useCallback(() => { goSegment(0); setPlaying(true) }, [goSegment])
  const setStepId = useCallback((id: string | null) => { setActive(true); setStepIdRaw(id) }, [])

  useEffect(() => {
    if (!playing || !animated) return
    if (index >= segs.length - 1) { setPlaying(false); return }
    const t = setTimeout(() => goSegment(index + 1), 1800 / speed)
    return () => clearTimeout(t)
  }, [playing, animated, index, segs.length, speed, goSegment])

  const segment = active ? segs[index] ?? null : null
  const step = stepId ? segs.flatMap((s) => s.steps).find((s) => s.id === stepId) ?? null : null

  // ---- history: the current entry always describes what is on screen; explicit navigation pushes a new one
  const scrollRef = useRef<HTMLElement | null>(null)
  const entry = useRef(newEntryId())
  const memory = useRef(new Map<string, ViewSnap>())
  const pendingScroll = useRef<{ top: number } | { anchor: string } | null>(initial ? { top: initial.scroll } : null)
  /** undefined = nothing to restore; null = collapse */
  const pendingSeg = useRef<string | null | undefined>(initial?.seg ?? undefined)
  const [restoreTick, setRestoreTick] = useState(0)
  const snapshot = (): ViewSnap => ({ nav, card, cardView, flowId, seg: expandedSeg, expanded: [...expanded], selection, panel, guideStep, phase, level, scroll: scrollRef.current?.scrollTop ?? 0 })
  const snapRef = useRef(snapshot)
  snapRef.current = snapshot
  const apply = useCallback((s: ViewSnap) => {
    setNav(s.nav); setCard(s.card); setCardView(s.cardView); setExpanded(new Set(s.expanded)); select(s.selection); setPanel(s.panel && !!s.selection); setGuideStep(s.guideStep)
    if (s.panel && s.selection) setAsk(false)
    setPhaseRaw(s.phase); setLevelRaw(s.level); setSpotlight(null)
    setFlowIdRaw((cur) => { if (cur !== s.flowId) resetPlayer(); return s.flowId })
    pendingSeg.current = s.seg
    setRestoreTick((n) => n + 1)
  }, [resetPlayer])
  const stamp = (snap: ViewSnap) => {
    memory.current.set(entry.current, snap)
    history.replaceState({ ...(history.state as object | null), prx: { id: entry.current, snap } } satisfies HistState, "")
  }
  useEffect(() => {
    if ("scrollRestoration" in history) history.scrollRestoration = "manual"
    stamp(snapRef.current())
    const onPop = (e: PopStateEvent) => {
      memory.current.set(entry.current, snapRef.current())
      const prx = (e.state as HistState | null)?.prx
      if (!prx) { const n = navFromHash(); if (n) apply({ ...snapRef.current(), nav: n, scroll: 0 }); return }
      entry.current = prx.id
      const s = memory.current.get(prx.id) ?? prx.snap
      apply(s)
      pendingScroll.current = { top: s.scroll }
    }
    window.addEventListener("popstate", onPop)
    return () => window.removeEventListener("popstate", onPop)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const go = useCallback((to: Partial<ViewSnap> & { nav: Nav }, anchor?: string) => {
    const cur = snapRef.current()
    const next: ViewSnap = { ...cur, scroll: 0, panel: false, ...(to.card !== undefined && to.card !== cur.card ? { cardView: null } : {}), ...to }
    const same = next.nav === cur.nav && next.card === cur.card && next.cardView === cur.cardView && next.flowId === cur.flowId && next.panel === cur.panel && sameSel(next.selection, cur.selection) &&
      next.phase === cur.phase && JSON.stringify(next.level) === JSON.stringify(cur.level) && next.seg === cur.seg
    if (!same) {
      stamp(cur)
      entry.current = newEntryId()
      memory.current.set(entry.current, next)
      history.pushState({ prx: { id: entry.current, snap: next } } satisfies HistState, "", `${window.location.pathname}${window.location.search}#${next.nav}`)
      apply(next)
    }
    pendingScroll.current = anchor ? { anchor } : same ? null : { top: 0 }
  }, [apply])
  const trigger = useRef<HTMLElement | null>(null)
  const readingPos = useRef(0)
  const openDetail = useCallback((s: Selection, from?: HTMLElement | null, fromCard?: string) => {
    // from inside the ask panel the reference replaces it; closing then returns to what opened the ask panel
    if (!panel && !ask) { trigger.current = from ?? (document.activeElement as HTMLElement | null); readingPos.current = scrollRef.current?.scrollTop ?? 0 }
    select(s); setPanel(true); setAsk(false)
    if (fromCard !== undefined) setCard(fromCard)
  }, [panel, ask])
  const openAsk = useCallback((from?: HTMLElement | null) => {
    // opened from outside the panels (the nav button): that button gets focus back, even if it replaced the detail panel
    if (!ask) trigger.current = from ?? (document.activeElement as HTMLElement | null)
    if (!panel && !ask) readingPos.current = scrollRef.current?.scrollTop ?? 0
    setPanel(false); setAsk(true)
  }, [panel, ask])
  const closeAsk = useCallback(() => {
    setAsk(false)
    pendingScroll.current = { top: readingPos.current }
    pendingFocus.current = trigger.current
  }, [])
  const closeDetail = useCallback(() => {
    setPanel(false)
    pendingScroll.current = { top: readingPos.current }
    pendingFocus.current = trigger.current
  }, [])
  const pendingFocus = useRef<HTMLElement | null>(null)
  // restore the segment once its flow's axis exists, then the scroll position / anchor once the view is in the DOM
  useEffect(() => {
    const id = pendingSeg.current
    if (id === undefined || (id !== null && !axis)) return
    pendingSeg.current = undefined
    const i = id === null || !axis ? -1 : axis.segments.findIndex((x) => x.id === id)
    if (i >= 0) goSegment(i)
    else { setExpandedSegRaw(null); setActive(false) }
  }, [axis, goSegment, restoreTick])
  useLayoutEffect(() => {
    const p = pendingScroll.current, el = scrollRef.current
    const f = pendingFocus.current
    if (f) {
      pendingFocus.current = null
      const back = f.isConnected ? f : document.getElementById("view-title")
      back?.focus({ preventScroll: true })
    }
    if (!p || !el) return
    pendingScroll.current = null
    if ("top" in p) { el.scrollTop = p.top; return }
    const target = document.getElementById(p.anchor)
    if (!target) { el.scrollTop = 0; return }
    target.scrollIntoView?.({ block: "start" })
    target.focus({ preventScroll: true })
  })

  const revealLevel = useCallback((id: string) => {
    const cap = capabilityOf(model, id)
    if (cap) setLevelRaw({ capId: cap }) // the breadcrumb derives the domain (or the shared / engineering bucket)
  }, [model])

  const selectElement = useCallback((id: string) => {
    const k = model.kindOf.get(id)
    if (!k) return
    if (k !== "flow") revealLevel(id)
    select({ kind: k, id })
  }, [model, revealLevel])

  const showArch = useCallback((checkId: string) => {
    go({ nav: "context", selection: { kind: "arch", id: checkId }, panel: true, phase: model.overlay ? "overlay" : "system", level: {} })
  }, [model, go])

  const focusCapability = useCallback((id: string) => {
    const cap = capabilityOf(model, id)
    if (!cap) return
    go({ nav: "context", phase: model.overlay ? "overlay" : "system", level: { capId: cap }, selection: { kind: "capability", id: cap }, panel: true })
  }, [model, go])

  /** Reading-path targets (walkthrough) → the main navigation entry that answers them. */
  const navigate = useCallback((t: WalkTarget) => {
    const caps = t.capability_ids ?? []
    const ov = !!model.overlay
    switch (t.view) {
      case "system": go({ nav: "context", phase: "system", level: {} }); break
      case "overlay": go({ nav: "context", phase: ov ? "overlay" : "system", level: {} }); break
      case "flow": {
        const id = t.flow_id ?? flowId
        const c = changeCards(model).find((x) => !!id && x.flowIds.includes(id))
        go({ nav: "changes", flowId: id, card: c?.id ?? null, cardView: "flow" }, "flow-focus"); break
      }
      case "findings": go({ nav: "review", card: t.finding_id ?? null }, t.finding_id ? `review-${t.finding_id.replace(/[^\w-]/g, "_")}` : "review-findings"); break
      case "architecture": go({ nav: "review" }, "review-arch"); break
      case "evidence": go({ nav: "review" }); break // the Evidence tab was removed (owner 2026-11-11); evidence stays in the data
    }
    setSpotlight(caps.length ? new Set(caps) : null)
  }, [model, go, flowId])

  const value: Explorer = {
    model, fixture, nav, go, scrollRef, card, setCard, cardView, panel, openDetail, closeDetail, ask, openAsk, closeAsk, expanded, toggle, phase, setPhase, selection, select, selectElement,
    level, setLevel, showShared, setShowShared, showEngineering, setShowEngineering, pages, nextPage, scene,
    layers, setLayers, emphasis, setEmphasis, query, setQuery, spotlight, setSpotlight, showArch, focusCapability,
    flowId, setFlowId, flowView, setFlowView, prFocus, setPrFocus, showInfraSteps, setShowInfraSteps: setShowInfra, expandedSeg, setExpandedSeg, segPage, setSegPage,
    guideStep, setGuideStep, navigate,
    player: { axis, mode, reason, index, setIndex: goSegment, playing, setPlaying: setPlay, speed, setSpeed, active, segment, stepId, setStepId, step, replay },
  }
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
