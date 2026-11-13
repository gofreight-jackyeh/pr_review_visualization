import { nameOfId, namesOf, type Model } from "@/entities/artifact/model"
import { useExplorer } from "@/app/store"
import { FileRow, SourceNote, TextBlock } from "@/shared/ui/ui"
import { Markdown } from "@/shared/ui/Markdown"
import type { Text } from "@/entities/artifact/types"
import { firstSentence, LONG_TEXT } from "./sentence"
import { ReadingPath } from "@/features/guide/Guide"
import { AI_MISSING, aiMissing, analysisStates, scopeLine } from "@/features/analysis-state/state"
import { RetryAi } from "@/features/analysis-state/RetryAi"
import { interfaceChanges } from "@/features/architecture/interfaces"
import { cardDomId, changeCards, missingBefore, STEP_COUNT_LABEL, type ChangeCard } from "@/features/changes/cards"
import { StaticGroups } from "@/features/changes/StaticChange"
import { aiLabel } from "@/shared/ai/ai"

const names = (m: Model, ids: string[]) => ids.map((id) => nameOfId(m, id).primary).join("、")

/** One-line label of a card for the 主要差異 choice and the 已辨識的修改 list (names only, no new claims). */
export function cardLabel(m: Model, c: ChangeCard): string {
  if (c.kind === "behavior") return names(m, c.capIds) || names(m, c.flowIds) || c.id
  if (c.kind === "flow") return `${nameOfId(m, c.flowId).primary}（流程）`
  return c.capId ? nameOfId(m, c.capId).primary : "不屬於任何功能的檔案"
}

/** A before/after text: very long ones are clamped to ~4 lines with an in-place 「顯示全文」. */
function ClampedText({ id, t }: { id: string; t: Text }) {
  const ex = useExplorer()
  const long = t.text.length > LONG_TEXT
  const open = ex.expanded.has(`clamp:${id}`)
  if (!long) return <TextBlock t={t} />
  return (
    <div className="clamped">
      <p className={"textblock" + (open ? "" : " clamp4")} data-testid="clamped-text">{t.text} <SourceNote source={t.source} /></p>
      <button className="link small" aria-expanded={open} onClick={() => ex.toggle(`clamp:${id}`, !open)}>{open ? "收合" : "顯示全文"}</button>
    </div>
  )
}

/**
 * 這次修改: Claude's purpose as text; the PR-description fallback as safe Markdown, labelled with why there is no AI
 * summary, clamped with an in-place 「展開全文」.
 */
function Purpose({ m, t }: { m: Model; t: Text }) {
  const ex = useExplorer()
  if (t.source !== "pr_description") return <TextBlock t={t} />
  const long = t.text.length > LONG_TEXT || t.text.split("\n").length > 4
  const open = ex.expanded.has("clamp:purpose")
  return (
    <div className="purpose-md" data-testid="purpose-md">
      <p className="source-note" data-testid="purpose-label" title="直接取自 PR 標題與描述，未經 AI 說明">PR 描述（AI 摘要{AI_MISSING[aiMissing(m)]}）<RetryAi /></p>
      <Markdown text={t.text} className={long && !open ? "md-clamp" : ""} />
      {long && <button className="link small" aria-expanded={open} data-testid="purpose-more" onClick={() => ex.toggle("clamp:purpose", !open)}>{open ? "收合" : "展開全文"}</button>}
    </div>
  )
}

