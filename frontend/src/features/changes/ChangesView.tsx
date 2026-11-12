import { flowList, flowSteps, nameOfId, type Model } from "@/entities/artifact/model"
import { useExplorer, NAV } from "@/app/store"
import { Chip, FileRow, TextBlock } from "@/shared/ui/ui"
import { ViewFlowLabel } from "./FlowName"
import { FlowFocus } from "@/features/flows/FlowFocus"
import { RelationView } from "@/features/relations/RelationView"
import { IMPACT_SENTENCE } from "@/features/relations/wording"
import { CHANGE_LABEL, COMPAT } from "@/features/architecture/interfaces"
import { AI_MISSING, aiMissing, scopeLine } from "@/features/analysis-state/state"
import { cardDomId, changeCards, missingBefore, STEP_COUNT_LABEL, stepCounts, type ChangeCard } from "./cards"
import { StaticGroups } from "./StaticChange"

const names = (m: Model, ids: string[]) => ids.map((id) => nameOfId(m, id).primary).join("、")

/** What the flow itself shows about this PR, from flow_changes (a structural fact). */
function flowFact(m: Model, flowId: string): string {
  const fc = m.flowChange.get(flowId)
  if (!fc) return "這條流程本身的步驟沒有變動"
  if (fc.change_status === "added") return "這條流程是這個 PR 新增的"
  if (fc.change_status === "removed") return "這條流程被這個 PR 移除"
  const c = stepCounts(flowSteps(m, flowId).steps)
  return `流程步驟有變動：${(["added", "removed", "changed"] as const).filter((k) => c[k]).map((k) => `${STEP_COUNT_LABEL[k]} ${c[k]} 步`).join("、")}`
}

/** Why a flow is shown, in words backed by the data that put it there. */
export function whyFlow(m: Model, flowId: string, card?: ChangeCard): string {
  if (card?.kind === "behavior") return `這項前後差異的分析結果列出了這條流程；${flowFact(m, flowId)}（靜態分析）。`
  if (card?.kind === "flow") return `${flowFact(m, flowId)}（靜態分析）；沒有業務說明，不描述業務差異。`
  const imp = m.impact.get(flowId)
  if (imp) return `影響分析：${IMPACT_SENTENCE[imp.impact_status]}${imp.impact_depth ? `（程式依賴距離 ${imp.impact_depth} 層）` : ""}；${flowFact(m, flowId)}。`
  return "與這次修改沒有已知關係，供了解背景。"
}

function CardBody({ c, m }: { c: ChangeCard; m: Model }) {
  if (c.kind === "behavior") {
    const why = missingBefore(m, c.bc)
    return (
      <>
        <div className="ba-row"><span className="ba-tag before">修改前</span>
          {c.bc.before ? <TextBlock t={c.bc.before} /> : <p className="textblock" data-before={why}>{why === "new_flow" ? "不存在：這是新增的流程。" : "無法判定：分析結果沒有提供修改前的說明。"}</p>}</div>
        <div className="ba-row"><span className="ba-tag after">修改後</span><TextBlock t={c.bc.after} /></div>
      </>
    )
  }
  if (c.kind === "flow") {
    return (
      <>
        <p className="textblock">流程「{nameOfId(m, c.flowId).primary}」{c.fc.change_status === "added" ? "是這個 PR 新增的" : c.fc.change_status === "removed" ? "被這個 PR 移除" : "的步驟有變動"}：
          {(["added", "removed", "changed"] as const).filter((k) => c.counts[k]).map((k) => <span key={k} className={"step-count sc-" + k}>{STEP_COUNT_LABEL[k]} {c.counts[k]} 步</span>)}
          <span className="source-note">來源：靜態分析；沒有業務語言的說明（AI 說明{AI_MISSING[aiMissing(m)]}），不描述業務差異</span></p>
        <StaticGroups m={m} flowId={c.flowId} />
      </>
    )
  }
  return (
    <>
      <p className="textblock">{c.capId ? <>功能「{nameOfId(m, c.capId).primary}」的檔案有修改，沒有對應的業務流程。</> : "這些檔案不屬於任何功能（例如 CI、設定），沒有對應的業務流程。"} <span className="source-note">來源：靜態分析</span></p>
      <ul className="plain files">{c.files.map((f) => <FileRow key={f.id} f={f} ov={m.overlay} />)}</ul>
      {c.interfaces.length > 0 && <ul className="plain">{c.interfaces.map((a) => <li key={a.id} className="small">介面{CHANGE_LABEL[a.interface_change!.change]}：{a.title} <Chip tone={COMPAT[a.interface_change!.compatibility].tone}>{COMPAT[a.interface_change!.compatibility].label}</Chip></li>)}</ul>}
    </>
  )
}

