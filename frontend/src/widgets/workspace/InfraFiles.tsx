import { useExplorer } from "@/app/store"
import { reviewLayers } from "@/features/review/layers"
import { Chip, FileRow } from "@/shared/ui/ui"
import { groupChangedFiles, groupKindLabel } from "./infra"

export const INFRA_FILES_QUESTION = "這次改了哪些模組與資源？"

/** Infrastructure repositories: the summary's lead-in — there is no flow to read, the review focus is where to start. */
export function InfraNotice() {
  const ex = useExplorer()
  const n = reviewLayers(ex.model).top.length
  return (
    <div className="infra-notice" role="note" data-testid="infra-notice">
      <p>這是基礎設施程式碼（Terraform / Helm / Kubernetes）的 repository：沒有可執行的程式進入點，所以不畫業務流程與系統地圖。建議先看審查重點，再看變更檔案（依模組分組）。</p>
      <div className="infra-actions">
        <button className="big" onClick={() => ex.go({ nav: "review" }, "view-title")} data-testid="infra-go-review">先看審查重點（{n} 項）</button>
        <button className="link" onClick={() => ex.go({ nav: "changes" }, "view-title")}>變更檔案（依模組）</button>
      </div>
    </div>
  )
}

/** Changed files grouped by Terraform module / Helm chart / Kubernetes manifest directory, each with its PR diff link. */
export function InfraFiles() {
  const ov = useExplorer().model.overlay
  const groups = groupChangedFiles(ov?.changes.files ?? [])
  return (
    <article className="view infra-files" data-testid="infra-files">
      <h2 id="view-title" tabIndex={-1}>{INFRA_FILES_QUESTION}</h2>
      <p className="muted small">依檔案路徑分組（Terraform 以目錄為模組；Kubernetes 資源種類依檔名推測，未讀取檔案內容）。GitHub PR 的檔案可以直接開到改動處。</p>
      {groups.length === 0 && <p className="empty-result">這次沒有變更檔案。</p>}
      {groups.map((g) => (
        <section key={g.key} className="infra-group" aria-labelledby={`ig-${g.key}`} data-testid="infra-group">
          <h3 id={`ig-${g.key}`}><Chip tone={g.kind === "other" ? "gray" : "blue"}>{groupKindLabel(g.kind)}</Chip> <code className="tech">{g.label}</code> <span className="count">{g.files.length} 個檔案</span></h3>
          <ul className="plain files">{g.files.map((f) => <FileRow key={f.id} f={f} ov={ov} />)}</ul>
        </section>
      ))}
    </article>
  )
}
