import type { ReactNode } from "react"
import type { MapView as View } from "@/entities/artifact/types"
import { useExplorer, type Highlight } from "@/app/store"
import { Legend } from "./Legend"
import { MapView } from "./MapView"
import { crumbsFor, NODE_BUDGET, type Scene } from "./scene"

/** 系統 › 領域 › 能力 — jump back to any level. */
function Crumbs({ scene }: { scene: Scene }) {
  const ex = useExplorer()
  if (scene.mode === "flow") return null
  const items = crumbsFor(ex.model, scene.mode, scene.level)
  return (
    <nav className="crumbs" aria-label="Breadcrumb">
      {items.map((it, i) => (
        <span key={i}>
          {i > 0 && <span className="sep">›</span>}
          {i === items.length - 1 ? <b>{it.label}</b> : <button className="link" onClick={() => { ex.setLevel(it.level); ex.select(null); ex.setQuery("") }}>{it.label}</button>}
        </span>
      ))}
      <span className="muted tiny crumb-count" data-testid="node-count">　{scene.nodes.length} / {NODE_BUDGET} 個節點 · 畫出 {scene.edges.length} 條連線{scene.hidden.length ? `，${scene.hidden.length} 條列在文字清單` : ""}</span>
    </nav>
  )
}

export function MapToolbar({ children }: { children?: ReactNode }) {
  const ex = useExplorer()
  const hasOverlay = !!ex.model.overlay
  return (
    <div className="toolbar">
      <input className="search" type="search" placeholder="搜尋能力、流程、資料…" aria-label="搜尋" value={ex.query} onChange={(e) => ex.setQuery(e.target.value)} />
      <span className="group-label">顯示</span>
      <label title="被大多數能力依賴的共用模組，與連往它們的線（預設隱藏）"><input type="checkbox" checked={ex.showShared} onChange={(e) => ex.setShowShared(e.target.checked)} />共用基礎</label>
      <label title="測試、部署、建置設定、文件、腳本（預設隱藏）"><input type="checkbox" checked={ex.showEngineering} onChange={(e) => ex.setShowEngineering(e.target.checked)} />工程與工具</label>
      <span className="group-label">圖層</span>
      <label title="在能力上顯示 Findings 數量（預設關閉）"><input type="checkbox" checked={ex.layers.findings} disabled={!hasOverlay} onChange={(e) => ex.setLayers({ ...ex.layers, findings: e.target.checked })} />Findings</label>
      <label title="未受影響的項目淡化"><input type="checkbox" checked={ex.emphasis} disabled={!hasOverlay} onChange={(e) => ex.setEmphasis(e.target.checked)} />只突出變更</label>
      {children}
    </div>
  )
}

export function MapArea({ scene, views, toolbar }: { scene: Scene; views: { view: View; title?: string; highlight?: Highlight }[]; toolbar?: ReactNode }) {
  return (
    <div className="map-wrap">
      <MapToolbar>{toolbar}</MapToolbar>
      <Crumbs scene={scene} />
      <div className="map-area">
        <div className="side-by-side">
          {views.map((v) => <MapView key={v.view} scene={scene} view={v.view} title={v.title} highlight={v.highlight} />)}
        </div>
        <Legend />
      </div>
    </div>
  )
}
