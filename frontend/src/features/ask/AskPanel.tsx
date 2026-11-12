import { useCallback, useEffect, useRef, useState, type FormEvent } from "react"
import { useExplorer } from "@/app/store"
import { ApiError, api, type AnswerCite, type Question } from "@/shared/api/client"
import { Markdown } from "@/shared/ui/Markdown"
import { CopyButton, DiffLink, RefRow } from "@/shared/ui/ui"
import { askContext, CITE_KIND, citeSelection, contextTitle, type AskAccess } from "./ask"

export const POLL_MS = 2000
const MAX = 2000

/** The 「問 AI」 entry of the main navigation (every tab). */
export function AskButton({ access }: { access: AskAccess }) {
  const ex = useExplorer()
  if (access.state === "hidden" || access.state === "loading") return null
  return (
    <button className={"ask-open" + (ex.ask ? " on" : "")} aria-expanded={ex.ask} aria-controls="ask-panel" data-testid="ask-open"
      onClick={(e) => (ex.ask ? ex.closeAsk() : ex.openAsk(e.currentTarget))}>問 AI</button>
  )
}

function Cites({ cites }: { cites: AnswerCite[] }) {
  const ex = useExplorer()
  const m = ex.model
  return (
    <div className="ask-cites" data-testid="ask-cites">
      <h5>回答引用的來源</h5>
      <ul className="plain small">
        {cites.map((c) => {
          const sel = citeSelection(m, c.kind, c.id)
          const lined = c.refs.filter((r) => r.start_line)
          return (
            <li key={c.id} data-cite={c.id}>
              <span className="muted">{CITE_KIND[c.kind] ?? "來源"}</span>{" "}
              {sel ? <button className="chip-btn" onClick={(e) => ex.openDetail(sel, e.currentTarget)}>{c.label}</button>
                : c.kind === "file" && c.refs.length ? null : <span>{c.label}</span>}
              {lined.map((r) => <RefRow key={`${r.side}:${r.path}:${r.start_line}`} r={{ ...r, start_line: r.start_line! }} ov={m.overlay} />)}
              {!lined.length && c.refs.slice(0, 1).map((r) => (
                <span key={r.path}> <code className="tech">{r.path}</code><CopyButton text={r.path} /><DiffLink ov={m.overlay} r={r} /></span>
              ))}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function Turn({ q, onRetry, canAsk }: { q: Question; onRetry: (q: Question) => void; canAsk: boolean }) {
  const m = useExplorer().model
  const a = q.answer
  return (
    <li className="ask-turn" data-testid="ask-turn" data-status={q.status}>
      <p className="ask-q"><b>問：</b>{q.question}{q.selected_id && <> <span className="chip-tag tone-gray">關於：{contextTitle(m, q.selected_id)}</span></>}</p>
      {(q.status === "queued" || q.status === "running") && (
        <p className="ask-wait muted" role="status" data-testid="ask-waiting"><span className="ask-spinner" aria-hidden />
          {q.status === "queued" ? "排隊中，等其他分析或問題完成…" : "AI 正在閱讀這個 PR 並回答（通常要幾十秒）…"}</p>
      )}
      {q.status === "failed" && (
        <div className="ask-error" role="alert" data-testid="ask-failed">
          <p>沒有得到回答：<span className="small">{q.error || "未知錯誤"}</span></p>
          {canAsk && <button onClick={() => onRetry(q)}>重試</button>}
        </div>
      )}
      {q.status === "done" && a && (
        <div className="ask-a">
          <Markdown text={a.text} className="ask-answer" />
          {a.unsupported
            ? <p className="ask-unsupported" data-testid="ask-unsupported"><span aria-hidden>⚠</span> 未附來源，請自行確認</p>
            : <Cites cites={a.cites} />}
        </div>
      )}
    </li>
  )
}

function sendError(e: unknown): string {
  if (e instanceof ApiError && e.status === 409) return "上一個問題還在回答中，請等它完成再問。"
  if (e instanceof ApiError && e.status === 403) return `這台伺服器不允許從這裡提問：${e.message}`
  return `送出失敗：${e instanceof Error ? e.message : String(e)}`
}

/**
 * Follow-up questions to Claude about this PR. Same place and rules as the detail panel (one layer, Esc / 關閉 restores
 * focus and reading position). Answers are AI interpretation: always labelled, with their cited sources or a warning.
 */
export function AskPanel({ access }: { access: AskAccess }) {
  const ex = useExplorer()
  const m = ex.model
  const ovId = m.overlay?.overlay_id ?? ""
  const live = access.state === "ok" || access.state === "denied"
  const canAsk = access.state === "ok"
  const head = useRef<HTMLHeadingElement>(null)
  const end = useRef<HTMLDivElement>(null)
  const [items, setItems] = useState<Question[] | null>(null)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [draft, setDraft] = useState("")
  const [sending, setSending] = useState(false)
  const [sendErr, setSendErr] = useState<string | null>(null)
  const ctx = askContext(m, ex.selection, ex.card)
  const [useCtx, setUseCtx] = useState(true)
  useEffect(() => setUseCtx(true), [ctx?.id])
  useEffect(() => { head.current?.focus({ preventScroll: true }) }, [])

  const refresh = useCallback(async () => {
    try { setItems(await api.listQuestions(ovId)); setLoadErr(null) } catch (e) { setLoadErr(e instanceof Error ? e.message : String(e)) }
  }, [ovId])
  useEffect(() => { if (live) void refresh() }, [live, refresh])
  const busy = !!items?.some((q) => q.status === "queued" || q.status === "running")
  useEffect(() => {
    if (!busy) return
    const t = setTimeout(() => void refresh(), POLL_MS)
    return () => clearTimeout(t)
  }, [busy, items, refresh])
  useEffect(() => { end.current?.scrollIntoView?.({ block: "nearest" }) }, [items?.length])

  const send = async (text: string, selectedId: string | null) => {
    setSending(true); setSendErr(null)
    try { await api.ask(ovId, text, selectedId); setDraft(""); await refresh() } catch (e) { setSendErr(sendError(e)) } finally { setSending(false) }
  }
  const text = draft.trim()
  const submit = (e?: FormEvent) => { e?.preventDefault(); if (text && !sending && !busy) void send(text, useCtx && ctx ? ctx.id : null) }

  return (
    <aside className="detail-panel ask-panel" id="ask-panel" aria-labelledby="ask-title" data-testid="ask-panel"
      onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); ex.closeAsk() } }}>
      <header className="dp-head">
        <div><p className="muted small dp-kind">問 AI</p><h2 id="ask-title" ref={head} tabIndex={-1}>針對這個 PR 提問</h2></div>
        <button className="dp-close" onClick={ex.closeAsk} aria-label="關閉問 AI 面板">關閉 ✕</button>
      </header>
      <p className="ask-ai-label" role="note" data-testid="ask-ai-label">
        <b>AI 解讀，需要自行確認。</b>回答由 Claude 根據這份分析與 PR 原始碼撰寫（只閱讀、不執行程式），可能出錯。
      </p>
      <div className="dp-body ask-body">
        {access.state === "fixture" && <p className="muted">範例資料不能提問：請開啟實際分析的結果。</p>}
        {loadErr && <div className="ask-error" role="alert">讀取先前的問答失敗：{loadErr} <button onClick={() => void refresh()}>重試</button></div>}
        {live && items && items.length === 0 && <p className="muted small">還沒有人問過這個 PR。可以問「為什麼要這樣改」「這會影響哪些客戶」等問題。</p>}
        {items && items.length > 0 && <ol className="plain ask-history" aria-label="先前的問答">{items.map((q) => <Turn key={q.question_id} q={q} canAsk={canAsk && !busy} onRetry={(x) => void send(x.question, x.selected_id)} />)}</ol>}
        <div ref={end} />
      </div>
      {access.state === "denied" && (
        <p className="ask-denied" data-testid="ask-denied">
          {access.mode === "local" ? "這台伺服器只允許在執行它的電腦上提問（PR_EXPLORER_ASK=local）。可以看先前的問答；要從這裡提問，請管理者以 PR_EXPLORER_ASK=all 啟動。" : "這台伺服器不允許從這裡提問。"}
        </p>
      )}
      {canAsk && (
        <form className="ask-form" onSubmit={submit} data-testid="ask-form">
          {ctx && useCtx && (
            <p className="ask-ctx"><span className="chip-tag tone-blue" data-testid="ask-context">關於：{ctx.title}
              <button type="button" className="chip-x" aria-label={`不要附上「${ctx.title}」`} onClick={() => setUseCtx(false)}>✕</button></span></p>
          )}
          <label htmlFor="ask-input" className="sr-only">你的問題</label>
          <textarea id="ask-input" data-testid="ask-input" rows={3} maxLength={MAX} value={draft} placeholder="例如：為什麼結帳要重新計算總額？"
            onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit() }} />
          {sendErr && <p className="ask-error small" role="alert">{sendErr}</p>}
          <div className="row between">
            <span className="muted small">{busy ? "等上一個回答完成後才能再問" : `${draft.length}/${MAX} · Ctrl+Enter 送出`}</span>
            <button type="submit" className="primary" disabled={!text || sending || busy}>{sending ? "送出中…" : "送出"}</button>
          </div>
        </form>
      )}
    </aside>
  )
}
