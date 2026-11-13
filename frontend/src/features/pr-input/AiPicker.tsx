import { useEffect, useState } from "react"
import { api, type AiOptions } from "@/shared/api/client"
import { ENGINE_LABEL, type AiChoice, type Effort, type Engine } from "@/shared/ai/ai"

/**
 * Engine / model / effort of this analysis' PR texts and its follow-up questions (the system map keeps the server's
 * default). Empty fields = the server default; the server lists the engines it enabled (GET /api/config).
 */
export function AiPicker({ value, onChange }: {
  value: AiChoice | null
  /** a React state setter: each change is applied to the latest value */
  onChange: (update: (prev: AiChoice | null) => AiChoice | null) => void
}) {
  const [opts, setOpts] = useState<AiOptions | null>(null)
  useEffect(() => { api.getConfig().then((c) => setOpts(c?.ai ?? null), () => setOpts(null)) }, [])
  if (!opts || opts.engines.length === 0) return null
  const engine: Engine = value?.engine ?? opts.default.engine
  const info = opts.engines.find((e) => e.engine === engine) ?? opts.engines[0]
  const placeholder = info.default?.model ? `預設：${info.default.model}` : "引擎預設"
  const set = (patch: Partial<AiChoice>) => onChange((prev) => {
    const was: Engine = prev?.engine ?? opts.default.engine
    const next: AiChoice = { engine: was, model: prev?.model, effort: prev?.effort, ...patch }
    if (patch.engine && patch.engine !== was) { next.model = undefined; next.effort = undefined } // another engine's names
    return next.engine === opts.default.engine && !next.model && !next.effort ? null : next
  })
  return (
    <fieldset className="field ai-picker" data-testid="ai-picker">
      <legend>AI 引擎（PR 說明與「問 AI」）</legend>
      <div className="row">
        {opts.engines.length > 1 && (
          <label className="field">引擎
            <select value={engine} onChange={(e) => set({ engine: e.target.value as Engine })} data-testid="ai-engine">
              {opts.engines.map((e) => <option key={e.engine} value={e.engine}>{ENGINE_LABEL[e.engine]}{e.engine === opts.default.engine ? "（預設）" : ""}</option>)}
            </select>
          </label>
        )}
        <label className="field grow">Model
          <input list="ai-models" value={value?.model ?? ""} placeholder={placeholder} maxLength={100} data-testid="ai-model"
            onChange={(e) => set({ model: e.target.value.trim() || undefined })} />
          <datalist id="ai-models">{info.models.map((m) => <option key={m} value={m} />)}</datalist>
        </label>
        <label className="field">Effort
          <select value={value?.effort ?? ""} onChange={(e) => set({ effort: (e.target.value || undefined) as Effort | undefined })} data-testid="ai-effort">
            <option value="">{info.default?.effort ? `預設（${info.default.effort}）` : "引擎預設"}</option>
            {info.efforts.map((x) => <option key={x} value={x}>{x}</option>)}
          </select>
        </label>
      </div>
      {engine === "codex" && <p className="muted small">Codex 會把 PR 的程式碼與分析資料送到 OpenAI。</p>}
      <p className="muted small">effort 較低會比較快，但可能漏掉審查重點；系統地圖一律用伺服器預設。</p>
    </fieldset>
  )
}
