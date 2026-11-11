import { useEffect, useRef, useState } from "react"
import { api, type AnalysisRequest, type AnalysisStatus, type AnalysisSummary, type SemanticMode } from "@/shared/api/client"

const STAGE_LABEL: Record<string, string> = {
  acquire: "取得 PR 與原始碼", system_map: "建立系統地圖", pr_static: "PR 靜態分析", flows: "業務流程",
  impact: "影響範圍", semantic: "Claude 業務說明", assemble: "組裝結果",
}
const ICON = { pending: "○", running: "◐", done: "●", failed: "✕", skipped: "–" } as const

/** Opens the explorer as soon as the static overlay is stored (Claude may still be writing business texts). */
export function Progress({ analysisId, onDone, onFail, onRetry, poll = api.getAnalysis }: {
  analysisId: string; onDone: (overlayId: string, analysisId: string) => void
  /** back to the (preserved) input */
  onFail?: () => void
  /** re-submit the same request */
  onRetry?: () => void
  poll?: (id: string) => Promise<AnalysisStatus>
}) {
  const [st, setSt] = useState<AnalysisStatus | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const done = useRef(onDone)
  done.current = onDone
  useEffect(() => {
    let live = true, timer: ReturnType<typeof setTimeout>
    const tick = async () => {
      try {
        const s = await poll(analysisId)
        if (!live) return
        setSt(s); setErr(null)
        if ((s.status === "done" || s.status === "running") && s.overlay_id) { done.current(s.overlay_id, analysisId); return }
        if (s.status === "failed") return
      } catch (e) { if (live) setErr(e instanceof Error ? e.message : String(e)) }
      timer = setTimeout(tick, 1000)
    }
    tick()
    return () => { live = false; clearTimeout(timer) }
  }, [analysisId]) // eslint-disable-line react-hooks/exhaustive-deps
  const failed = st?.status === "failed"
  const failedStage = st?.stages.find((s) => s.status === "failed")
  return (
    <div className="card-box" aria-live="polite" data-testid="progress">
      <h3>{failed ? "分析失敗" : "分析中…"} <span className="muted small">{analysisId}</span></h3>
      {failed && (
        <div className="notice warn" role="alert" data-testid="analysis-failed">
          <p><b>原因：</b>{st.error ?? "後端沒有提供失敗原因"}{failedStage ? `（失敗的步驟：${STAGE_LABEL[failedStage.name] ?? failedStage.name}）` : ""}</p>
          <div className="row">
            {onRetry && <button className="primary" onClick={onRetry}>重試</button>}
            {onFail && <button onClick={onFail}>修改輸入</button>}
          </div>
        </div>
      )}
      {err && <p className="notice warn">{err} （重試中）</p>}
      <ol className="stages">
        {(st?.stages ?? Object.keys(STAGE_LABEL).map((name) => ({ name, status: "pending" as const, message: "" }))).map((s) => (
          <li key={s.name} className={"stage " + s.status}><span className="ico">{ICON[s.status]}</span> {STAGE_LABEL[s.name] ?? s.name} <span className="muted small">{s.status}{s.message ? ` — ${s.message}` : ""}</span></li>
        ))}
      </ol>
    </div>
  )
}

