import type { ImpactStatus, Relation } from "@/entities/artifact/types"

/**
 * Impact wording (plan: Impact has no full causal chain). A static dependency is never phrased as a certain business
 * impact; the sentence says only what the data supports.
 */
export const IMPACT_SENTENCE: Record<Exclude<ImpactStatus, "none">, string> = {
  direct: "這次直接修改",
  confirmed_dependency: "與修改有已確認的程式依賴；是否影響業務行為需確認",
  evidence_supported: "有證據支持的依賴關係；是否影響業務行為需確認",
  potential: "可能相關（潛在影響，未證實）",
  unknown: "無法判定是否相關",
}

/** 「訂單呼叫付款」: one relation sentence (source, verb, target). */
export function relationSentence(source: string, target: string, kinds: Relation["kind"][]): string {
  const k = kinds.includes("calls") ? "calls" : kinds.includes("depends_on") ? "depends_on" : "uses_entity"
  return k === "calls" ? `${source}呼叫${target}` : k === "depends_on" ? `${source}依賴${target}` : `${source}使用${target}的資料`
}