/** 相關背景: only the first sentence of the system summary; the rest expands in place. */
function Background({ m, bizCaps }: { m: Model; bizCaps: number }) {
  const ex = useExplorer()
  const sum = m.map.system.summary
  const open = ex.expanded.has("summary-bg")
  const { first, rest } = sum ? firstSentence(sum.text) : { first: "", rest: "" }
  return (
    <section aria-labelledby="sum-bg">
      <h3 id="sum-bg">相關背景</h3>
      <div className="row wrap bg-line">
        {sum ? <p className="textblock" data-testid="bg-text">{open ? sum.text : first} <SourceNote source={sum.source} /></p>
          : <p className="textblock">{m.map.system.technical_name}：{bizCaps} 個業務模組、{m.map.entry_points.length} 個入口。<span className="muted small">（沒有系統的業務說明）</span></p>}
        {rest || !sum ? <button className="link" aria-expanded={open} data-testid="bg-more" onClick={() => ex.toggle("summary-bg", !open)}>{open ? "收起背景" : "更多背景"}</button> : null}
        {(open || !rest) && <button className="link" onClick={() => ex.go({ nav: "context", phase: "system", level: {} })}>查看專案背景</button>}
      </div>
    </section>
  )
}

function Counts({ m }: { m: Model }) {
  const ifaces = interfaceChanges(m.archChecks)
  const breaking = ifaces.filter((c) => c.interface_change!.compatibility === "breaking").length
  const findings = m.overlay?.findings.length ?? 0
  const st = analysisStates(m)
  const aiNote = st.includes("pending") ? "（AI 疑點產生中）" : st.includes("partial") || st.includes("unavailable") ? "（未包含 AI 疑點）" : ""
  return (
    <div className="counts" data-testid="summary-counts">
      <span data-testid="count-iface">介面變動：{ifaces.length ? <b>{ifaces.length} 項</b> : "本次分析未發現"}{breaking > 0 && <>（其中 <b>{breaking} 項可能破壞客戶端</b>）</>}</span>
      <span data-testid="count-findings">待確認事項：{findings ? <b>{findings} 項</b> : "本次分析未發現"}{aiNote}</span>
      {(!ifaces.length || !findings) && <span className="muted small scope-inline" data-testid="count-scope">{scopeLine(m)}</span>}
    </div>
  )
}

function MainDifference({ m, cards }: { m: Model; cards: ChangeCard[] }) {
  const ex = useExplorer()
  const st = analysisStates(m)
  const behaviors = cards.filter((c): c is Extract<ChangeCard, { kind: "behavior" }> => c.kind === "behavior")
  if (!behaviors.length) {
    const lead = st.includes("pending") ? "業務差異產生中，先列出已辨識的修改："
      : st.includes("partial") ? "AI 業務說明產生失敗，這裡不拼湊業務差異。已辨識的修改："
      : st.includes("unavailable") ? "業務說明不可用，這裡不拼湊業務差異。已辨識的修改："
      : "本次分析未發現業務行為差異。已辨識的修改："
    return (
      <div className="diff-card static" data-testid="main-diff-static">
        <p>{lead}</p>
        <ul className="plain known-changes">{cards.map((c) => <li key={c.id}>{cardLabel(m, c)}{c.kind === "flow" && <span className="muted small">　{(["added", "removed", "changed"] as const).filter((k) => c.counts[k]).map((k) => `${STEP_COUNT_LABEL[k]} ${c.counts[k]} 步`).join("、")}</span>}
          {c.kind === "tech" && <span className="muted small">　{c.files.length} 個檔案</span>}
          {c.kind === "flow" && <StaticGroups m={m} flowId={c.flowId} />}
          {c.kind === "tech" && <ul className="plain files">{c.files.map((f) => <FileRow key={f.id} f={f} ov={m.overlay} />)}</ul>}</li>)}</ul>
        <p className="muted small">{scopeLine(m)}</p>
      </div>
    )
  }
  const cur = behaviors.find((c) => c.id === ex.card) ?? behaviors[0]
  const why = missingBefore(m, cur.bc)
  return (
    <>
      {behaviors.length > 1 && (
        <div className="diff-pick" role="radiogroup" aria-label="選擇要看的差異" data-testid="diff-pick">
          <p className="muted small">共 {behaviors.length} 項差異，依分析結果的順序列出，不代表重要性。</p>
          {behaviors.map((c, i) => (
            <button key={c.id} role="radio" aria-checked={c.id === cur.id} className={c.id === cur.id ? "on" : ""} onClick={() => ex.setCard(c.id)}>差異 {i + 1}：{cardLabel(m, c)}</button>
          ))}
        </div>
      )}
      <div className="diff-card" data-testid="main-diff">
        <div className="ba-row"><span className="ba-tag before">修改前</span>
          {cur.bc.before ? <ClampedText id={`${cur.id}:before`} t={cur.bc.before} /> : <p className="textblock">{why === "new_flow" ? "不存在：這是新增的流程。" : "無法判定：分析結果沒有提供修改前的說明。"}</p>}</div>
        <div className="ba-row"><span className="ba-tag after">修改後</span><ClampedText id={`${cur.id}:after`} t={cur.bc.after} /></div>
        {cur.capIds.length > 0 && <p className="muted small">涉及功能：{names(m, cur.capIds)}</p>}
      </div>
    </>
  )
}

