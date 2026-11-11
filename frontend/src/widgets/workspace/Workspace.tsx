import { NAV, useExplorer } from "@/app/store"
import { MapTab } from "@/features/system-map/MapTab"
import { DetailPanel } from "@/features/detail/DetailPanel"
import { Summary } from "@/features/summary/Summary"
import { ChangesView } from "@/features/changes/ChangesView"
import { ReviewView } from "@/features/review/ReviewView"
import { StateNotices } from "@/features/analysis-state/StateNotices"

/** The single main navigation (plan「主導覽與閱讀順序」). Each entry is a history entry (browser back / forward). */
export function MainNav() {
  const ex = useExplorer()
  const items = ex.model.overlay ? NAV : NAV.filter((n) => n.key === "context")
  return (
    <nav className="main-nav" aria-label="主導覽">
      <ul>
        {items.map((n) => (
          <li key={n.key}><button aria-current={ex.nav === n.key ? "page" : undefined} className={ex.nav === n.key ? "on" : ""} data-nav={n.key} onClick={() => ex.go({ nav: n.key })}>{n.label}</button></li>
        ))}
      </ul>
    </nav>
  )
}

function ContextView() {
  return (
    <div className="view context" data-testid="context">
      <h2 id="view-title" tabIndex={-1} className="sr-only">{NAV[3].question}</h2>
      <MapTab />
    </div>
  )
}

/** Main content plus the on-demand detail panel (beside it on wide screens, an overlay on narrower ones; see CSS). */
export function Workspace({ onReanalyze }: { onReanalyze?: () => void }) {
  const ex = useExplorer()
  return (
    <div className="workspace">
      <MainNav />
      <div className={"split" + (ex.panel ? " with-panel" : "")}>
        <main className={"content" + (ex.nav === "context" ? " fill" : "")} ref={ex.scrollRef} data-testid="content">
          <StateNotices onReanalyze={onReanalyze} />
          {ex.nav === "summary" && <Summary />}
          {ex.nav === "changes" && <ChangesView />}
          {ex.nav === "review" && <ReviewView />}
          {ex.nav === "context" && <ContextView />}
        </main>
        <DetailPanel />
      </div>
    </div>
  )
}
