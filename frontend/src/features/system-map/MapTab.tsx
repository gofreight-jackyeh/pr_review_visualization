import { useExplorer } from "@/app/store"
import { Chip, FocusQuestion, TextBlock } from "@/shared/ui/ui"
import { focusQuestion } from "@/features/focus/focus"
import { groupChecks } from "@/features/architecture/arch"
import { ringSummary } from "@/features/architecture/interfaces"
import { MapArea } from "./MapArea"
import { ContextList } from "./ContextList"

/** One-line architecture verdict for the PR view; the full cards live in 審查重點. */
function ArchStrip() {
  const ex = useExplorer()
  const g = groupChecks(ex.model.archChecks)
  if (!ex.model.archChecks.length) return null
  return (
    <p className="small arch-strip" data-testid="arch-strip">
      <b>架構一致性</b> <span data-testid="arch-strip-iface">{ringSummary(ex.model.archChecks)}</span>{" "}
      {g.violates.length > 0 && <Chip tone="red">違反 {g.violates.length}</Chip>} {g.new_debt.length > 0 && <Chip tone="amber">新增架構債 {g.new_debt.length}</Chip>}{" "}
      {g.confirm.length > 0 && <Chip tone="orange">需要確認 {g.confirm.length}</Chip>} {g.follows.length > 0 && <Chip tone="green">符合 {g.follows.length}</Chip>}{" "}
      <button className="link" onClick={() => ex.go({ nav: "review" }, "review-arch")}>在審查重點看介面變動與規則 →</button>
    </p>
  )
}

function Intro() {
  const ex = useExplorer()
  const m = ex.model
  const ov = m.overlay
  const atRoot = !ex.level.domainId && !ex.level.capId
  return (
    <div className="intro">
      <div className="row between">
        <div className="seg" role="group" aria-label="地圖範圍">
          <button className={ex.phase === "system" ? "on" : ""} aria-pressed={ex.phase === "system"} onClick={() => ex.setPhase("system")}>系統概覽</button>
          <button className={ex.phase === "overlay" ? "on" : ""} aria-pressed={ex.phase === "overlay"} disabled={!ov} onClick={() => ex.setPhase("overlay")}>PR 影響範圍</button>
        </div>
        {ex.phase === "system" && ov && <button className="primary" onClick={() => ex.setPhase("overlay")}>看這個 PR 動到哪裡 →</button>}
      </div>
      <FocusQuestion q={focusQuestion(m, { view: "map", mode: ex.scene.mode, level: ex.level })} />
      {ex.phase === "system" ? (
        atRoot && <TextBlock t={m.map.system.summary} fallback={`${m.map.system.technical_name}：${m.caps.filter((c) => !m.headOnly.has(c.id) && c.category === "business").length} 個業務模組、${m.map.entry_points.length} 個入口。${m.semanticAvailable ? "（系統說明這次沒有產生）" : "（業務說明不可用）"}`} />
      ) : ov && atRoot && (
        <div>
          <p className="small muted" style={{ margin: 0 }}>這個 PR 修改或影響的功能，以及各自關係最強的鄰居；修改目的與前後差異在「PR 摘要」與「變更與影響」。</p>
          <ArchStrip />
        </div>
      )}
      <ContextList />
    </div>
  )
}

export function MapTab() {
  const ex = useExplorer()
  return (
    <div className="map-stack">
      <Intro />
      <div className="map-top"><MapArea scene={ex.scene} views={[{ view: ex.phase === "overlay" && ex.model.overlay ? "overlay" : "system" }]} /></div>
    </div>
  )
}
