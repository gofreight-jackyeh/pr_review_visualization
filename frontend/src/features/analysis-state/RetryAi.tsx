import { useExplorer } from "@/app/store"
import { canRetryAi } from "./state"

/** 「重新產生 AI 說明」: only where the AI texts failed / are unavailable (not turned off, not already running). */
export function RetryAi({ primary }: { primary?: boolean }) {
  const ex = useExplorer()
  if (!ex.retryAi || !canRetryAi(ex.model)) return null
  return <button className={primary ? "primary" : undefined} data-testid="retry-ai" onClick={ex.retryAi}>重新產生 AI 說明</button>
}
