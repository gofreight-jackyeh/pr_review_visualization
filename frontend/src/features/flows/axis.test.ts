import { describe, expect, it } from "vitest"
import { large, sample, sampleNoSemantic } from "@/fixtures"
import { buildModel, isInfra } from "@/entities/artifact/model"
import { buildAxis, focusItems, MAX_SEGMENTS, STEPS_PER_PAGE, stepPage } from "./axis"

const S = buildModel(sample.map, sample.overlay)
const N = buildModel(sampleNoSemantic.map, sampleNoSemantic.overlay)
const L = buildModel(large().map, large().overlay)
const allModels = [S, N, L]

describe("flow axis", () => {
  it("every flow of every fixture has 3–7 segments when it has ≥ 3 visible steps", () => {
    for (const m of allModels) for (const f of m.flows) for (const v of ["overlay", "before", "after"] as const) {
      const a = buildAxis(m, f.id, v)
      const visible = a.segments.reduce((n, s) => n + s.steps.length, 0)
      expect(a.segments.length, `${f.id} ${v}`).toBeLessThanOrEqual(MAX_SEGMENTS)
      if (visible >= 3 && a.source === "phases") expect(a.segments.length, f.id).toBeGreaterThanOrEqual(3)
      expect(visible + a.hiddenSteps, f.id).toBe(a.totalSteps) // every step lands in exactly one segment (or is a hidden infra step)
    }
  })
  it("prefers Claude phases over capability runs", () => {
    const a = buildAxis(S, "flow:checkout", "overlay")
    expect(a.source).toBe("phases")
    expect(a.segments.map((s) => s.label)).toEqual(["接收結帳", "計算折扣", "保留庫存並扣款", "建立訂單與通知"])
    expect(buildAxis(L, "flow:cart_to_checkout", "overlay").source).toBe("phases")
    // the same flow without Claude falls back to one segment per capability run
    const n = buildAxis(N, "flow:checkout", "overlay")
    expect(n.source).toBe("capabilities")
    expect(n.segments.map((s) => s.label)).toEqual(["app.orders", "app.discounts", "app.inventory", "app.payments", "app.orders", "app.notifications"])
  })
  it("merges capability runs of a 40-step flow down to ≤ 7, keeping the changed run apart", () => {
    const a = buildAxis(L, "flow:ship_to_invoice", "overlay")
    expect(a.source).toBe("capabilities")
    expect(a.totalSteps).toBeGreaterThanOrEqual(40)
    expect(a.segments.length).toBeGreaterThanOrEqual(3)
    expect(a.segments.length).toBeLessThanOrEqual(7)
    expect(a.segments.some((s) => s.changed)).toBe(true)
  })
  it("hides steps in shared / engineering capabilities by default", () => {
    for (const m of allModels) for (const f of m.flows) {
      const a = buildAxis(m, f.id, "overlay")
      expect(a.segments.flatMap((s) => s.steps).some((s) => isInfra(m, s.capability_id)), f.id).toBe(false)
    }
    const hid = buildAxis(S, "flow:checkout", "overlay")
    expect(hid.hiddenSteps).toBe(1)
    expect(hid.segments[3].hiddenSteps).toBe(1)
    const shown = buildAxis(S, "flow:checkout", "overlay", { showInfra: true })
    expect(shown.hiddenSteps).toBe(0)
    expect(shown.segments[3].steps.map((s) => s.id)).toContain("step:checkout:8")
    expect(buildAxis(L, "flow:ship_to_invoice", "overlay").hiddenSteps).toBeGreaterThanOrEqual(4)
  })
  it("PR focus keeps the changed segment ±1 and collapses the rest", () => {
    const a = buildAxis(L, "flow:ship_to_invoice", "overlay")
    const items = focusItems(a, true)
    const changed = a.segments.findIndex((s) => s.changed)
    const kept = items.flatMap((i) => (i.kind === "segment" ? [i.index] : []))
    for (const i of kept) expect(a.segments.slice(Math.max(0, i - 1), i + 2).some((s) => s.changed), `segment ${i}`).toBe(true)
    expect(kept).toContain(changed)
    const collapsed = items.filter((i) => i.kind === "collapsed")
    expect(collapsed.length).toBeGreaterThan(0)
    for (const c of collapsed) if (c.kind === "collapsed") expect(c.label).toMatch(/^(前面|中間|後面) \d+ 段$/)
    expect(items.reduce((n, i) => n + (i.kind === "segment" ? 1 : i.segments.length), 0)).toBe(a.segments.length)
    // off → everything; a kept id (the expanded / playing segment) always stays open
    expect(focusItems(a, false).every((i) => i.kind === "segment")).toBe(true)
    const far = a.segments[0].id
    expect(focusItems(a, true, [far]).some((i) => i.kind === "segment" && i.seg.id === far)).toBe(true)
    // a flow the PR did not change is not collapsed
    expect(focusItems(buildAxis(S, "flow:refund", "overlay"), true).every((i) => i.kind === "segment")).toBe(true)
  })
  it("an expanded segment shows ≤ 7 steps, the rest behind 「再顯示 N 步」", () => {
    for (const m of allModels) for (const f of m.flows) for (const s of buildAxis(m, f.id, "overlay", { showInfra: true }).segments) {
      const p = stepPage(s, 0)
      expect(p.shown.length).toBeLessThanOrEqual(STEPS_PER_PAGE)
      expect(p.shown.length + p.remaining).toBe(s.steps.length)
    }
    const big = buildAxis(L, "flow:ship_to_invoice", "overlay").segments.reduce((a, b) => (b.steps.length > a.steps.length ? b : a))
    expect(big.steps.length).toBeGreaterThan(STEPS_PER_PAGE)
    const p1 = stepPage(big, 1)
    expect(p1.shown[0]).toBe(big.steps[STEPS_PER_PAGE])
  })
  it("above the axis: change counts and the findings / architecture checks touching a segment", () => {
    const a = buildAxis(S, "flow:checkout", "overlay")
    const disc = a.segments[1]
    expect(disc.counts).toMatchObject({ added: 1, removed: 1 })
    expect(disc.findings.map((f) => f.id)).toEqual(expect.arrayContaining(["fd:race"]))
    expect(disc.checks.map((c) => c.id)).toContain("ac:discounts-orders")
    expect(a.segments[0].changed).toBe(false)
    // before / after views drop added / removed steps
    expect(buildAxis(S, "flow:checkout", "before").segments[1].counts.added).toBe(0)
    expect(buildAxis(S, "flow:checkout", "after").segments[1].counts.removed).toBe(0)
  })
})
