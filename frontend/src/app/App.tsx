import { useEffect, useMemo, useState } from "react"
import { buildModel, type Model } from "@/entities/artifact/model"
import type { PrOverlay, SystemMap } from "@/entities/artifact/types"
import { api, isPending, loadOverlayWithMap, whenSemanticSettles, type Freshness } from "@/shared/api/client"
import { PrInput, Progress } from "@/features/pr-input/PrInput"
import { Workspace } from "@/widgets/workspace/Workspace"
import { FAILED_ANALYSIS, FAILED_INPUT, FIXTURES } from "@/fixtures"
import { ExplorerProvider } from "./store"

type Loaded = { model: Model; fixture: string | null; analysisId?: string | null }

const overlayUrl = (overlayId: string, analysisId?: string | null) =>
  `?overlay=${encodeURIComponent(overlayId)}${analysisId ? `&analysis=${encodeURIComponent(analysisId)}` : ""}`

/** Re-run the analysis of the shown PR (PR 已更新 / 局部失敗); opens the new result as a new page so versions never mix. */
function useReanalysis(model: Model, fixture: string | null) {
  const ov = model.overlay
  const [running, setRunning] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const start = fixture || !ov?.pr.url ? undefined : async () => {
    setErr(null)
    try { setRunning((await api.createAnalysis({ pr_url: ov.pr.url!, semantic_mode: "auto" })).analysis_id) } catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
  }
  const ui = (
    <>
      {err && <div className="banner" role="alert">重新分析失敗：{err}</div>}
      {running && <div className="overlay"><Progress analysisId={running} onDone={(id, aid) => { window.location.search = overlayUrl(id, aid) }} onFail={() => setRunning(null)} /></div>}
    </>
  )
  return { start, ui }
}

function Header({ model, fixture, onHome, reanalyze }: { model: Model; fixture: string | null; onHome: () => void; reanalyze?: () => void }) {
  const ov = model.overlay
  const [fresh, setFresh] = useState<Freshness | null>(null)
  useEffect(() => {
    if (fixture || !ov) return
    api.getFreshness(ov.overlay_id).then(setFresh, () => setFresh(null))
  }, [ov, fixture])
  return (
    <>
      <header className="top">
        <button className="link" onClick={onHome}>← PR Review Explorer</button>
        <span className="title">{ov ? ov.pr.title : model.map.system.technical_name}</span>
        <span className="muted small">{model.map.repo.owner ? `${model.map.repo.owner}/` : ""}{model.map.repo.name}{ov ? ` #${ov.pr.number ?? "local"}` : ""}
          {ov && <> · 分析版本 <code className="tech" data-testid="analyzed-sha">{ov.pr.head_sha.slice(0, 7)}</code></>} {fixture && <b>· 範例資料（{fixture}）</b>}</span>
      </header>
      {fresh?.stale && (
        <div className="banner" role="alert" data-testid="state-stale">
          <b>PR 已更新。</b>目前顯示的是 head <code>{fresh.analyzed_head_sha.slice(0, 7)}</code> 的分析，GitHub 上已是 <code>{fresh.current_head_sha.slice(0, 7)}</code>；
          畫面內容不會混用兩個版本。{" "}
          {reanalyze ? <button className="primary" onClick={reanalyze}>分析新版</button> : <button onClick={onHome}>回首頁重新分析</button>}
        </div>
      )}
    </>
  )
}

function Explorer({ loaded, onHome }: { loaded: Loaded; onHome: () => void }) {
  const re = useReanalysis(loaded.model, loaded.fixture)
  const key = loaded.model.overlay?.overlay_id ?? loaded.model.map.system_map_id
  return (
    <>
      <Header model={loaded.model} fixture={loaded.fixture} onHome={onHome} reanalyze={re.start} />
      {re.ui}
      {/* keyed by overlay id (unchanged by backfill), so navigation, selection, expansion and scroll survive the reload */}
      <ExplorerProvider key={key} model={loaded.model} fixture={!!loaded.fixture}><Workspace onReanalyze={re.start} /></ExplorerProvider>
    </>
  )
}

export function App() {
  const params = useMemo(() => new URLSearchParams(window.location.search), [])
  const fixtureName = params.get("fixture")
  const fx = useMemo(() => (fixtureName && FIXTURES[fixtureName] ? FIXTURES[fixtureName]() : undefined), [fixtureName])
  const [loaded, setLoaded] = useState<Loaded | null>(() => (fx ? { model: buildModel(fx.map, fx.overlay), fixture: fixtureName } : null))
  const known = [...Object.keys(FIXTURES), "failed"]
  const [error, setError] = useState<string | null>(fixtureName && !fx && fixtureName !== "failed" ? `找不到範例資料「${fixtureName}」（可用：${known.join(", ")}）` : null)
  const overlayParam = params.get("overlay")
  const analysisParam = params.get("analysis")

  const [loading, setLoading] = useState(false)
  const load = async (overlayId: string, analysisId?: string | null) => {
    setError(null); setLoading(true) // show the loading state at once: the system map can take seconds to download
    try {
      const { overlay, map }: { overlay: PrOverlay; map: SystemMap } = await loadOverlayWithMap(overlayId)
      history.replaceState(null, "", overlayUrl(overlayId, analysisId) + window.location.hash) // keep #view: the provider reads it on mount
      setLoaded({ model: buildModel(map, overlay), fixture: null, analysisId })
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setLoading(false) }
  }
  useEffect(() => { if (overlayParam && !loaded) void load(overlayParam, analysisParam) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Progressive results: while Claude is still writing, poll; then swap in the final documents.
  const pendingOverlay = loaded && !loaded.fixture && loaded.model.overlay && (isPending(loaded.model.map) || isPending(loaded.model.overlay))
    ? loaded.model.overlay.overlay_id : null
  useEffect(() => {
    if (!pendingOverlay) return
    return whenSemanticSettles(pendingOverlay, loaded?.analysisId ?? null, () => {
      loadOverlayWithMap(pendingOverlay).then(({ overlay, map }) => setLoaded((prev) => (prev ? { ...prev, model: buildModel(map, overlay) } : prev)),
        (e: unknown) => setError(e instanceof Error ? e.message : String(e)))
    })
  }, [pendingOverlay]) // eslint-disable-line react-hooks/exhaustive-deps
  const home = () => { history.replaceState(null, "", window.location.pathname); window.location.reload() }

  if (loaded) {
    return (
      <div className="app">
        {error && <div className="banner" role="alert">{error}</div>}
        <Explorer loaded={loaded} onHome={home} />
      </div>
    )
  }
  return (
    <div className="app">
      {error && (
        <div className="banner" role="alert">{error}{overlayParam && <> <button onClick={() => void load(overlayParam, analysisParam)}>重試載入</button></>}</div>
      )}
      {(overlayParam || loading) && !error ? <p className="pad">載入分析結果…</p>
        : fixtureName === "failed" ? <PrInput initialAnalysis={FAILED_ANALYSIS.analysis_id} initialRequest={FAILED_INPUT} poll={async () => FAILED_ANALYSIS} onLoaded={load} />
        : <PrInput initialAnalysis={analysisParam ?? undefined} onLoaded={load} />}
    </div>
  )
}