export function ChangeCardView({ c }: { c: ChangeCard }) {
  const ex = useExplorer()
  const m = ex.model
  const on = ex.card === c.id
  const openFlow = on && ex.cardView === "flow" && ex.flowId && c.flowIds.includes(ex.flowId) ? ex.flowId : null
  return (
    <>
      <article id={cardDomId(c.id)} tabIndex={-1} className={"change-card ck-" + c.kind + (on ? " on" : "")} aria-current={on ? "true" : undefined} data-testid={`card-${c.id}`}>
        <CardBody c={c} m={m} />
        {c.capIds.length > 0 && <p className="muted small">涉及功能：{names(m, c.capIds)}</p>}
        <div className="row wrap card-actions">
          {c.flowIds.map((f) => (
            <button key={f} data-testid={`card-flow-${f}`} aria-pressed={openFlow === f} className={openFlow === f ? "on" : ""}
              onClick={() => ex.go({ nav: "changes", card: c.id, cardView: "flow", flowId: f }, "flow-focus")}><ViewFlowLabel n={nameOfId(m, f)} /></button>
          ))}
          {c.capIds.length > 0 && (
            <button data-testid={`card-rel-${c.id}`} aria-pressed={on && ex.cardView === "relations"} className={on && ex.cardView === "relations" ? "on" : ""}
              onClick={() => ex.go({ nav: "changes", card: c.id, cardView: "relations" }, "relation-focus")}>查看相關功能</button>
          )}
        </div>
      </article>
      {openFlow && <FlowFocus why={whyFlow(m, openFlow, c)} />}
      {on && ex.cardView === "relations" && <RelationView cardId={c.id} capIds={c.capIds} />}
    </>
  )
}

/** 變更與影響: concrete change cards lead into their flows and related features; other flows sit behind an entry. */
export function ChangesView() {
  const ex = useExplorer()
  const m = ex.model
  const cards = changeCards(m)
  const onCards = new Set(cards.flatMap((c) => c.flowIds))
  const items = flowList(m)
  const related = items.filter((i) => i.related && !onCards.has(i.flow.id))
  const others = items.filter((i) => !i.related)
  const othersOpen = ex.expanded.has("other-flows")
  const looseFlow = ex.cardView === "flow" && ex.flowId && !cards.some((c) => c.id === ex.card) ? ex.flowId : null
  const flowBtn = (id: string) => (
    <button data-testid={`flow-${id}`} aria-pressed={looseFlow === id} className={"link" + (looseFlow === id ? " on" : "")}
      onClick={() => ex.go({ nav: "changes", card: null, cardView: "flow", flowId: id }, "flow-focus")}>{nameOfId(m, id).primary}</button>
  )
  return (
    <article className="view changes" data-testid="changes">
      <h2 id="view-title" tabIndex={-1}>{NAV[1].question}</h2>
      <p className="muted small">從具體的修改開始：先看修改前後，再看它經過的流程或相關的功能。</p>
      {cards.length === 0 ? <p className="empty-result">本次分析未發現修改。<span className="muted small">{scopeLine(m)}</span></p> : cards.map((c) => <ChangeCardView key={c.id} c={c} />)}
      {related.length > 0 && (
        <section aria-labelledby="h-related" data-testid="related-flows">
          <h3 id="h-related">也可能相關的流程 <span className="count">{related.length} 條</span></h3>
          <ul className="plain">{related.map((i) => <li key={i.flow.id}>{flowBtn(i.flow.id)}　<span className="small muted">{i.impact !== "none" ? IMPACT_SENTENCE[i.impact] : "與修改的功能有關"}</span></li>)}</ul>
        </section>
      )}
      {others.length > 0 && (
        <section aria-labelledby="h-others">
          <h3 id="h-others"><button className="disclosure" aria-expanded={othersOpen} onClick={() => ex.toggle("other-flows")} data-testid="other-flows-toggle">
            {othersOpen ? "▾" : "▸"} 其他流程（與這次修改沒有已知關係）<span className="count">{others.length} 條</span></button></h3>
          {othersOpen && <ul className="plain cols" data-testid="other-flows">{others.map((i) => <li key={i.flow.id}>{flowBtn(i.flow.id)}</li>)}</ul>}
        </section>
      )}
      {looseFlow && <FlowFocus why={whyFlow(m, looseFlow)} />}
    </article>
  )
}
