import { useState, type ReactNode } from "react"
import { githubEvidenceUrl, refText } from "@/entities/artifact/model"
import { SOURCE_LABEL, SOURCE_TITLE, STATUS, type Source, type Status } from "@/entities/artifact/provenance"
import type { CodeRef, PullRequest, Text } from "@/entities/artifact/types"

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

/** One evidence location: `path:line` + copy button (+ GitHub link for GitHub PRs). No code is shown (spec v2 §4). */
export function RefRow({ r, pr }: { r: CodeRef; pr?: PullRequest | null }) {
  const url = githubEvidenceUrl(pr, r)
  return (
    <div className="refrow">
      <span className={"side side-" + r.side}>{r.side === "base" ? "修改前" : "修改後"}</span>
      <code className="tech" data-testid="ref-text">{refText(r)}</code>
      <CopyButton text={refText(r)} />
      {url && <a className="mini" href={url} target="_blank" rel="noreferrer noopener">GitHub ↗</a>}
    </div>
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
