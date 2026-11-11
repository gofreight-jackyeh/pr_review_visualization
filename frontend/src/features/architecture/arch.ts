import type { ArchCheck } from "@/entities/artifact/types"

/** Spec §8 判定 → panel groups, most urgent first. Anything not statically verified is 需要確認, whatever its verdict. */
export type ArchGroup = "violates" | "new_debt" | "confirm" | "follows"
export const ARCH_GROUPS: { key: ArchGroup; label: string; tone: "red" | "amber" | "orange" | "green" }[] = [
  { key: "violates", label: "違反", tone: "red" },
  { key: "new_debt", label: "新增架構債", tone: "amber" },
  { key: "confirm", label: "需要確認", tone: "orange" },
  { key: "follows", label: "符合", tone: "green" },
]
export const ORIGIN_LABEL: Record<ArchCheck["origin"], string> = { documented: "文件規則", inferred: "從系統推斷", observed: "直接從 diff 看到", hypothesis: "Claude 假設" }

export function archGroupOf(c: ArchCheck): ArchGroup {
  if (c.status === "needs_confirmation" || c.verdict === "unclear") return "confirm"
  return c.verdict === "violates" ? "violates" : c.verdict === "new_debt" ? "new_debt" : "follows"
}
/** Dependency-rule checks (middle / inner ring) grouped by verdict; interface changes have their own section (見 interfaces.ts). */
export function groupChecks(checks: ArchCheck[]): Record<ArchGroup, ArchCheck[]> {
  const out: Record<ArchGroup, ArchCheck[]> = { violates: [], new_debt: [], confirm: [], follows: [] }
  for (const c of checks) if (c.check !== "interface_change") out[archGroupOf(c)].push(c)
  return out
}
