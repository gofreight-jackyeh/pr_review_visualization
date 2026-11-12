import { NAV, useExplorer } from "@/app/store"
import { MapTab } from "@/features/system-map/MapTab"
import { DetailPanel } from "@/features/detail/DetailPanel"
import { Summary } from "@/features/summary/Summary"
import { ChangesView } from "@/features/changes/ChangesView"
import { ReviewView } from "@/features/review/ReviewView"
import { StateNotices } from "@/features/analysis-state/StateNotices"
import { AskButton, AskPanel } from "@/features/ask/AskPanel"
import { useAskAccess } from "@/features/ask/ask"
import { InfraFiles, InfraNotice } from "./InfraFiles"
import { isInfraRepo } from "./infra"

/** Infrastructure repositories: review focus right after the summary, changed files by module instead of flows, and
 * the (directory-only) project map last and de-emphasised. Same keys, so history and deep links keep working. */
const INFRA_NAV = (["summary", "review", "changes", "context"] as const).map((k) => {
  const n = NAV.find((x) => x.key === k)!
  return { ...n, label: k === "changes" ? "變更檔案" : k === "context" ? "專案背景（有限）" : n.label, dim: k === "context" }
})

/** The single main navigation (plan「主導覽與閱讀順序」). Each entry is a history entry (browser back / forward). */
export function MainNav() {
  const ex = useExplorer()
  const items: { key: (typeof NAV)[number]["key"]; label: string; dim?: boolean }[] =
    !ex.model.overlay ? NAV.filter((n) => n.key === "context") : isInfraRepo(ex.model) ? INFRA_NAV : NAV
  return (
    <nav className="main-nav" aria-label="主導覽">
      <ul>
        {items.map((n) => (
          <li key={n.key}><button aria-current={ex.nav === n.key ? "page" : undefined} className={(ex.nav === n.key ? "on" : "") + (n.dim ? " dim" : "")} data-nav={n.key} onClick={() => ex.go({ nav: n.key })}>{n.label}</button></li>
        ))}
      </ul>
    </nav>
  )
}

function ContextView() {
  const infra = isInfraRepo(useExplorer().model)
  return (
    <div className="view context" data-testid="context">
      <h2 id="view-title" tabIndex={-1} className="sr-only">{NAV[3].question}</h2>
      {infra && <p className="infra-context-note small" data-testid="infra-context-note">這個 repository 沒有程式進入點，地圖只反映目錄結構，沒有業務流程。</p>}
      <MapTab />
    </div>
  )
}

/** Main content plus the on-demand detail panel or the 問 AI panel (one at a time; beside the content on wide screens,
 * an overlay on narrower ones; see CSS). */
export function Workspace() {
  const ex = useExplorer()
  const ask = useAskAccess(ex.model, ex.fixture)
  const askOpen = ex.ask && !ex.panel && ask.state !== "hidden" && ask.state !== "loading"
  const infra = isInfraRepo(ex.model)
  return (
    <div className="workspace">
      {/* 問 AI opens a panel, it is not a navigation entry: beside the nav landmark, reachable from every tab */}
      <div className="nav-row"><MainNav /><AskButton access={ask} /></div>
      <div className={"split" + (ex.panel || askOpen ? " with-panel" : "")}>
        <main className={"content" + (ex.nav === "context" ? " fill" : "")} ref={ex.scrollRef} data-testid="content">
          <StateNotices />
          {ex.nav === "summary" && infra && <InfraNotice />}
          {ex.nav === "summary" && <Summary />}
          {ex.nav === "changes" && (infra ? <InfraFiles /> : <ChangesView />)}
          {ex.nav === "review" && <ReviewView />}
          {ex.nav === "context" && <ContextView />}
        </main>
        <DetailPanel />
        {askOpen && <AskPanel access={ask} />}
      </div>
    </div>
  )
}
