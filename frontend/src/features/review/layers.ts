/**
 * 審查重點 layers (pure). Readers are human reviewers who come after an AI review bot, so the page leads with what
 * needs a person:
 * - top 「需要你判斷或確認的事」: every finding still to confirm (AI findings, static questions), AI architecture
 *   hypotheses (incl. documented_rule), interface changes a person must judge (outer ring, or not statically
 *   compatible) and static checks that could not decide. Sorted by severity, then backed by a written rule first, then
 *   decisions / release context (focus human_decision, release_dependency) first, then AI before static.
 * - second 「結構事實」: what static analysis verified (findings, rule checks, compatible inner interface changes);
 *   collapsed by default with its count.
 * Counts stay per category; there is no combined risk score.
 */
import type { Model } from "@/entities/artifact/model"
import type { ArchCheck, CodeRef, Finding } from "@/entities/artifact/types"
import { archGroupOf } from "@/features/architecture/arch"

export type Focus = "human_decision" | "release_dependency" | "public_contract" | "responsibility_boundary" | "convention" | "claim_needs_proof"
export const FOCUS_LABEL: Record<Focus, string> = {
  human_decision: "需要人或 PM 決定",
  release_dependency: "跨 PR／上線順序",
  public_contract: "對外公開的內容",
  responsibility_boundary: "責任邊界",
  convention: "與既有做法是否一致",
  claim_needs_proof: "需要佐證的說法",
}
const FIRST_FOCUS: Focus[] = ["human_decision", "release_dependency"]

/** Semantic findings carry their focus as `rule: "semantic_review:<focus>"`. */
export function focusOf(f: Pick<Finding, "source" | "rule">): Focus | undefined {
  const m = f.source === "semantic" ? /^semantic_review:(\w+)$/.exec(f.rule ?? "") : null
  return m && m[1] in FOCUS_LABEL ? (m[1] as Focus) : undefined
}

/** Instruction files (CLAUDE.md / AGENTS.md at any depth, .claude/CLAUDE.md, .claude/rules/**.md); see backend instruction_scope. */
export function isInstructionPath(path: string): boolean {
  const parts = path.split("/")
  if (parts[0] === ".claude") return path === ".claude/CLAUDE.md" || (parts[1] === "rules" && parts.length > 2 && path.toLowerCase().endsWith(".md"))
  return (parts[parts.length - 1] === "CLAUDE.md" || parts[parts.length - 1] === "AGENTS.md") && !parts.some((p) => p.startsWith(".") || p === "node_modules")
}

/** Written rules (instruction files) an item cites: the doc evidence among its evidence_refs. */
export function ruleRefs(m: Model, evidenceRefs: string[]): { id: string; ref: CodeRef }[] {
  const out: { id: string; ref: CodeRef }[] = []
  for (const id of evidenceRefs) {
    const e = m.evidence.get(id)
    if (e?.kind !== "doc") continue
    for (const ref of e.refs) if (isInstructionPath(ref.path)) out.push({ id, ref })
  }
  return out
}

export type FocusItem = { kind: "finding"; id: string; f: Finding } | { kind: "arch"; id: string; c: ArchCheck }
export interface ReviewLayers {
  top: FocusItem[]
  facts: { findings: Finding[]; ifaces: ArchCheck[]; checks: ArchCheck[] }
  /** per category, never summed into a score */
  counts: { findings: number; hypotheses: number; ifaces: number; checks: number }
}

const SEV = { warning: 0, attention: 1, info: 2 } as const

const isIface = (c: ArchCheck) => c.check === "interface_change" && !!c.interface_change

/** An interface change a person must judge: anything on the outer ring (what clients see), or not statically compatible. */
const ifaceNeedsPerson = (c: ArchCheck) => c.origin === "hypothesis" || c.ring === "outer" || c.interface_change?.compatibility !== "compatible"

function severity(x: FocusItem): number {
  if (x.kind === "finding") return SEV[x.f.severity]
  const c = x.c
  if (isIface(c)) return c.interface_change!.compatibility === "breaking" ? 0 : c.interface_change!.compatibility === "unknown" ? 1 : 2
  return c.verdict === "violates" ? 0 : 1
}

export function reviewLayers(m: Model): ReviewLayers {
  const top: FocusItem[] = []
  const facts: ReviewLayers["facts"] = { findings: [], ifaces: [], checks: [] }
  const counts = { findings: 0, hypotheses: 0, ifaces: 0, checks: 0 }
  for (const f of m.overlay?.findings ?? []) {
    if (f.status === "verified") facts.findings.push(f)
    else { top.push({ kind: "finding", id: f.id, f }); counts.findings++ }
  }
  for (const c of m.archChecks) {
    if (c.origin === "hypothesis") { top.push({ kind: "arch", id: c.id, c }); counts.hypotheses++ }
    else if (isIface(c)) {
      if (ifaceNeedsPerson(c)) { top.push({ kind: "arch", id: c.id, c }); counts.ifaces++ } else facts.ifaces.push(c)
    } else if (archGroupOf(c) === "confirm") { top.push({ kind: "arch", id: c.id, c }); counts.checks++ }
    else facts.checks.push(c)
  }
  const ruled = (x: FocusItem) => (x.kind === "finding" ? ruleRefs(m, x.f.evidence_refs).length > 0
    : x.c.check === "documented_rule" || x.c.origin === "documented" || ruleRefs(m, x.c.evidence_refs).length > 0)
  const first = (x: FocusItem) => x.kind === "finding" && FIRST_FOCUS.includes(focusOf(x.f)!)
  const ai = (x: FocusItem) => (x.kind === "finding" ? x.f.source : x.c.source) === "semantic"
  const key = top.map((x, i) => ({ x, k: [severity(x), ruled(x) ? 0 : 1, first(x) ? 0 : 1, ai(x) ? 0 : 1, i] }))
  key.sort((a, b) => { for (let i = 0; i < a.k.length; i++) if (a.k[i] !== b.k[i]) return a.k[i] - b.k[i]; return 0 })
  return { top: key.map((x) => x.x), facts, counts }
}

/** Is this item (finding / check id) in the collapsed 結構事實 layer? A jump to it opens the layer. */
export function inFacts(l: ReviewLayers, id: string | null | undefined): boolean {
  if (!id) return false
  return l.facts.findings.some((f) => f.id === id) || l.facts.ifaces.some((c) => c.id === id) || l.facts.checks.some((c) => c.id === id)
}
