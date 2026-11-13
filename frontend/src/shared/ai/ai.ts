/** Which AI wrote an overlay's texts (semantic.ai, schema 2.6.0) and the choices a request may make (GET /api/config). */
export type Engine = "claude" | "codex"
export type Effort = "low" | "medium" | "high" | "xhigh" | "max"
export interface AiChoice { engine: Engine; model?: string; effort?: Effort }

export const ENGINE_LABEL: Record<Engine, string> = { claude: "Claude", codex: "Codex" }
export const EFFORT_LABEL: Record<Effort, string> = { low: "low", medium: "medium", high: "high", xhigh: "xhigh", max: "max" }

/** 「Codex · gpt-6.1-sol · high」; missing parts were the engine's own defaults. */
export function aiLabel(ai: AiChoice | undefined | null): string | null {
  if (!ai) return null
  return [ENGINE_LABEL[ai.engine] ?? ai.engine, ai.model, ai.effort && `effort ${ai.effort}`].filter(Boolean).join(" · ")
}

/** The engine's name for sentences such as 「回答由 Claude 撰寫」 (Claude for documents from before 2.6.0). */
export const engineName = (ai: AiChoice | undefined | null) => ENGINE_LABEL[ai?.engine ?? "claude"] ?? "Claude"
