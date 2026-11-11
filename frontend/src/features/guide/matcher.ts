import type { Walkthrough } from "@/entities/artifact/types"

type Question = Walkthrough["questions"][number]
export type MatchResult = { kind: "match"; question: Question; score: number } | { kind: "insufficient" }

export const INSUFFICIENT = "這份分析的資料不足以回答這個問題"
const STOP = new Set(["the", "a", "an", "is", "are", "of", "to", "in", "on", "and", "or", "this", "that", "it", "do", "does", "did", "what", "why", "how", "where", "which", "pr", "for", "with"])
const CJK_STOP = new Set(["的", "了", "嗎", "呢", "是", "在", "有", "會", "什", "麼", "個", "這", "那", "？", "?"])

export function tokens(s: string): string[] {
  const out: string[] = []
  for (const w of s.toLowerCase().split(/[^\p{L}\p{N}_]+/u)) {
    if (!w) continue
    if (/[一-鿿]/.test(w)) out.push(...[...w].filter((c) => !CJK_STOP.has(c)))
    else if (!STOP.has(w)) out.push(w)
  }
  return out
}

/** Spec §9: no live AI. Only match the pre-generated questions; below the threshold say the data is insufficient. */
export function matchQuestion(text: string, questions: Question[], threshold = 0.5): MatchResult {
  const q = new Set(tokens(text))
  if (q.size === 0) return { kind: "insufficient" }
  let best: MatchResult = { kind: "insufficient" }, bestScore = 0
  for (const cand of questions) {
    const c = new Set(tokens(cand.question))
    if (c.size === 0) continue
    let hit = 0
    for (const t of q) if (c.has(t)) hit++
    const score = hit / Math.max(q.size, c.size)
    if (score >= threshold && score > bestScore) { best = { kind: "match", question: cand, score }; bestScore = score }
  }
  return best
}
