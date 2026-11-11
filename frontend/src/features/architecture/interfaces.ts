import { descendants, type Model } from "@/entities/artifact/model"
import type { ArchCheck, Interface } from "@/entities/artifact/types"

/** Spec §8: interfaces are the starting point; the outer a ring (closer to clients), the less it should change. */
export type Ring = ArchCheck["ring"]
export const RING_ORDER: Ring[] = ["outer", "middle", "inner"]
export const RING: Record<Ring, { label: string; color: string; bg: string; fg: string }> = {
  outer: { label: "外圈", color: "#c2410c", bg: "#c2410c", fg: "#ffffff" },
  middle: { label: "中圈", color: "#0e7490", bg: "#cffafe", fg: "#155e75" },
  inner: { label: "內圈", color: "#64748b", bg: "#e2e8f0", fg: "#475569" },
}
export const KIND_LABEL: Record<Interface["kind"], string> = {
  http_api: "對外 API", web_route: "Web 頁面", sdk_export: "SDK", outbound_format: "對外格式", module_api: "模組介面", task: "背景任務", event: "事件",
}
export const CHANGE_LABEL: Record<NonNullable<ArchCheck["interface_change"]>["change"], string> = { added: "新增", modified: "修改", removed: "移除" }
export const COMPAT: Record<NonNullable<ArchCheck["interface_change"]>["compatibility"], { label: string; tone: "green" | "red" | "gray" }> = {
  compatible: { label: "相容", tone: "green" }, breaking: { label: "可能破壞客戶端", tone: "red" }, unknown: { label: "無法判定", tone: "gray" },
}
export const CHECKLIST = "需要確認：這個改動是否必要？能否改在更內層？是否需要版本控管或通知客戶端？"

export const isInterfaceChange = (c: ArchCheck) => c.check === "interface_change" && !!c.interface_change
const ringIdx = (r: Ring | undefined) => (r ? RING_ORDER.indexOf(r) : RING_ORDER.length)

/** Interface-change checks ordered outer → middle → inner; within a ring the ones that may break clients come first. */
export function interfaceChanges(checks: ArchCheck[]): ArchCheck[] {
  const sev = (c: ArchCheck) => ({ breaking: 0, unknown: 1, compatible: 2 })[c.interface_change!.compatibility]
  return checks.filter(isInterfaceChange).map((c, i) => ({ c, i })).sort((a, b) => ringIdx(a.c.ring) - ringIdx(b.c.ring) || sev(a.c) - sev(b.c) || a.i - b.i).map((x) => x.c)
}
export const groupByRing = (list: ArchCheck[]): [Ring, ArchCheck[]][] =>
  RING_ORDER.map((r) => [r, list.filter((c) => c.ring === r)] as [Ring, ArchCheck[]]).filter(([, l]) => l.length > 0)

/** 「需要確認」 checklist: only for outer interfaces that are modified or removed. */
export const needsChecklist = (c: ArchCheck) => !!c.interface_change && c.ring === "outer" && c.interface_change.change !== "added"

/** e.g. 外圈 3 項變動（1 項可能破壞）；中圈 1 項變動 */
export function ringSummary(checks: ArchCheck[]): string {
  const list = interfaceChanges(checks)
  if (!list.length) return "沒有對外介面變動"
  return groupByRing(list).map(([r, l]) => {
    const bad = l.filter((c) => c.interface_change!.compatibility === "breaking").length
    return `${RING[r].label} ${l.length} 項變動${bad ? `（${bad} 項可能破壞）` : ""}`
  }).join("；")
}

const capOfCheck = (m: Model, c: ArchCheck) => (c.interface_change && m.ifaceById.get(c.interface_change.interface_id)?.capability_id) || c.capability_ids[0]

/** Outermost ring the PR touched per capability: from interface changes; a capability changed without any interface change is 內圈. Split parents roll up their children. */
export function capRings(m: Model): Map<string, Ring> {
  const own = ownRings(m)
  const out = new Map<string, Ring>()
  for (const cap of m.caps) for (const d of descendants(m, cap.id)) { const r = own.get(d); if (r) { const cur = out.get(cap.id); if (!cur || ringIdx(r) < ringIdx(cur)) out.set(cap.id, r) } }
  return out
}
/** Rings per capability itself (no roll-up to split parents). */
export function ownRings(m: Model): Map<string, Ring> {
  const own = new Map<string, Ring>()
  const put = (id: string | undefined, r: Ring) => { if (id && m.capById.has(id) && ringIdx(r) < ringIdx(own.get(id))) own.set(id, r) }
  if (!m.overlay) return own
  for (const c of interfaceChanges(m.archChecks)) put(capOfCheck(m, c), c.ring)
  for (const cap of m.caps) if (!own.has(cap.id) && (m.impact.get(cap.id)?.change_status ?? (m.headOnly.has(cap.id) ? "added" : "unchanged")) !== "unchanged") put(cap.id, "inner")
  return own
}
export function outermost(rings: (Ring | undefined)[]): Ring | undefined {
  return rings.filter((r): r is Ring => !!r).sort((a, b) => ringIdx(a) - ringIdx(b))[0]
}

/** Interfaces of a capability (and its split children), outer ring first. */
export function interfacesOf(m: Model, capId: string): Interface[] {
  const ids = new Set(descendants(m, capId))
  return m.interfaces.filter((i) => ids.has(i.capability_id)).sort((a, b) => ringIdx(a.ring) - ringIdx(b.ring))
}

const UNCERTAIN = /無法|不確定|未能|cannot|not resolvable|unknown|undetermined/i

/**
 * Split an interface change's `detail` (clauses joined by ；) into what is established and what is not. A clause that
 * mixes both ("處理程式有改動，且請求 / 回應格式無法靜態判定") is cut at 「，且」 / 「，無法」: the part before it is the fact,
 * the part from the uncertainty phrase on goes to 不確定之處. Pure wording split; no new claims.
 */
export function splitDetail(detail: string): { facts: string[]; uncertain: string[] } {
  const facts: string[] = [], uncertain: string[] = []
  for (const clause of detail.split(/[；;](?![^（(]*[）)])\s*/).map((c) => c.trim()).filter(Boolean)) {
    if (!UNCERTAIN.test(clause)) { facts.push(clause); continue }
    const mixed = /^(.*?)(?:，且|[，,]\s*(?=無法)|,\s*and\s+)(.*)$/.exec(clause)
    if (mixed && mixed[1] && !UNCERTAIN.test(mixed[1]) && UNCERTAIN.test(mixed[2])) {
      const parts = /（[^（）]*）\s*$|\([^()]*\)\s*$/.exec(mixed[2]) // trailing detail list (changed symbols) is a fact
      facts.push(mixed[1] + (parts ? parts[0].trim() : ""))
      uncertain.push(parts ? mixed[2].slice(0, parts.index).trim() : mixed[2])
    } else uncertain.push(clause)
  }
  return { facts, uncertain }
}
