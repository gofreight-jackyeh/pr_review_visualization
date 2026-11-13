import { useExplorer, NAV } from "@/app/store"
import { ArchCard } from "@/features/architecture/Architecture"
import { interfaceChanges, ringSummary } from "@/features/architecture/interfaces"
import { ARCH_GROUPS, archGroupOf } from "@/features/architecture/arch"
import { analysisStates, scopeLine } from "@/features/analysis-state/state"
import { Chip } from "@/shared/ui/ui"
import { FindingCard } from "./ReviewCard"
import { AuthorRecord } from "./AuthorRecord"
import { inFacts, reviewLayers } from "./layers"

export const FACTS_KEY = "review-facts"

/**
 * 審查重點 for a human reviewer who comes after an AI review bot: first 「需要你判斷或確認的事」 (one list, ordered by
 * severity, then rule-backed first — see layers.ts), then 「結構事實」 (what static analysis verified), collapsed with
 * its count. Each category keeps its own count — no summed risk score, no conversion between finding severity and
 * interface compatibility. Every card leads with what to confirm and carries one interpretation status.
 */
export function ReviewView() {
  const ex = useExplorer()
  const m = ex.model
  const l = reviewLayers(m)
  const st = analysisStates(m)
  const authorCount = m.overlay?.author_record?.length ?? 0
  const factCount = l.facts.findings.length + l.facts.ifaces.length + l.facts.checks.length
  const open = ex.expanded.has(FACTS_KEY) || inFacts(l, ex.card) // a jump to a structural fact opens the layer
  const empty = (what: string) => <p className="empty-result">本次分析未發現{what}。<span className="muted small">{scopeLine(m)}</span></p>
  const jump = (id: string) => (e: React.MouseEvent) => { e.preventDefault(); const t = document.getElementById(id); t?.scrollIntoView?.({ block: "start" }); t?.focus({ preventScroll: true }) }
  const { counts: c } = l
  // ring line describes only the interface changes listed in this layer; the rest are structural facts below
  const ifaces = interfaceChanges(m.archChecks).filter((x) => l.top.some((t) => t.kind === "arch" && t.id === x.id))
  const verdicts = ARCH_GROUPS.filter((g) => g.key !== "confirm").map((g) => [g, l.facts.checks.filter((x) => archGroupOf(x) === g.key).length] as const).filter(([, n]) => n > 0)
  return (
    <article className="view review" data-testid="review">
      <h2 id="view-title" tabIndex={-1}>{NAV[2].question}</h2>
      <p className="muted small">先列出需要你判斷或確認的事（依嚴重度排序，有文件規則依據的排前面），接著是作者在 PR 說明裡自己的記錄（已定案的決策、驗證狀態、未解問題），最後是靜態分析已確認的結構事實。每張卡先寫需要確認什麼；狀態只依資料本身的確認程度，「來源」只說明內容從哪裡來。各類分開計數，不加總成風險分數。</p>
      <nav className="review-index" aria-label="審查分類">
        <a href="#review-findings" onClick={jump("review-findings")} data-testid="index-focus">需要你判斷或確認的事 {l.top.length} 項</a>
        {authorCount > 0 && <a href="#review-author" onClick={jump("review-author")} data-testid="index-author">作者的記錄 {authorCount} 項</a>}
        <a href="#review-arch" onClick={jump("review-arch")} data-testid="index-facts">結構事實 {factCount} 項</a>
      </nav>
      <section id="review-findings" tabIndex={-1} aria-labelledby="h-focus" data-testid="review-focus">
        <h3 id="h-focus">需要你判斷或確認的事 <span className="count">{l.top.length} 項</span></h3>
        <p className="small review-counts" data-testid="focus-counts">
          待確認事項 {c.findings} 項 · AI 架構疑點 {c.hypotheses} 項 · 需判斷的介面變動 {c.ifaces} 項{c.checks > 0 && ` · 無法判定的規則檢查 ${c.checks} 項`}
        </p>
        {ifaces.length > 0 && <p className="arch-summary small" data-testid="arch-summary">{ringSummary(ifaces)}　<span className="muted">越外圈越靠近客戶端，越不該改。</span></p>}
        {st.includes("pending") && <p className="muted small">AI 疑點產生中，完成後會補在這裡。</p>}
        {(st.includes("unavailable") || st.includes("partial")) && <p className="muted small">這次沒有 AI 疑點，只列出靜態分析的結果。</p>}
        {l.top.length === 0 && empty("需要判斷或確認的事")}
        <div data-testid="focus-list">
          {l.top.map((x) => x.kind === "finding" ? <FindingCard key={x.id} f={x.f} /> : <ArchCard key={x.id} c={x.c} />)}
        </div>
      </section>
      <AuthorRecord />
      <section id="review-arch" tabIndex={-1} aria-labelledby="h-facts" data-testid="review-facts" className="review-facts">
        <h3 id="h-facts">結構事實 <span className="count">{factCount} 項</span>
          {factCount > 0 && <button className="link small" aria-expanded={open} aria-controls="facts-body" data-testid="facts-toggle" onClick={() => ex.toggle(FACTS_KEY, !open)}> {open ? "收合" : "展開"}</button>}
        </h3>
        <p className="muted small" data-testid="facts-counts">
          靜態分析確認的事實，不代表業務上沒問題：靜態發現 {l.facts.findings.length} 項 · 規則檢查 {l.facts.checks.length} 項
          {verdicts.length > 0 && <>（{verdicts.map(([g, n], i) => <span key={g.key}>{i > 0 && "、"}<Chip tone={g.tone}>{g.label}</Chip> {n}</span>)}）</>} · 相容的內部介面變動 {l.facts.ifaces.length} 項
        </p>
        {factCount === 0 && empty("結構事實")}
        {open && factCount > 0 && (
          <div id="facts-body">
            {l.facts.findings.length > 0 && <div data-testid="facts-findings"><h4>靜態發現 <span className="count">{l.facts.findings.length} 項</span></h4>{l.facts.findings.map((f) => <FindingCard key={f.id} f={f} />)}</div>}
            {l.facts.checks.length > 0 && <div data-testid="facts-checks"><h4>規則檢查 <span className="count">{l.facts.checks.length} 項</span></h4>{l.facts.checks.map((x) => <ArchCard key={x.id} c={x} />)}</div>}
            {l.facts.ifaces.length > 0 && <div data-testid="facts-ifaces"><h4>相容的內部介面變動 <span className="count">{l.facts.ifaces.length} 項</span></h4>{l.facts.ifaces.map((x) => <ArchCard key={x.id} c={x} />)}</div>}
          </div>
        )}
      </section>
    </article>
  )
}
