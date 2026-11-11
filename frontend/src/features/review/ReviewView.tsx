import { useExplorer, NAV } from "@/app/store"
import { statusOf, STATUS, type Status } from "@/entities/artifact/provenance"
import { DependencyChecks, InterfaceChanges } from "@/features/architecture/Architecture"
import { interfaceChanges, ringSummary } from "@/features/architecture/interfaces"
import { analysisStates, scopeLine } from "@/features/analysis-state/state"
import { FindingCard } from "./ReviewCard"

const ORDER: Status[] = ["needs_confirmation", "undetermined", "confirmed"]

/**
 * 審查重點: three categories, each counted on its own — no summed risk score, no conversion between finding severity and
 * interface compatibility. Every card leads with what to confirm and carries one interpretation status.
 */
export function ReviewView() {
  const ex = useExplorer()
  const m = ex.model
  const ifaces = interfaceChanges(m.archChecks)
  const deps = m.archChecks.filter((c) => c.check !== "interface_change")
  const findings = m.overlay?.findings ?? []
  const st = analysisStates(m)
  const empty = (what: string) => <p className="empty-result">本次分析未發現{what}。<span className="muted small">{scopeLine(m)}</span></p>
  const jump = (id: string) => (e: React.MouseEvent) => { e.preventDefault(); const t = document.getElementById(id); t?.scrollIntoView?.({ block: "start" }); t?.focus({ preventScroll: true }) }
  return (
    <article className="view review" data-testid="review">
      <h2 id="view-title" tabIndex={-1}>{NAV[2].question}</h2>
      <p className="muted small">每張卡先寫需要確認什麼，再列出已知事實與不確定之處。卡片上的狀態只依資料本身的確認程度；「來源」只說明內容從哪裡來。各類分開計數，不加總成風險分數。</p>
      <nav className="review-index" aria-label="審查分類">
        <a href="#review-iface" onClick={jump("review-iface")}>對外介面變動 {ifaces.length} 項</a>
        <a href="#review-arch" onClick={jump("review-arch")}>架構一致性 {deps.length} 項</a>
        <a href="#review-findings" onClick={jump("review-findings")}>待確認事項 {findings.length} 項</a>
      </nav>
      <section id="review-iface" tabIndex={-1} aria-labelledby="h-iface" data-testid="review-iface">
        <h3 id="h-iface">對外介面變動 <span className="count">{ifaces.length} 項</span></h3>
        {ifaces.length > 0 && <p className="arch-summary" data-testid="arch-summary"><b>{ringSummary(m.archChecks)}</b>　<span className="muted small">依圈別由外往內；越外圈越靠近客戶端，越不該改。</span></p>}
        {ifaces.length ? <InterfaceChanges /> : empty("對外介面變動")}
      </section>
      <section id="review-arch" tabIndex={-1} aria-labelledby="h-arch" data-testid="review-arch">
        <h3 id="h-arch">架構一致性 <span className="count">{deps.length} 項</span></h3>
        <p className="muted small">依賴規則來自 Repository 文件（例如 .importlinter.ini）或整個系統的依賴統計；AI 提出的疑點一律待確認。</p>
        {deps.length ? <DependencyChecks /> : empty("架構規則檢查結果")}
      </section>
      <section id="review-findings" tabIndex={-1} aria-labelledby="h-findings" data-testid="review-findings">
        <h3 id="h-findings">待確認事項 <span className="count">{findings.length} 項</span></h3>
        {st.includes("pending") && <p className="muted small">AI 疑點產生中，完成後會補在這裡。</p>}
        {(st.includes("unavailable") || st.includes("partial")) && <p className="muted small">這次沒有 AI 疑點，只列出靜態分析的結果。</p>}
        {!findings.length && empty("待確認事項")}
        {ORDER.map((s) => {
          const list = findings.filter((f) => statusOf({ finding: f }) === s)
          if (!list.length) return null
          return (
            <div key={s} data-testid={`findings-${s}`}>
              <h4 className="status-group">{STATUS[s].glyph} {STATUS[s].label} <span className="count">{list.length} 項</span></h4>
              {list.map((f) => <FindingCard key={f.id} f={f} />)}
            </div>
          )
        })}
      </section>
    </article>
  )
}
