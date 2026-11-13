import { useExplorer } from "@/app/store"
import type { AuthorNote } from "@/entities/artifact/types"

const GROUPS: { kind: AuthorNote["kind"]; label: string; hint: string }[] = [
  { kind: "settled_decision", label: "作者記為已定案的決策", hint: "不必再問是否核准；來源標示未驗證時，再點來源確認" },
  { kind: "verification", label: "驗證與 QA 狀態", hint: "作者說測過什麼、在哪個 commit、還有什麼沒驗證" },
  { kind: "open_issue", label: "作者列出的未解問題", hint: "決定能不能先合併" },
]

/**
 * 作者在 PR 說明裡的記錄 (overlay.author_record, 2.7.0): the author's own account — settled decisions, verification /
 * QA status, open issues — kept apart from the AI's questions. Each entry shows the author's words (quote, checked to
 * occur in the description); it is the author's claim, not something this tool verified.
 */
export function AuthorRecord() {
  const notes = useExplorer().model.overlay?.author_record ?? []
  if (notes.length === 0) return null
  return (
    <section id="review-author" tabIndex={-1} aria-labelledby="h-author" data-testid="review-author">
      <h3 id="h-author">作者在 PR 說明裡的記錄 <span className="count">{notes.length} 項</span></h3>
      <p className="muted small">AI 從 PR 說明整理，附作者原文；這是作者的說法，本工具沒有驗證。</p>
      {GROUPS.map((g) => {
        const items = notes.filter((n) => n.kind === g.kind)
        if (items.length === 0) return null
        return (
          <div key={g.kind} className="author-group" data-testid={`author-${g.kind}`}>
            <h4>{g.label} <span className="count">{items.length}</span> <span className="muted small">{g.hint}</span></h4>
            <ul className="plain">
              {items.map((n) => (
                <li key={n.id} id={n.id} className="author-note">
                  <p>{n.text.text}</p>
                  <details><summary className="small">PR 說明原文</summary><blockquote className="small">{n.quote}</blockquote></details>
                </li>
              ))}
            </ul>
          </div>
        )
      })}
    </section>
  )
}
