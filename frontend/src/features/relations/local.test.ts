import { describe, expect, it } from "vitest"
import { large, sample } from "@/fixtures"
import { buildModel } from "@/entities/artifact/model"
import { localGraph, localScene, LOCAL_TARGET, reviewHints } from "./local"
import { relatedOf, type ReviewItem } from "@/features/review/related"
import { changeCards, missingBefore } from "@/features/changes/cards"

describe("local relation graph", () => {
  it("large: the change's capabilities + neighbours stay within 5–7 drawn nodes; the rest is grouped with its total", () => {
    const m = buildModel(large().map, large().overlay)
    const g = localGraph(m, large().overlay.behavior_changes[0].capability_ids)
    const drawn = g.nodes.length + (g.group ? 1 : 0)
    expect(drawn).toBeGreaterThanOrEqual(5)
    expect(drawn).toBeLessThanOrEqual(LOCAL_TARGET)
    expect(g.group).not.toBeNull()
    expect(g.nodes.length + g.group!.members.length).toBe(g.all.length) // nothing dropped
    // review hints are never hidden: drawn or counted on the group
    const hinted = g.all.filter((n) => n.hints > 0)
    expect(g.nodes.filter((n) => n.hints > 0).length + g.group!.members.filter((n) => n.hints > 0).length).toBe(hinted.length)
    expect(g.group!.hints).toBe(g.group!.members.reduce((s, n) => s + n.hints, 0))
    // the violating dependency is always drawn
    expect(g.edges.some((e) => e.violations.includes("ac:ops-gl"))).toBe(true)
    // paging walks through every neighbour
    const seen = new Set<string>()
    for (let p = 0; p < g.group!.pages; p++) localGraph(m, large().overlay.behavior_changes[0].capability_ids, p).nodes.forEach((n) => seen.add(n.id))
    expect(seen.size).toBe(g.all.length)
    const scene = localScene(m, g, "k")
    expect(scene.nodes.length).toBe(drawn)
    expect(scene.nodes.find((n) => n.kind === "summary")!.label).toContain(`其他 ${g.group!.members.length} 個相關功能`)
  })

  it("edges carry relation sentences; dependency is never worded as a certain business impact", () => {
    const m = buildModel(sample.map, sample.overlay)
    const g = localGraph(m, ["cap:payments"])
    expect(g.all.length).toBeLessThanOrEqual(LOCAL_TARGET)
    expect(g.group).toBeNull()
    const sentences = g.allEdges.map((e) => e.sentence)
    expect(sentences.some((t) => /^訂單呼叫付款（/.test(t))).toBe(true)
    for (const n of g.all.filter((x) => !x.focus)) expect(n.note).not.toMatch(/會影響|一定影響|確定影響/)
    expect(g.all.find((n) => n.id === "cap:payments")!.focus).toBe(true)
    expect(reviewHints(m, "cap:discounts").findings).toBe(3)
  })
})

describe("change cards", () => {
  it("behavior cards in artifact order, then uncovered flows and technical changes", () => {
    const m = buildModel(large().map, large().overlay)
    expect(changeCards(m).map((c) => `${c.kind}:${c.id}`)).toEqual(["behavior:bc:auto-invoice", "flow:fc:flow:cart_to_checkout"])
    const s = buildModel(sample.map, sample.overlay)
    expect(changeCards(s).map((c) => c.id)).toEqual(["bc:code", "bc:charge", "tech:none"])
  })
  it("a missing 修改前 is 不存在 only when every flow of the change is new, otherwise 無法判定", () => {
    const m = buildModel(sample.map, sample.overlay)
    const bc = sample.overlay.behavior_changes[0]
    expect(missingBefore(m, bc)).toBeNull()
    expect(missingBefore(m, { ...bc, before: undefined, flow_ids: ["flow:apply_code"] })).toBe("new_flow")
    expect(missingBefore(m, { ...bc, before: undefined })).toBe("undetermined")
  })
})

describe("related checks", () => {
  const ref = (path: string, line: number) => ({ side: "head" as const, path, start_line: line })
  const items: ReviewItem[] = [
    { id: "a", title: "A", kind: "check", ruleId: "r1", refs: [ref("x.py", 3)] },
    { id: "b", title: "B", kind: "check", ruleId: "r1", refs: [ref("y.py", 9)] },
    { id: "c", title: "C", kind: "finding", refs: [ref("x.py", 3)] },
    { id: "d", title: "A（同義但沒有連結）", kind: "finding", refs: [ref("x.py", 4)] },
  ]
  it("links only through the same rule or the same code location — no semantic de-duplication", () => {
    expect(relatedOf(items, "a")).toEqual([{ id: "b", title: "B", via: "same_rule" }, { id: "c", title: "C", via: "same_location" }])
    expect(relatedOf(items, "d")).toEqual([])
  })
})