/** PR 摘要 (plan「PR 摘要首屏」): what changed and why, then one primary action; deeper content follows below. */
export function Summary() {
  const ex = useExplorer()
  const m = ex.model, ov = m.overlay
  if (!ov) {
    return (
      <article className="view summary">
        <h2 id="view-title" tabIndex={-1}>{m.map.system.technical_name}</h2>
        <p>這份結果沒有 PR 資料，只有系統地圖。</p>
        <button className="primary" onClick={() => ex.go({ nav: "context" })}>查看專案背景</button>
      </article>
    )
  }
  const cards = changeCards(m)
  const github = ov.pr.source === "github" && ov.pr.url ? ov.pr.url : null
  const target = cards.find((c) => c.id === ex.card && c.kind === "behavior") ?? cards[0]
  const bizCaps = m.caps.filter((c) => c.category === "business" && !m.headOnly.has(c.id)).length
  return (
    <article className="view summary" data-testid="summary">
      <header className="sum-head">
        <h2 id="view-title" tabIndex={-1}>PR：{ov.pr.title}</h2>
        {github && <a className="btn" href={github} target="_blank" rel="noreferrer noopener">前往 GitHub ↗</a>}
      </header>
      <Background m={m} bizCaps={bizCaps} />
      <section aria-labelledby="sum-purpose">
        <h3 id="sum-purpose">這次修改</h3>
        <Purpose m={m} t={ov.purpose} />
        {aiLabel(ov.semantic.ai) && <p className="muted small" data-testid="ai-used">AI 說明：{aiLabel(ov.semantic.ai)}</p>}
      </section>
      <div className="next-row" data-testid="next-row">
        <button className="primary big" data-testid="go-changes" onClick={() => ex.go({ nav: "changes", card: target?.id ?? null }, target ? cardDomId(target.id) : undefined)}>查看這次修改</button>
        <Counts m={m} />
        <button data-testid="go-review" onClick={() => ex.go({ nav: "review" })}>查看審查重點</button>
      </div>
      <section aria-labelledby="sum-diff">
        <h3 id="sum-diff">主要差異</h3>
        <MainDifference m={m} cards={cards} />
      </section>
      <hr />
      <ReadingPath />
      <details className="scope" open={ex.expanded.has("summary-scope")} onToggle={(e) => ex.toggle("summary-scope", (e.target as HTMLDetailsElement).open)}>
        <summary>分析範圍與版本</summary>
        <p className="small">{scopeLine(m)}</p>
        <p className="small muted">分析的版本：{ov.pr.head_ref} <code className="tech">{ov.pr.head_sha.slice(0, 7)}</code>（base {ov.pr.base_ref} <code className="tech">{ov.pr.merge_base_sha.slice(0, 7)}</code>）</p>
        {ov.design_rationale && <><h4>設計考量</h4><TextBlock t={ov.design_rationale} /></>}
        {m.domains.length > 0 && <p className="small muted">系統的業務領域：{m.domains.map((d) => namesOf(d).primary).join("、")}</p>}
      </details>
    </article>
  )
}
