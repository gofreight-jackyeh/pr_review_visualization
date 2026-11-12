import { Handle, Position, type Node as RFNode, type NodeProps } from "@xyflow/react"
import type { ChangeStatus, ImpactStatus } from "@/entities/artifact/types"
import { ACTIVE, CHANGE, IMPACT, NEUTRAL, POSSIBLE } from "./style"
import { RING, type Ring } from "@/features/architecture/interfaces"
import type { SNodeKind } from "./scene"

export interface MapNodeData extends Record<string, unknown> {
  kind: SNodeKind
  primary: string
  secondary?: string
  detail?: string // second small line: counts, entry kind, or path:line
  showStatus: boolean
  change: ChangeStatus
  impact: ImpactStatus
  w: number
  h: number
  tag?: string
  context?: boolean
  dim: boolean
  selected: boolean
  stepState: null | "active" | "possible" | "unknown"
  visited: boolean
  badges: { findings: number }
  /** outermost ring the PR touched in this node (PR view only) */
  ring?: Ring
}
export type MapNodeT = RFNode<MapNodeData, "iso" | "domain" | "summary">

const D = 14 // depth of the slanted box
const trunc = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s)

function Badges({ d, x, y }: { d: MapNodeData; x: number; y: number }) {
  const items: [string, number, string][] = []
  if (d.badges.findings) items.push(["F", d.badges.findings, "#c0392b"])
  return (
    <g>{items.map(([k, n, c], i) => (
      <g key={k} transform={`translate(${x - i * 34}, ${y})`}><rect width="30" height="16" rx="8" fill={c} /><text x="15" y="12" textAnchor="middle" fontSize="10" fontWeight="700" fill="#fff">{k}{n}</text></g>
    ))}</g>
  )
}

/** Ring badge (spec §8): 外圈 is solid and larger so it reads first; 中圈 / 內圈 are quiet. */
function RingPill({ ring, x, y }: { ring: Ring; x: number; y: number }) {
  const r = RING[ring], outer = ring === "outer"
  const w = outer ? 44 : 36, h = outer ? 18 : 15
  return (
    <g transform={`translate(${x - w}, ${y})`} data-ring={ring}>
      <rect width={w} height={h} rx={h / 2} fill={r.bg} stroke={r.color} strokeWidth={outer ? 2 : 1} />
      <text x={w / 2} y={h - (outer ? 5 : 4)} textAnchor="middle" fontSize={outer ? 11 : 9.5} fontWeight="700" fill={r.fg}>{r.label}</text>
    </g>
  )
}

const skin = (d: MapNodeData) => (d.showStatus ? CHANGE[d.change] : NEUTRAL)
const ringOf = (d: MapNodeData) =>
  d.stepState === "active" ? ACTIVE : d.stepState === "possible" || d.stepState === "unknown" ? POSSIBLE : d.selected ? "#111827" : d.showStatus && d.change === "unchanged" && d.impact !== "none" ? IMPACT[d.impact].color : null

