import { useMemo } from "react"
import { useExplorer } from "@/app/store"
import { MapView } from "@/features/system-map/MapView"
import { scopeLine } from "@/features/analysis-state/state"
import { localGraph, localScene, LOCAL_TARGET, type LNode } from "./local"

/**
 * 查看相關功能: the local dependency graph of one change card, with an equivalent text list. A dependency graph says
 * who depends on whom; it is not an execution order and not a proven business impact (that is said on screen).
 */
export function RelationView({ cardId, capIds }: { cardId: string; capIds: string[] }) {
  const ex = useExplorer()
  const m = ex.model
  const key = `local:${cardId}`
  const page = ex.pages[key] ?? 0
  const g = useMemo(() => localGraph(m, capIds, page), [m, capIds, page])
  const scene = useMemo(() => localScene(m, g, key), [m, g, key])
  const allOpen = ex.expanded.has(`rel-all:${cardId}`)
  const item = (n: LNode) => (
    <li key={n.id}>
      <button className="link" onClick={(e) => ex.openDetail({ kind: "capability", id: n.id }, e.currentTarget, cardId)}>{n.label}</button>：{n.note}
      {n.hints > 0 && <span className="hint-count">　審查提示 {n.hints} 項</span>}
    </li>
  )
  const drawnCount = g.nodes.length + (g.group ? 1 : 0)
  return (
    <section id="relation-focus" tabIndex={-1} className="relation-focus" data-testid="relation-view" aria-labelledby="rel-title">
      <h3 id="rel-title">相關功能 <span className="kind-note">依賴關係圖：誰依賴誰</span></h3>
      <p className="disclaimer small" role="note">⚠ 線條是靜態分析找到的程式依賴，說明誰呼叫或依賴誰；不是執行順序，也不代表一定影響業務。業務步驟的先後請看流程。</p>
      {g.all.length === 0 ? <p className="empty-result">本次分析未發現這項修改涉及的功能與其他功能之間的依賴。<span className="muted small">{scopeLine(m)}</span></p> : (
        <>
          <p className="small muted" data-testid="relation-count">圖上 {drawnCount} 個節點（目標約 {LOCAL_TARGET - 2}–{LOCAL_TARGET} 個）{g.group ? `；其餘 ${g.group.members.length} 個相關功能已分組，可展開` : ""}。點節點或下方名稱可看說明。</p>
          <div className="rel-map"><MapView scene={scene} view="overlay" /></div>
          <h4>文字清單（與圖等價）</h4>
          <ul className="rel-list" data-testid="relation-list">{g.edges.map((e) => <li key={e.id} data-violation={e.violations.length > 0 || undefined}>{e.violations.length > 0 && <b>⚠ </b>}{e.sentence}</li>)}</ul>
          <ul className="plain small" data-testid="relation-nodes">{g.nodes.map(item)}</ul>
          {g.group && (
            <div className="rel-group" data-testid="relation-group">
              <p><b>其他 {g.group.members.length} 個相關功能</b>{g.group.hints > 0 && <>，<span className="hint-count">含 {g.group.hints} 項審查提示</span></>}
                {g.group.pages > 1 && <button className="mini" onClick={() => ex.nextPage(key)}>圖上換下一批（第 {g.group.page + 1}/{g.group.pages} 批）</button>}
                <button className="mini" aria-expanded={allOpen} onClick={() => ex.toggle(`rel-all:${cardId}`)}>{allOpen ? "收起" : "展開清單"}</button></p>
              {allOpen && <>
                <ul className="plain small" data-testid="relation-group-nodes">{g.group.members.map(item)}</ul>
                <ul className="rel-list">{g.group.edges.map((e) => <li key={e.id}>{e.violations.length > 0 && <b>⚠ </b>}{e.sentence}</li>)}</ul>
              </>}
            </div>
          )}
        </>
      )}
    </section>
  )
}
