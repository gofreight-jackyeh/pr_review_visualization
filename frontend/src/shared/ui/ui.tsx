import { useState, type ReactNode } from "react"
import { githubEvidenceUrl, prDiffUrl, refText } from "@/entities/artifact/model"
import { SOURCE_LABEL, SOURCE_TITLE, STATUS, type Source, type Status } from "@/entities/artifact/provenance"
import type { ChangedFile, CodeRef, PrOverlay, Text } from "@/entities/artifact/types"

type PrDiff = Pick<PrOverlay, "pr" | "changes">

export type Tone = "gray" | "green" | "amber" | "red" | "blue" | "purple" | "orange"
export const Chip = ({ children, tone = "gray", title }: { children: ReactNode; tone?: Tone; title?: string }) => (
  <span className={"chip-tag tone-" + tone} title={title}>{children}</span>
)

export function CopyButton({ text, label = "複製" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false)
  const copy = async () => {
    try { await navigator.clipboard.writeText(text) } catch {
      const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select()
      try { document.execCommand("copy") } catch { /* clipboard unavailable */ }
      ta.remove()
    }
    setDone(true); setTimeout(() => setDone(false), 1200)
  }
  return <button className="mini" onClick={copy} aria-label={`${label} ${text}`}>{done ? "已複製" : label}</button>
}

/** Link to this line in the PR's 「Files changed」 view (GitHub PRs, files this PR changes); nothing otherwise. */
export function DiffLink({ ov, r }: { ov?: PrDiff | null; r: { path: string; side?: CodeRef["side"]; start_line?: number } }) {
  const url = prDiffUrl(ov, r)
  return url ? <a className="mini diff-link" href={url} target="_blank" rel="noreferrer noopener" title="在 GitHub 的 Files changed 看這裡的改動" data-testid="diff-link">PR 改動 ↗</a> : null
}

/** One evidence location: `path:line` + copy button (+ PR diff and file links for GitHub PRs). No code is shown (spec v2 §4). */
export function RefRow({ r, ov }: { r: CodeRef; ov?: PrDiff | null }) {
  const url = githubEvidenceUrl(ov?.pr, r)
  return (
    <div className="refrow">
      <span className={"side side-" + r.side}>{r.side === "base" ? "修改前" : "修改後"}</span>
      <code className="tech" data-testid="ref-text">{refText(r)}</code>
      <CopyButton text={refText(r)} />
      <DiffLink ov={ov} r={r} />
      {url && <a className="mini" href={url} target="_blank" rel="noreferrer noopener" title="在 GitHub 看這個版本的完整檔案">GitHub ↗</a>}
    </div>
  )
}

const FILE_STATUS: Record<ChangedFile["status"], string> = { added: "新增", modified: "修改", removed: "移除", renamed: "改名" }
/** One changed file: status, path, line counts and the PR diff link. */
export function FileRow({ f, ov }: { f: ChangedFile; ov?: PrDiff | null }) {
  return (
    <li className="filerow"><span className={"fstat fs-" + f.status}>{FILE_STATUS[f.status]}</span> <code className="tech">{f.path}</code> <span className="muted small">+{f.additions} −{f.deletions}</span>
      {" "}<DiffLink ov={ov} r={{ path: f.path }} /></li>
  )
}

/** Where a statement comes from — low-key text, never a status (plan「來源與確認程度分開」). */
export function SourceNote({ source, prefix = "來源：" }: { source: Source; prefix?: string }) {
  return <span className="source-note" title={SOURCE_TITLE[source]} data-source={source}>{prefix}{SOURCE_LABEL[source]}</span>
}

/** One primary interpretation status: glyph + text, so it never relies on colour alone. */
export function StatusBadge({ s, title }: { s: Status; title?: string }) {
  const v = STATUS[s]
  return <span className={"status-badge sb-" + s} title={title ?? v.title} data-status={s}><span aria-hidden>{v.glyph}</span> {v.label}</span>
}

/** A text item with its source as a quiet suffix. */
export function TextBlock({ t, fallback }: { t?: Text; fallback?: string }) {
  if (!t) return fallback ? <p className="muted textblock">{fallback}</p> : null
  return <p className="textblock">{t.text} <SourceNote source={t.source} /></p>
}

/** The one question a view answers, at its top. */
export const FocusQuestion = ({ q, children }: { q: string; children?: ReactNode }) => (
  <div className="focus-q" data-testid="focus-question"><h2>{q}</h2>{children}</div>
)
