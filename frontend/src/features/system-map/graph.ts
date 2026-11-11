import ELK from "elkjs/lib/elk.bundled.js"
import type { ElkNode } from "elkjs/lib/elk-api"
import { visibleInView, type Model } from "@/entities/artifact/model"
import type { MapView } from "@/entities/artifact/types"
import { sceneNodeStatus, type Scene, type SNode } from "./scene"

export interface Box { x: number; y: number; w: number; h: number }
export type Layout = Map<string, Box>
const elk = new ELK()

/** Flat layered layout, left → right (one level is drawn at a time, so no nested groups). */
export async function computeLayout(scene: Scene): Promise<Layout> {
  const graph: ElkNode = {
    id: "root",
    layoutOptions: { "elk.algorithm": "layered", "elk.direction": "RIGHT", "elk.spacing.nodeNode": "40", "elk.layered.spacing.nodeNodeBetweenLayers": "90",
      "elk.padding": "[top=20,left=20,bottom=20,right=20]", "elk.separateConnectedComponents": "true" },
    children: scene.nodes.map((n) => ({ id: n.id, width: n.w, height: n.h })),
    edges: scene.edges.map((e) => ({ id: e.id, sources: [e.source], targets: [e.target] })),
  }
  const res = await elk.layout(graph)
  const out: Layout = new Map()
  for (const c of res.children ?? []) out.set(c.id, { x: Math.round(c.x ?? 0), y: Math.round(c.y ?? 0), w: Math.round(c.width ?? 0), h: Math.round(c.height ?? 0) })
  return out
}

// Keyed by the scene (stable ids), computed over base+head, so system / overlay / before / after share positions.
const cache = new WeakMap<Model, Map<string, Promise<Layout>>>()
export function getLayout(m: Model, scene: Scene): Promise<Layout> {
  let per = cache.get(m)
  if (!per) cache.set(m, (per = new Map()))
  let p = per.get(scene.key)
  if (!p) per.set(scene.key, (p = computeLayout(scene)))
  return p
}

export interface Placed extends Box { id: string; node: SNode }
/** Nodes drawn in a view; element nodes absent from that side (added / removed) are hidden but keep their slot. */
export function placeNodes(m: Model, scene: Scene, layout: Layout, view: MapView): Placed[] {
  return scene.nodes.filter((n) => layout.has(n.id) && (!n.ref || !m.kindOf.has(n.ref) || visibleInView(m, n.ref, view)))
    .map((n) => ({ id: n.id, node: n, ...layout.get(n.id)! }))
}

/** Initial framing: the whole scene, except PR views frame what the PR changed or directly impacts. */
export function fitTargets(m: Model, placed: Placed[], view: MapView): string[] {
  const all = placed.map((p) => p.id)
  if (view === "system" || !m.overlay) return all
  const core = placed.filter((p) => p.node.kind !== "summary" && (() => { const s = sceneNodeStatus(m, p.node); return s.change !== "unchanged" || s.impact === "direct" })())
  return core.length ? core.map((p) => p.id) : all
}
