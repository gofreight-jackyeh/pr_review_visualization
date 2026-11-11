import { RING, RING_ORDER } from "@/features/architecture/interfaces"
import { CHANGE, IMPACT, STEP, VIOLATION } from "./style"

const sw = (bg: string, border: string, dashed = false) => <span className="sw" style={{ background: bg, borderColor: border, borderStyle: dashed ? "dashed" : "solid" }} />

export function Legend() {
  return (
    <details className="legend">
      <summary>圖例</summary>
      <div className="legend-body">
        <div><b>變更狀態</b> {(Object.keys(CHANGE) as (keyof typeof CHANGE)[]).map((k) => <span key={k} className="lg">{sw(CHANGE[k].face, CHANGE[k].stroke, k === "removed")}{CHANGE[k].label}</span>)}</div>
        <div><b>影響範圍</b> {(Object.keys(IMPACT) as (keyof typeof IMPACT)[]).map((k) => <span key={k} className="lg">{sw("#fff", IMPACT[k].color, !!IMPACT[k].dash)}{IMPACT[k].label}</span>)}</div>
        <div><b>關係線</b>
          <span className="lg"><span className="ln" />已確認（越粗＝證據越多）</span>
          <span className="lg"><span className="ln dashed" />未確認 / 未知</span>
          <span className="lg"><span className="ln green" />PR 新增</span>
          <span className="lg"><span className="ln" style={{ borderTopColor: VIOLATION }} />違反架構規則</span>
          <span className="lg muted">每個節點只畫最重要的 3 條；其餘列在文字清單</span>
        </div>
        <div><b>節點</b>
          <span className="lg"><span className="sw" style={{ background: "#eef4fb", borderColor: "#6f8db5", width: 20 }} />業務領域（點擊進入）</span>
          <span className="lg"><span className="sw" style={{ background: "#fff", borderColor: "#8794a3", borderStyle: "dashed", width: 20 }} />摘要（收合的其餘節點，點擊展開）</span>
          <span className="lg"><span className="pill tone-purple chip-tag">由 Claude 歸類</span>／<span className="pill chip-tag tone-gray">依目錄分組</span></span>
        </div>
        <div><b>流程步驟</b> {(Object.keys(STEP) as (keyof typeof STEP)[]).map((k) => <span key={k} className="lg">{sw(STEP[k].bg, STEP[k].color)}{STEP[k].label}</span>)}
          <span className="lg">{sw("#fff", "#ea580c", true)}可能（未確認的分支）</span>
          <span className="lg">{sw("#fff", "#ea580c", true)}未知（動態呼叫）</span>
          <span className="lg">{sw("#e3edfd", "#1d4ed8")}目前步驟</span>
        </div>
        <div><b>介面圈別</b> {RING_ORDER.map((r) => <span key={r} className="lg"><span className="pill" style={{ background: RING[r].bg, color: RING[r].fg, border: `1px solid ${RING[r].color}` }}>{RING[r].label}</span>{r === "outer" ? "對外（客戶端直接使用，最不該改）" : r === "middle" ? "能力之間的介面" : "能力內部實作"}</span>)}
          <span className="lg muted">節點上標示這個 PR 動到的最外層圈別</span></div>
        <div><b>圖層</b> <span className="lg"><span className="pill" style={{ background: "#c0392b", color: "#fff" }}>F</span>Findings 數量</span></div>
      </div>
    </details>
  )
}