export function PrInput({ initialAnalysis, initialRequest, poll, onLoaded }: {
  initialAnalysis?: string; initialRequest?: Partial<AnalysisRequest>; poll?: (id: string) => Promise<AnalysisStatus>
  onLoaded: (overlayId: string, analysisId?: string) => void
}) {
  const [prUrl, setPrUrl] = useState(initialRequest?.pr_url ?? "")
  const [local, setLocal] = useState(false)
  const [repo, setRepo] = useState(""); const [base, setBase] = useState("main"); const [head, setHead] = useState("")
  const [docName, setDocName] = useState(""); const [docText, setDocText] = useState("")
  const [mode, setMode] = useState<SemanticMode>("auto")
  const [analysisId, setAnalysisId] = useState<string | undefined>(initialAnalysis)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [recent, setRecent] = useState<AnalysisSummary[] | null>(null)
  const [recentErr, setRecentErr] = useState<string | null>(null)

  useEffect(() => { api.listAnalyses().then(setRecent, (e: unknown) => setRecentErr(e instanceof Error ? e.message : String(e))) }, [analysisId])

  const request = (): AnalysisRequest => {
    const body: AnalysisRequest = { semantic_mode: mode }
    if (local) body.local = { repo_path: repo.trim(), base: base.trim(), head: head.trim() }
    else body.pr_url = prUrl.trim()
    if (docText.trim()) body.context_docs = [{ name: docName.trim() || "context.md", content: docText }]
    return body
  }
  // the form keeps every field while the analysis runs or fails: 修改輸入 returns to it unchanged, 重試 re-sends it
  const run = async (body: AnalysisRequest) => {
    setError(null); setAnalysisId(undefined)
    setBusy(true)
    try {
      const r = await api.createAnalysis(body)
      const s = await api.getAnalysis(r.analysis_id)
      if (r.cached && s.overlay_id) onLoaded(s.overlay_id, r.analysis_id)
      else setAnalysisId(r.analysis_id)
    } catch (err) { setAnalysisId(undefined); setError(err instanceof Error ? err.message : String(err)) } finally { setBusy(false) }
  }
  const submit = (e: React.FormEvent) => { e.preventDefault(); void run(request()) }
  const canRetry = local ? !!(repo.trim() && head.trim()) : !!prUrl.trim()

  return (
    <main className="landing">
      <h1>PR Review Explorer</h1>
      <p className="muted">先看這個 PR 改了什麼、為什麼，再按需要深入流程、審查重點與專案背景。在本機執行，不需要閱讀程式碼。</p>
      {analysisId ? <Progress analysisId={analysisId} onDone={onLoaded} onFail={() => setAnalysisId(undefined)} onRetry={canRetry ? () => void run(request()) : undefined} poll={poll} /> : (
        <form className="card-box" onSubmit={submit}>
          {!local && (
            <label className="field">GitHub PR 網址
              <input value={prUrl} onChange={(e) => setPrUrl(e.target.value)} placeholder="https://github.com/owner/repo/pull/123" required />
            </label>
          )}
          <details open={local} onToggle={(e) => setLocal((e.target as HTMLDetailsElement).open)}>
            <summary>本機 repo 模式</summary>
            <label className="field">Repo 路徑<input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="/abs/path/to/repo" required={local} /></label>
            <div className="row">
              <label className="field grow">Base<input value={base} onChange={(e) => setBase(e.target.value)} required={local} /></label>
              <label className="field grow">Head<input value={head} onChange={(e) => setHead(e.target.value)} placeholder="feature-branch" required={local} /></label>
            </div>
          </details>
          <details>
            <summary>補充文件（選填）</summary>
            <label className="field">檔名<input value={docName} onChange={(e) => setDocName(e.target.value)} placeholder="jira-ABC-1.md" /></label>
            <label className="field">貼上文字<textarea rows={4} value={docText} onChange={(e) => setDocText(e.target.value)} placeholder="貼上 Jira、Slack 或設計文件" /></label>
          </details>
          <label className="field">Claude 業務說明
            <select value={mode} onChange={(e) => setMode(e.target.value as SemanticMode)}>
              <option value="auto">自動（依複雜度）</option><option value="off">關閉（只做靜態分析）</option>
              <option value="light">Light</option><option value="standard">Standard</option><option value="deep">Deep</option>
            </select>
          </label>
          {error && <p className="notice warn" role="alert">{error}</p>}
          <div className="row">
            <button className="primary" type="submit" disabled={busy}>{busy ? "啟動中…" : "開始分析"}</button>
            <a href="?fixture=sample">開啟範例（不需後端）</a> <a href="?fixture=sample-nosemantic">範例：沒有 AI 說明</a>
          </div>
        </form>
      )}
      <section className="card-box">
        <h3>最近的分析</h3>
        {recentErr && <p className="muted small">後端無法連線： {recentErr}</p>}
        {recent && recent.length === 0 && <p className="muted small">尚無紀錄。</p>}
        <ul className="plain">
          {(recent ?? []).map((r) => (
            <li key={r.analysis_id} className="row between">
              <span>{r.title ?? r.analysis_id} <span className="muted small">{r.repo} {r.head_sha?.slice(0, 7)} · {r.status} · {new Date(r.created_at).toLocaleString()}</span></span>
              {r.overlay_id ? <button onClick={() => onLoaded(r.overlay_id!, r.analysis_id)}>開啟</button> : r.status !== "failed" ? <button onClick={() => setAnalysisId(r.analysis_id)}>進度</button> : null}
            </li>
          ))}
        </ul>
      </section>
    </main>
  )
}
