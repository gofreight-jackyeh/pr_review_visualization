import type { ReactNode } from "react"
import { useExplorer } from "@/app/store"
import { analysisStates, failureOf, unavailableReason } from "./state"

function Notice({ kind, children, action }: { kind: string; children: ReactNode; action?: ReactNode }) {
  return <div className={"state-notice st-" + kind} data-testid={`state-${kind}`}><p>{children}</p>{action}</div>
}

/**
 * One notice per analysis state that applies (plan「分析狀態與可用路徑」); every primary action leads to content that
 * exists in this result. Rendered inside one polite live region so backfill announces itself without moving focus.
 */
export function StateNotices({ onReanalyze }: { onReanalyze?: () => void }) {
  const ex = useExplorer()
  const m = ex.model
  const st = analysisStates(m)
  const toChanges = (label: string, anchor?: string) => <button onClick={() => ex.go({ nav: "changes" }, anchor)}>{label}</button>
  const fail = failureOf(m)
  return (
    <div className="state-notices" role="status" aria-live="polite">
      {st.includes("pending") && (
        <Notice kind="pending" action={ex.nav !== "changes" && m.overlay ? toChanges("查看已辨識的修改") : undefined}>
          <b>業務說明產生中。</b>目前先顯示 PR 描述與靜態分析已辨識的修改；AI 說明完成後會自動補上，不會改變你目前的位置與選取。
        </Notice>
      )}
      {st.includes("partial") && fail && (
        <Notice kind="partial" action={onReanalyze ? <button className="primary" onClick={onReanalyze}>重新分析</button> : undefined}>
          <b>部分內容未完成：</b>缺少{fail.what}{fail.error ? `（${fail.error}）` : ""}。靜態分析的結構、介面與檢查結果已完整保留。
          {onReanalyze ? "可重新分析以補上說明。" : "可回到首頁以相同 PR 重新分析。"}
        </Notice>
      )}
      {st.includes("unavailable") && (
        <Notice kind="unavailable" action={ex.nav !== "changes" && m.overlay ? toChanges("查看修改範圍") : undefined}>
          <b>業務說明不可用</b>{unavailableReason(m) ? `（${unavailableReason(m)}）` : ""}：顯示修改範圍、介面與靜態分析資訊，名稱以技術名稱呈現，不提供業務前後差異。
        </Notice>
      )}
      {st.includes("no_flows") && (
        <Notice kind="no-flows" action={ex.nav !== "changes" ? toChanges("查看相關檔案與功能") : undefined}>
          <b>沒有可辨識的業務流程。</b>這次修改仍可從相關檔案、功能與介面變動查看。
        </Notice>
      )}
    </div>
  )
}
