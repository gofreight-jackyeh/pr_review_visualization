import { Capped } from "@/features/detail/Capped"
import { nameOfId, namesOf } from "@/entities/artifact/model"
import type { Capability } from "@/entities/artifact/types"
import { useExplorer } from "@/app/store"
import { IMPACT_SENTENCE, relationSentence } from "@/features/relations/wording"
import type { SEdge } from "./scene"

/**
 * The map as text (keyboard and screen-reader equivalent of 專案背景): domains and their capabilities, the PR's
 * impacted elements in the PR view, and the connections the map does not draw.
 */
export function ContextList() {
  const ex = useExplorer()
  const m = ex.model, ov = m.overlay, scene = ex.scene
  const open = ex.expanded.has("context-list")
  const cap = (c: Capability) => (
    <button key={c.id} className="chip-btn" onClick={(e) => { ex.setLevel({ capId: c.id }); ex.openDetail({ kind: "capability", id: c.id }, e.currentTarget) }}>{namesOf(c).primary}</button>
  )
  const label = (id: string) => scene.nodes.find((n) => n.id === id)?.label ?? id
  return (
    <div className="context-list">
      <button className="disclosure small" aria-expanded={open} onClick={() => ex.toggle("context-list")} data-testid="context-list-toggle">{open ? "▾ 收起文字清單" : "▸ 以文字清單瀏覽（領域、功能與連線）"}</button>
      {open && (
        <div data-testid="context-list">
          {ex.phase === "overlay" && ov ? (
            <>
              <h4>這個 PR 修改或影響的項目</h4>
              <ul className="plain small">{ov.impacts.filter((i) => i.target_kind === "capability" || i.target_kind === "flow").map((i) => (
                <li key={i.target_id}><button className="chip-btn" onClick={(e) => ex.openDetail({ kind: m.kindOf.get(i.target_id) ?? "capability", id: i.target_id }, e.currentTarget)}>{nameOfId(m, i.target_id).primary}</button>
                  {i.change_status !== "unchanged" ? `${i.change_status === "added" ? "新增" : i.change_status === "removed" ? "移除" : "修改"}；` : ""}{IMPACT_SENTENCE[i.impact_status]}</li>
              ))}</ul>
            </>
          ) : (
            <>
              <h4>業務領域與功能</h4>
              {m.domains.map((d) => {
                const caps = m.caps.filter((c) => c.category === "business" && !m.headOnly.has(c.id) && (c.domain_id ?? "") === d.id && !c.parent_capability_id)
                if (!caps.length) return null
                return (
                  <div key={d.id} className="dom-list">
                    <button className="link" onClick={() => { ex.setLevel({ domainId: d.id }); ex.select({ kind: "domain", id: d.id }) }}><b>{namesOf(d).primary}</b></button>{" "}
                    <span className="muted small">{caps.length} 個能力 · {d.source === "semantic" ? "由 AI 歸類" : "依目錄分組"}</span>
                    <div><Capped id={`ctx:${d.id}`} items={caps} limit={8} render={cap} /></div>
                  </div>
                )
              })}
            </>
          )}
          {scene.hidden.length > 0 && (
            <>
              <h4>地圖上沒有畫出的連線（{scene.hidden.length}）</h4>
              <ul className="plain small" data-testid="hidden-edges"><Capped id="ctx:hidden" items={[...scene.hidden].sort((a, b) => b.weight - a.weight)} limit={20} render={(e: SEdge) => (
                <li key={e.id}>{relationSentence(label(e.source), label(e.target), e.kinds)}<span className="muted">（{e.weight} 項依據{e.confidence === "unknown" ? "；未確認" : ""}{e.hiddenReason === "shared" ? "；連往共用基礎" : ""}）</span></li>
              )} /></ul>
            </>
          )}
        </div>
      )}
    </div>
  )
}
