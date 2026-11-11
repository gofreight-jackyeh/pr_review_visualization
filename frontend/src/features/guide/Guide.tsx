import { useState } from "react"
import type { WalkTarget } from "@/entities/artifact/types"
import { useExplorer } from "@/app/store"
import { TextBlock } from "@/shared/ui/ui"
import { INSUFFICIENT, matchQuestion, type MatchResult } from "./matcher"

const GO_LABEL: Record<WalkTarget["view"], string> = {
  system: "看專案背景", overlay: "看 PR 影響範圍", flow: "看這條流程", findings: "看審查重點", architecture: "看架構一致性", evidence: "看審查重點",
}

/** Navigation is opt-in (owner 2026-11-11): reading an explanation never moves the view; this button does. */
function GoButton({ target }: { target: WalkTarget }) {
  const ex = useExplorer()
  return <button className="mini" data-testid="guide-go" onClick={() => ex.navigate(target)}>{GO_LABEL[target.view]} →</button>
}

/**
 * The pre-generated walkthrough (spec §9) as a suggested reading path, not a tutorial: every item expands in place,
 * nothing is required, and only 「看…」 navigates.
 */
export function ReadingPath() {
  const ex = useExplorer()
  const wt = ex.model.overlay?.walkthrough
  const [text, setText] = useState("")
  const [answer, setAnswer] = useState<MatchResult | null>(null)
  if (!wt || (!wt.steps.length && !wt.questions.length)) return null
  const toggle = (n: number) => ex.setGuideStep(ex.guideStep === n ? -1 : n)
  // the opened answer lives in the history-backed expanded set, so Back after 「看…」 shows it again
  const open = wt.questions.find((q) => ex.expanded.has(`faq:${q.id}`))
  const picked = open ?? (answer?.kind === "match" ? answer.question : null)
  const ask = (q: (typeof wt.questions)[number]) => {
    setAnswer(null)
    for (const o of wt.questions) if (o.id !== q.id) ex.toggle(`faq:${o.id}`, false)
    ex.toggle(`faq:${q.id}`)
  }
  return (
    <section className="reading-path" aria-labelledby="rp-title" data-testid="reading-path">
      <h3 id="rp-title">建議閱讀路徑</h3>
      <p className="muted small">可依需要跳著看；點標題只在原地展開說明。</p>
      <ol className="plain">
        {wt.steps.map((s, i) => (
          <li key={s.id} className={"gstep" + (i === ex.guideStep ? " on" : "")} data-testid={`guide-${s.id}`}>
            <button className="gstep-toggle" onClick={() => toggle(i)} aria-expanded={i === ex.guideStep}><span aria-hidden>{i === ex.guideStep ? "▾" : "▸"}</span> {i + 1}. {s.title}</button>
            {i === ex.guideStep && <div className="gstep-body"><TextBlock t={s.body} /><GoButton target={s.target} /></div>}
          </li>
        ))}
      </ol>
      {wt.questions.length > 0 && (
        <>
          <h4>常見問題</h4>
          <ul className="plain">{wt.questions.map((q) => <li key={q.id}><button className="qbtn" aria-expanded={picked?.id === q.id} onClick={() => ask(q)}>{q.question}</button></li>)}</ul>
          <form className="row" onSubmit={(e) => { e.preventDefault(); const r = matchQuestion(text, wt.questions); if (r.kind === "match") ask(r.question); else { for (const o of wt.questions) ex.toggle(`faq:${o.id}`, false); setAnswer(r) } }}>
            <input className="grow" value={text} onChange={(e) => setText(e.target.value)} placeholder="輸入問題（只會比對上方的既有問題）" aria-label="自由提問" />
            <button type="submit">詢問</button>
          </form>
          {answer?.kind === "insufficient" && <p className="notice warn" role="status">{INSUFFICIENT}</p>}
          {picked && <div className="notice info" role="status"><b>{picked.question}</b><TextBlock t={picked.answer} /><GoButton target={picked.target} /></div>}
        </>
      )}
    </section>
  )
}
