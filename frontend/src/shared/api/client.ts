import type { PrOverlay, SystemMap } from "@/entities/artifact/types"

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message) }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, { ...init, headers: { "Content-Type": "application/json", ...init?.headers } })
  } catch {
    throw new ApiError(0, "network", "無法連線到後端（是否已在 127.0.0.1:8766 啟動？）")
  }
  if (!res.ok) {
    let code = "internal", message = res.statusText
    try { const j = await res.json(); code = j.error?.code ?? code; message = j.error?.message ?? message } catch { /* non-JSON body */ }
    throw new ApiError(res.status, code, message)
  }
  return res.json() as Promise<T>
}

export type SemanticMode = "auto" | "off" | "light" | "standard" | "deep"
export interface AnalysisRequest {
  pr_url?: string
  local?: { repo_path: string; base: string; head: string; title?: string; body?: string }
  context_docs?: { name: string; content: string }[]
  semantic_mode: SemanticMode
}
export interface AnalysisCreated { analysis_id: string; status: string; cached: boolean }
export interface Stage { name: string; status: "pending" | "running" | "done" | "failed" | "skipped"; message: string }
export interface AnalysisStatus { analysis_id: string; status: "queued" | "running" | "done" | "failed"; stages: Stage[]; error: string | null; overlay_id: string | null; system_map_id: string | null }
export interface AnalysisSummary { analysis_id: string; status: string; title: string | null; repo: string | null; head_sha: string | null; created_at: string; overlay_id: string | null; system_map_id: string | null }
export interface Freshness { stale: boolean; analyzed_head_sha: string; current_head_sha: string }
/** PR_EXPLORER_ASK on the server, and whether this client may ask (docs/api.md「GET /api/config」). */
export interface ServerConfig { ask: { mode: "local" | "all" | "off"; allowed: boolean; reason: string | null } }
/** One cited source of an answer, resolved by the backend against the overlay (code refs may lack a line). */
export interface AnswerCite { id: string; kind: string; label: string; refs: { side: "base" | "head"; path: string; start_line?: number; end_line?: number }[] }
export interface Answer { text: string; cites: AnswerCite[]; unsupported: boolean; dropped: string[] }
export interface Question {
  question_id: string; overlay_id: string; created_at: string; question: string; selected_id: string | null
  status: "queued" | "running" | "done" | "failed"; answer: Answer | null; error: string | null
}

const enc = encodeURIComponent
export const api = {
  createAnalysis: (b: AnalysisRequest) => req<AnalysisCreated>("/api/analyses", { method: "POST", body: JSON.stringify(b) }),
  listAnalyses: () => req<AnalysisSummary[]>("/api/analyses"),
  getAnalysis: (id: string) => req<AnalysisStatus>(`/api/analyses/${enc(id)}`),
  getOverlay: (id: string) => req<PrOverlay>(`/api/overlays/${enc(id)}`),
  getSystemMap: (id: string) => req<SystemMap>(`/api/system-maps/${enc(id)}`),
  getFreshness: (overlayId: string) => req<Freshness>(`/api/overlays/${enc(overlayId)}/freshness`),
  getConfig: () => req<ServerConfig>("/api/config"),
  listQuestions: (overlayId: string) => req<Question[]>(`/api/overlays/${enc(overlayId)}/questions`),
  ask: (overlayId: string, question: string, selectedId: string | null) =>
    req<{ question_id: string; status: string }>(`/api/overlays/${enc(overlayId)}/questions`, { method: "POST", body: JSON.stringify({ question, selected_id: selectedId }) }),
}

/** An overlay is useless without its system map (spec v2: the PR is laid over the map). */
export async function loadOverlayWithMap(overlayId: string): Promise<{ overlay: PrOverlay; map: SystemMap }> {
  const overlay = await api.getOverlay(overlayId)
  const map = await api.getSystemMap(overlay.system_map_id)
  return { overlay, map }
}

/** Progressive results: documents stored before Claude finished carry semantic.status "pending". */
export const isPending = (d: { semantic: { status: string } } | null | undefined) => d?.semantic.status === "pending"

/**
 * Resolves once the analysis that produced a pending overlay has finished (done or failed), polling its status
 * (or, without an analysis id, the overlay itself). Returns a cancel function.
 */
export function whenSemanticSettles(overlayId: string, analysisId: string | null, onSettled: () => void, intervalMs = 2000): () => void {
  let live = true, timer: ReturnType<typeof setTimeout> | undefined
  const tick = async () => {
    try {
      const settled = analysisId
        ? ["done", "failed"].includes((await api.getAnalysis(analysisId)).status)
        : !isPending(await api.getOverlay(overlayId))
      if (!live) return
      if (settled) { onSettled(); return }
    } catch { /* transient: keep polling */ }
    if (live) timer = setTimeout(tick, intervalMs)
  }
  timer = setTimeout(tick, intervalMs)
  return () => { live = false; clearTimeout(timer) }
}
