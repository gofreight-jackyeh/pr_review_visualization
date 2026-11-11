import type { ReactNode } from "react"
import { nameOfId } from "@/entities/artifact/model"
import { statusOf, type Source, type Status } from "@/entities/artifact/provenance"
import type { CodeRef, Finding } from "@/entities/artifact/types"
import { useExplorer } from "@/app/store"
import { RefRow, SourceNote, StatusBadge } from "@/shared/ui/ui"
import { relatedOf, reviewItems, VIA_LABEL } from "./related"

export const reviewDomId = (id: string) => `review-${id.replace(/[^\w-]/g, "_")}`
const SEVERITY: Record<Finding["severity"], string> = { warning: "警告", attention: "注意", info: "資訊" }

/**
 * Shared layout of every review card: what to confirm first, then the features involved, known facts vs what is
 * uncertain, and the code locations. One primary interpretation status; the source is a quiet note.
 */
export function ReviewCard({ id, testid, status, source, confirm, title, caps, facts, uncertain, refs, extra, className, attrs, embedded }: {
  id: string; testid: string; status: Status; source: Source; confirm: ReactNode; title?: ReactNode; caps: string[]
  facts: ReactNode[]; uncertain: ReactNode[]; refs: { label?: string; refs: CodeRef[] }[]; extra?: ReactNode; className?: string
  attrs?: Record<string, string>
  /** rendered inside the detail panel: no page anchor, not marked as the source card */
  embedded?: boolean
}) {
  const ex = useExplorer()
  const m = ex.model
  const on = !embedded && ex.card === id
  const related = relatedOf(reviewItems(m), id)
  return (
    <article id={embedded ? undefined : reviewDomId(id)} tabIndex={-1} className={"review-card " + (className ?? "") + (on ? " on" : "")} aria-current={on ? "true" : undefined} data-testid={testid} {...attrs}>
      <div className="rc-head"><StatusBadge s={status} /> <SourceNote source={source} /></div>
      <p className="rc-confirm"><b>需確認：</b>{confirm}</p>
      {title && <p className="rc-title">{title}</p>}
      {caps.length > 0 && (
        <p className="small rc-caps">涉及功能：{caps.map((c) => (
          <button key={c} className="chip-btn" aria-label={`查看功能「${nameOfId(m, c).primary}」的說明`} onClick={(e) => ex.openDetail({ kind: "capability", id: c }, e.currentTarget, id)}>{nameOfId(m, c).primary}</button>
        ))}</p>
      )}
      {facts.length > 0 && <div className="rc-facts"><h5>已知事實</h5><ul>{facts.map((f, i) => <li key={i}>{f}</li>)}</ul></div>}
      {uncertain.length > 0 && <div className="rc-uncertain"><h5>不確定之處</h5><ul>{uncertain.map((f, i) => <li key={i}>{f}</li>)}</ul></div>}
      {refs.filter((r) => r.refs.length).map((r, i) => <div key={i} className="small rc-refs">{r.label && <span className="muted">{r.label}</span>}{r.refs.map((x, j) => <RefRow key={j} r={x} pr={m.overlay?.pr} />)}</div>)}
      {extra}
      {related.length > 0 && (
        <p className="small rc-related" data-testid={`related-${id}`}>相關檢查：{related.map((r) => (
          <button key={r.id} className="link" onClick={() => ex.go({ nav: "review", card: r.id }, reviewDomId(r.id))}>{r.title}（{VIA_LABEL[r.via]}）</button>
        ))}</p>
      )}
    </article>
  )
}

/** A finding (待確認事項): verified → structural fact; otherwise its description is the open question. */
export function FindingCard({ f, embedded }: { f: Finding; embedded?: boolean }) {
  const ex = useExplorer()
  const st = statusOf({ finding: f })
  const facts: ReactNode[] = [<>嚴重度（僅在待確認事項內比較）：{SEVERITY[f.severity]}</>]
  const uncertain: ReactNode[] = []
  ;(st === "confirmed" ? facts : uncertain).push(f.description)
  if (f.business_impact) uncertain.push(<>業務影響（{f.source === "semantic" ? "AI 說明" : "靜態分析規則"}的推論，需確認）：{f.business_impact}</>)
  if (f.rule) facts.push(<>靜態規則：<code className="tech">{f.rule}</code></>)
  const flows = f.flow_ids ?? []
  return (
    <ReviewCard id={f.id} testid={embedded ? `panel-finding-${f.id}` : `finding-${f.id}`} embedded={embedded} status={st} source={f.source === "semantic" ? "semantic" : "static"} confirm={f.title} caps={f.capability_ids}
      facts={facts} uncertain={uncertain} refs={[{ refs: f.refs }]}
      extra={flows.length > 0 && <p className="small">涉及流程：{flows.map((id) => <button key={id} className="link" onClick={() => ex.go({ nav: "changes", flowId: id, card: null, cardView: null }, "flow-focus")}>{nameOfId(ex.model, id).primary}</button>)}</p>} />
  )
}
