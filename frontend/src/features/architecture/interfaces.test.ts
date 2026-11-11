import { describe, expect, it } from "vitest"
import { large, sample, sampleNoSemantic } from "@/fixtures"
import { buildModel } from "@/entities/artifact/model"
import { statusOf } from "@/entities/artifact/provenance"
import { buildScene } from "@/features/system-map/scene"
import { capRings, COMPAT, groupByRing, interfaceChanges, needsChecklist, outermost, ringSummary } from "./interfaces"

const m = buildModel(sample.map, sample.overlay)
const byId = (id: string) => m.archChecks.find((c) => c.id === id)!

describe("interface changes (spec §8)", () => {
  it("are ordered outer → middle → inner", () => {
    const rings = interfaceChanges(m.archChecks).map((c) => c.ring)
    expect(rings).toEqual(["outer", "outer", "outer", "outer", "middle"])
    for (const fx of [sampleNoSemantic, large()]) {
      const r = interfaceChanges(buildModel(fx.map, fx.overlay).archChecks).map((c) => ["outer", "middle", "inner"].indexOf(c.ring))
      expect(r).toEqual([...r].sort())
    }
    // shuffled input still comes out ordered, breaking first inside a ring
    const shuffled = [...m.archChecks].reverse()
    expect(interfaceChanges(shuffled).map((c) => c.id)).toEqual(["ac:if-cancel", "ac:if-receipt", "ac:if-apply", "ac:if-checkout", "ac:if-charge"])
    expect(groupByRing(interfaceChanges(m.archChecks)).map(([r, l]) => [r, l.length])).toEqual([["outer", 4], ["middle", 1]])
  })
  it("maps compatibility to its badge", () => {
    expect(COMPAT.compatible).toMatchObject({ label: "相容", tone: "green" })
    expect(COMPAT.breaking).toMatchObject({ label: "可能破壞客戶端", tone: "red" })
    expect(COMPAT.unknown).toMatchObject({ label: "無法判定", tone: "gray" })
    expect(byId("ac:if-checkout").interface_change!.compatibility).toBe("compatible")
    expect(byId("ac:if-cancel").interface_change!.compatibility).toBe("breaking")
    expect(byId("ac:if-receipt").interface_change!.compatibility).toBe("unknown")
  })
  it("shows the 需要確認 checklist only for outer modified / removed", () => {
    expect(interfaceChanges(m.archChecks).filter(needsChecklist).map((c) => c.id).sort()).toEqual(["ac:if-cancel", "ac:if-checkout", "ac:if-receipt"])
    expect(needsChecklist(byId("ac:if-apply"))).toBe(false) // outer, added
    expect(needsChecklist(byId("ac:if-charge"))).toBe(false) // middle, modified
    const removed = { ...byId("ac:if-apply"), interface_change: { ...byId("ac:if-apply").interface_change!, change: "removed" as const } }
    expect(needsChecklist(removed)).toBe(true)
  })
  it("summarises counts per ring, with how many may break", () => {
    expect(ringSummary(m.archChecks)).toBe("外圈 4 項變動（1 項可能破壞）；中圈 1 項變動（1 項可能破壞）")
    expect(ringSummary([])).toBe("沒有對外介面變動")
  })
  it("badges each impacted capability with the outermost ring the PR touched", () => {
    const r = capRings(m)
    expect(r.get("cap:orders")).toBe("outer") // checkout / cancel / apply-code
    expect(r.get("cap:notifications")).toBe("outer") // receipt format (even though only potentially impacted)
    expect(r.get("cap:payments")).toBe("middle") // charge() only
    expect(r.get("cap:payments.refunds")).toBeUndefined() // untouched
    expect(r.get("cap:discounts")).toBe("inner") // changed, no interface change
    expect(outermost([undefined, "inner", "middle"])).toBe("middle")
    const lm = buildModel(large().map, large().overlay)
    expect(capRings(lm).get("cap:finance")).toBe("outer") // split parent rolls up its child (invoice: pdf format is outer)
    expect(capRings(lm).get("cap:shipment")).toBe("outer")
  })
  it("scene nodes carry the outermost ring of their members (what the map badge shows)", () => {
    const rings = capRings(m)
    const ringOfNode = (n: { members: string[] }) => outermost(n.members.map((id) => rings.get(id)))
    const nodes = buildScene(m, { mode: "pr" }).nodes
    expect(ringOfNode(nodes.find((n) => n.label === "訂單")!)).toBe("outer")
    expect(ringOfNode(nodes.find((n) => n.label === "付款")!)).toBe("middle")
    expect(ringOfNode(nodes.find((n) => n.label === "折扣")!)).toBe("inner")
    expect(nodes.filter((n) => n.kind === "capability" && !ringOfNode(n)).map((n) => n.label)).not.toContain("訂單")
    expect(buildScene(m, { mode: "system" }).nodes.length).toBeGreaterThan(0) // system overview shows no PR information
  })
  it("observed static changes read as 已確認的結構事實; an undecidable one as 無法判定 (both are static)", () => {
    expect(statusOf({ arch: byId("ac:if-cancel") })).toBe("confirmed")
    expect(byId("ac:if-receipt").source).toBe("static")
    expect(statusOf({ arch: byId("ac:if-receipt") })).toBe("undetermined")
  })
})
