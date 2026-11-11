import { useEffect, useMemo, useRef, useState } from "react"
import { Background, BackgroundVariant, Controls, getViewportForBounds, MarkerType, ReactFlow, ReactFlowProvider, useReactFlow, useStore, type Edge as RFEdge } from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import { visibleInView } from "@/entities/artifact/model"
import type { MapView as View } from "@/entities/artifact/types"
import { useExplorer, type Highlight } from "@/app/store"
import { capRings, outermost } from "@/features/architecture/interfaces"
import { fitTargets, getLayout, placeNodes, type Box, type Layout } from "./graph"
import { nodeTypes, type MapNodeData, type MapNodeT } from "./nodes"
import { resolveNode, sceneNodeStatus, type Scene, type SNode } from "./scene"
import { ACTIVE, POSSIBLE, RELATION_LABEL, VIOLATION } from "./style"

const MIN_ZOOM = 0.08
/** the local relation view never fits below this zoom, so node titles stay readable (the user pans instead) */
const LOCAL_MIN_ZOOM = 0.6

function Inner({ scene, view, highlight }: { scene: Scene; view: View; highlight?: Highlight }) {
  const ex = useExplorer()
  const m = ex.model
  const rf = useReactFlow()
  const [hoverEdge, setHoverEdge] = useState<string | null>(null)
  const [ready, setReady] = useState<{ scene: Scene; layout: Layout } | null>(null)
  useEffect(() => {
    let live = true
    getLayout(m, scene).then((layout) => { if (live) setReady({ scene, layout }) })
    return () => { live = false }
  }, [m, scene])

  const q = ex.query.trim().toLowerCase()
  const cur = highlight?.current ?? null
  const resolveSet = (ids: Iterable<string> | null | undefined, s: Scene) => {
    if (!ids) return null
    const out = new Set<string>()
    for (const id of ids) { const v = resolveNode(m, s.owner, id) ?? (s.nodes.some((n) => n.id === id) ? id : undefined); if (v) out.add(v) }
    return out
  }

  const { nodes, edges } = useMemo(() => {
    if (!ready) return { nodes: [] as MapNodeT[], edges: [] as RFEdge[] }
    const { scene: s, layout } = ready
    const placed = placeNodes(m, s, layout, view)
    const stepNodes = resolveSet(cur?.ids, s) ?? new Set<string>()
    const visitedIds = resolveSet(highlight?.visited, s) ?? new Set<string>()
    const spotKeep = resolveSet(ex.spotlight, s)
    const matchIds = q ? [...m.byId.values()].filter((e) => `${e.business_name?.text ?? ""} ${e.technical_name} ${e.id}`.toLowerCase().includes(q)).map((e) => e.id) : null
    const matchKeep = matchIds ? new Set([...resolveSet(matchIds, s)!, ...s.nodes.filter((n) => n.label.toLowerCase().includes(q)).map((n) => n.id)]) : null
    const showStatus = view !== "system" && !!m.overlay
    const selArch = ex.selection?.kind === "arch" ? ex.selection.id : null
    const findingCount = new Map<string, number>()
    if (ex.layers.findings && m.overlay) for (const f of m.overlay.findings) for (const c of f.capability_ids) {
      const v = resolveNode(m, s.owner, c)
      if (v) findingCount.set(v, (findingCount.get(v) ?? 0) + 1)
    }
    const selNode = ex.selection ? resolveNode(m, s.owner, ex.selection.id) ?? (s.nodes.some((n) => n.id === ex.selection!.id) ? ex.selection.id : undefined) : undefined
    const rings = showStatus ? capRings(m) : new Map()
    const rfNodes: MapNodeT[] = placed.map((p) => {
      const n: SNode = p.node
      const st = showStatus ? sceneNodeStatus(m, n) : { change: "unchanged" as const, impact: "none" as const }
      const dimEmph = ex.emphasis && showStatus && s.mode !== "local" && !spotKeep && st.change === "unchanged" && st.impact === "none" && !stepNodes.size
      const dim = (spotKeep ? !spotKeep.has(p.id) : false) || (matchKeep ? !matchKeep.has(p.id) : false) || dimEmph
      const data: MapNodeData = {
        kind: n.kind, primary: n.label, secondary: n.secondary, detail: n.detail, tag: n.tag, context: n.context, showStatus, change: st.change, impact: st.impact,
        w: p.w, h: p.h, dim, selected: selNode === p.id,
        stepState: stepNodes.has(p.id) ? cur!.state : null, visited: visitedIds.has(p.id),
        badges: { findings: findingCount.get(p.id) ?? 0 },
        ring: showStatus ? outermost(n.members.map((id) => rings.get(id))) : undefined,
      }
      const type = n.kind === "domain" || n.kind === "bucket" ? "domain" : n.kind === "summary" ? "summary" : "iso"
      return { id: p.id, type, position: { x: p.x, y: p.y }, data, style: { width: p.w, height: type === "iso" ? p.h + 8 : p.h }, draggable: false, zIndex: 2 }
    })
    const shown = new Set(rfNodes.map((n) => n.id))
    const rfEdges: RFEdge[] = []
    for (const e of s.edges) {
      if (!shown.has(e.source) || !shown.has(e.target)) continue
      const violation = showStatus && e.violations.length > 0
      if (!violation && e.relationIds.length && !e.relationIds.some((id) => visibleInView(m, id, view))) continue
      const unknown = e.confidence === "unknown"
      const isNew = showStatus && e.headOnly
      const onPath = !!cur && stepNodes.has(e.source) && stepNodes.has(e.target)
      const focused = !!selArch && e.violations.includes(selArch)
      const color = violation ? VIOLATION : onPath ? (cur?.state === "active" ? ACTIVE : POSSIBLE) : isNew ? "#2f9e5b" : "#5b6572"
      const width = 1 + Math.min(e.weight, 8) * 0.45
      const kinds = e.kinds.map((k) => RELATION_LABEL[k]).join("/") || "依賴"
      rfEdges.push({
        id: e.id, source: e.source, target: e.target, type: "smoothstep", animated: onPath || focused,
        label: s.mode === "local" ? (hoverEdge === e.id ? e.sentence : undefined) : e.label ?? (violation ? `⚠ 違反架構規則${e.violations.length > 1 ? ` ×${e.violations.length}` : ""}` : `${kinds}${e.weight > 1 ? ` ×${e.weight}` : ""}${unknown ? "（未確認）" : ""}${isNew ? "（新）" : ""}`),
        labelStyle: { fontSize: 10, fill: violation ? VIOLATION : "#334155", fontWeight: violation ? 700 : 400 }, labelBgStyle: { fill: "#fff", fillOpacity: 0.9 }, labelBgPadding: [3, 2],
        markerEnd: { type: MarkerType.ArrowClosed, color },
        style: { stroke: color, strokeWidth: focused ? width + 4 : violation ? width + 2 : onPath ? width + 1.5 : width, strokeDasharray: unknown && !violation ? "7 5" : undefined,
          opacity: selArch && !focused ? 0.25 : 1 },
        zIndex: violation ? 6 : onPath ? 5 : 1,
        data: { violation, checks: e.violations },
        ...(s.mode === "local" ? { interactionWidth: 24, focusable: true, ariaLabel: e.sentence } : {}),
      })
    }
    return { nodes: rfNodes, edges: rfEdges }
  }, [ready, m, view, q, cur, highlight?.visited, ex.selection, ex.spotlight, ex.layers, ex.emphasis, hoverEdge]) // eslint-disable-line react-hooks/exhaustive-deps

  // Frame once per (view, drawn node set) from our own layout boxes: never again for the same set, so manual
  // zoom/pan survives final documents replacing pending ones.
  const paneW = useStore((s) => s.width), paneH = useStore((s) => s.height)
  const fitted = useRef("")
  // pane size is part of the key: the pane settles after the intro above it renders, and a fit made before that sits off-centre
  const fitKey = ready ? `${view}#${ready.scene.key}#${Math.round(paneW / 40)}x${Math.round(paneH / 40)}` : ""
  useEffect(() => {
    if (!ready || !fitKey || fitted.current === fitKey || !paneW || !paneH) return
    const placedNow = placeNodes(m, ready.scene, ready.layout, view)
    const boxes = (ready.scene.mode === "local" ? placedNow.map((p) => p.id) : fitTargets(m, placedNow, view)).map((id) => ready.layout.get(id)).filter((b): b is Box => !!b)
    if (!boxes.length) return
    fitted.current = fitKey
    const x = Math.min(...boxes.map((b) => b.x)), y = Math.min(...boxes.map((b) => b.y))
    const bounds = { x, y, width: Math.max(...boxes.map((b) => b.x + b.w)) - x, height: Math.max(...boxes.map((b) => b.y + b.h)) - y }
    void rf.setViewport(getViewportForBounds(bounds, paneW, paneH, ready.scene.mode === "local" ? LOCAL_MIN_ZOOM : MIN_ZOOM, 1.2, 0.12), { duration: 250 })
  }, [ready, fitKey, paneW, paneH, m, view, rf])

  const onNode = (id: string) => {
    const n = ready?.scene.nodes.find((x) => x.id === id)
    if (!n) return
    const a = n.action
    if (a.kind === "page") ex.nextPage(a.key)
    else if (a.kind === "domain") { ex.setLevel({ domainId: a.id }); ex.select({ kind: "domain", id: a.id }) }
    else if (a.kind === "capability" && scene.mode !== "flow" && scene.mode !== "local") { ex.setLevel({ capId: a.id }); ex.openDetail({ kind: "capability", id: a.id }) }
    else ex.openDetail({ kind: m.kindOf.get(a.id) ?? "capability", id: a.id })
  }

  return (
    <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} minZoom={MIN_ZOOM} maxZoom={2} nodesDraggable={false} nodesConnectable={false}
      onNodeClick={(_, n) => onNode(n.id)}
      onEdgeMouseEnter={(_, e) => setHoverEdge(e.id)} onEdgeMouseLeave={() => setHoverEdge(null)} onEdgeClick={(_, e) => setHoverEdge((h) => (h === e.id ? null : e.id))}
      onPaneClick={() => ex.setSpotlight(null)} proOptions={{ hideAttribution: true }}>
      <Background variant={BackgroundVariant.Dots} gap={22} size={1.2} color="#cbd5e1" />
      <Controls showInteractive={false} />
    </ReactFlow>
  )
}

export function MapView(props: { scene: Scene; view: View; title?: string; highlight?: Highlight }) {
  return (
    <div className="mapview" aria-label={props.title ?? "系統地圖"} data-view={props.view} data-nodes={props.scene.nodes.length}>
      {props.title && <div className="map-title">{props.title}</div>}
      <ReactFlowProvider><Inner scene={props.scene} view={props.view} highlight={props.highlight} /></ReactFlowProvider>
    </div>
  )
}