export function IsoNode({ data: d }: NodeProps<MapNodeT>) {
  const c = skin(d)
  const imp = d.showStatus && d.impact !== "none" ? IMPACT[d.impact] : null
  const { w, h } = d
  const ring = ringOf(d)
  const statusLine = d.showStatus ? [d.change !== "unchanged" ? c.label : "", imp?.short ?? ""].filter(Boolean).join(" · ") : ""
  return (
    <div className="iso" title={[d.primary, d.secondary, d.detail].filter(Boolean).join("\n")} style={{ width: w, height: h + 8, opacity: d.dim ? 0.22 : 1 }} data-testid={`node-${d.primary}`} data-change={d.change} data-step={d.stepState ?? ""}>
      <Handle type="target" position={Position.Left} className="hdl" />
      <Handle type="source" position={Position.Right} className="hdl" />
      <svg width={w} height={h + 8} viewBox={`0 0 ${w} ${h + 8}`} role="img" aria-label={`${d.primary}${d.secondary ? " " + d.secondary : ""}, ${d.showStatus ? c.label : ""}${imp ? ", " + imp.label : ""}`}>
        {d.stepState && <rect x="-2" y="-2" width={w + 4} height={h + 12} rx="8" fill="none" stroke={d.stepState === "active" ? ACTIVE : POSSIBLE} strokeWidth="3" opacity=".4" className="pulse" />}
        <g strokeLinejoin="round" strokeDasharray={d.showStatus && d.change === "removed" ? "6 3" : undefined}>
          <polygon points={`${D},4 ${w},4 ${w - D},22 0,22`} fill={c.top} stroke={c.stroke} strokeWidth="1.2" />
          <polygon points={`0,22 ${w - D},22 ${w - D},${h + 4} 0,${h + 4}`} fill={c.face} stroke={c.stroke} strokeWidth="1.2" />
          <polygon points={`${w - D},22 ${w},4 ${w},${h - 10} ${w - D},${h + 4}`} fill={c.side} stroke={c.stroke} strokeWidth="1.2" />
        </g>
        {ring && <polygon points={`0,22 ${D},4 ${w},4 ${w},${h - 10} ${w - D},${h + 4} 0,${h + 4}`} fill="none" stroke={ring} strokeWidth={d.stepState || d.selected ? 3 : 2}
          strokeDasharray={d.stepState === "possible" || d.stepState === "unknown" ? "5 3" : !d.selected && imp ? imp.dash : undefined} />}
        <text x="10" y="43" fontSize="13" fontWeight="700" fill="#1f2933" textDecoration={d.showStatus && d.change === "removed" ? "line-through" : undefined}>{trunc(d.primary, 17)}</text>
        {d.secondary && <text x="10" y="57" fontSize="9.5" fill="#52606d" fontFamily="ui-monospace, Menlo, monospace">{trunc(d.secondary, 28)}</text>}
        <text x="10" y={d.secondary ? 70 : 62} fontSize="10" fill="#52606d">{trunc(d.detail ?? "", 30)}</text>
        {statusLine && <text x="10" y="82" fontSize="10" fontWeight="700" fill={imp?.color ?? c.stroke}>{statusLine}</text>}
        {d.ring && <RingPill ring={d.ring} x={w - D - 4} y={26} />}
        <Badges d={d} x={w - D - 36} y={h - 22} />
      </svg>
      {d.context && <span className="ctx-tag">相關</span>}
      {d.stepState === "possible" && <span className="tag-possible">可能路徑</span>}
      {d.stepState === "unknown" && <span className="tag-possible tag-unknown">未知</span>}
      {d.visited && !d.stepState && <span className="visited-dot" title="動畫中先前走過" />}
    </div>
  )
}

/** A business domain (or the shared / engineering bucket): a card with its capability count and grouping provenance. */
export function DomainNode({ data: d }: NodeProps<MapNodeT>) {
  const c = skin(d)
  const imp = d.showStatus && d.impact !== "none" ? IMPACT[d.impact] : null
  const ring = d.stepState === "active" ? ACTIVE : d.stepState ? POSSIBLE : d.selected ? "#111827" : imp ? imp.color : null
  return (
    <div className={"domain-node" + (d.context ? " ctx" : "") + (d.kind === "bucket" ? " bucket" : "")} data-testid={`domain-${d.primary}`} title={[d.primary, d.secondary].filter(Boolean).join("\n")}
      style={{ width: d.w, height: d.h, opacity: d.dim ? 0.25 : 1, borderColor: c.stroke, background: c.top, boxShadow: ring ? `0 0 0 3px ${ring}` : undefined }}>
      <Handle type="target" position={Position.Left} className="hdl" />
      <Handle type="source" position={Position.Right} className="hdl" />
      <div className="dn-title"><strong>{trunc(d.primary, 16)}</strong>{d.secondary && <code className="tech">{trunc(d.secondary, 18)}</code>}</div>
      <div className="dn-meta"><span>{d.detail}</span>{d.tag && <span className={"dn-tag" + (d.tag === "由 Claude 歸類" ? " claude" : "")}>{d.tag}</span>}</div>
      {d.showStatus && (d.change !== "unchanged" || imp) && <div className="dn-status" style={{ color: imp?.color ?? c.stroke }}>{[d.change !== "unchanged" ? "含修改" : "", imp?.short ?? ""].filter(Boolean).join(" · ")}</div>}
      {d.ring && <span className={"ring-pill ring-" + d.ring} data-ring={d.ring} title="這個 PR 動到的最外層介面圈別">{RING[d.ring].label}</span>}
      {d.badges.findings > 0 && <span className="pill" style={{ background: "#c0392b", color: "#fff" }}>F{d.badges.findings}</span>}
      <span className="dn-go" aria-hidden>›</span>
    </div>
  )
}

/** Overflow / unaffected-domain summary: dashed, clickable. */
export function SummaryNode({ data: d }: NodeProps<MapNodeT>) {
  const imp = d.showStatus && d.impact !== "none" ? IMPACT[d.impact] : null
  return (
    <div className="summary-node" data-testid={`summary-${d.primary}`} style={{ width: d.w, height: d.h, opacity: d.dim ? 0.3 : 1, borderColor: imp?.color }}>
      <Handle type="target" position={Position.Left} className="hdl" />
      <Handle type="source" position={Position.Right} className="hdl" />
      <strong>{d.primary}</strong>
      <span className="muted tiny">{d.detail}</span>
    </div>
  )
}

export const nodeTypes = { iso: IsoNode, domain: DomainNode, summary: SummaryNode }
