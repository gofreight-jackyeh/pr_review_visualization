import { describe, expect, it } from "vitest"
import { large, sample } from "@/fixtures"
import { buildModel, categoryOf, capabilityOf, type Model } from "@/entities/artifact/model"
import {
  buildScene, crumbsFor, EDGES_PER_NODE, ENGINEERING_BUCKET, NODE_BUDGET, PR_EDGES_PER_NODE, PR_NEIGHBOURS_PER_CAP, PR_TARGET, prCaps, SHARED_BUCKET,
  type Scene, type SceneOpts,
} from "./scene"

const L = buildModel(large().map, large().overlay)
const S = buildModel(sample.map, sample.overlay)

const capNodes = (s: Scene) => s.nodes.filter((n) => n.kind === "capability").map((n) => n.ref!)
const allViews = (m: Model, extra: Partial<SceneOpts> = {}): [string, Scene][] => {
  const out: [string, Scene][] = []
  for (const mode of ["system", "pr"] as const) {
    out.push([`${mode} root`, buildScene(m, { mode, ...extra })])
    for (const d of [...m.domains.map((x) => x.id), SHARED_BUCKET, ENGINEERING_BUCKET]) out.push([`${mode} ${d}`, buildScene(m, { mode, level: { domainId: d }, ...extra })])
    for (const c of m.caps) out.push([`${mode} ${c.id}`, buildScene(m, { mode, level: { capId: c.id }, ...extra })])
  }
  for (const f of m.flows) out.push([`flow ${f.id}`, buildScene(m, { mode: "flow", flowId: f.id, ...extra })])
  return out
}

describe("large fixture is shaped like a large monorepo", () => {
  it("has ~60 capabilities incl. shared / engineering, a split into 8, ~900 entry points, ~400 relations, a 40-step flow", () => {
    expect(L.caps.length).toBeGreaterThanOrEqual(55)
    expect(L.caps.some((c) => c.category === "shared") && L.caps.some((c) => c.category === "engineering")).toBe(true)
    expect(L.children.get("cap:finance")).toHaveLength(8)
    expect(L.entryPoints.length).toBeGreaterThan(800)
    expect(L.relations.length).toBeGreaterThan(350)
    expect(Math.max(...L.flows.map((f) => f.timeline.steps.length))).toBe(40)
  })
})

describe("System Overview", () => {
  it("large: ≤ 10 domain nodes and no engineering capability", () => {
    const s = buildScene(L, { mode: "system" })
    expect(s.nodes.filter((n) => n.kind === "domain").length).toBeLessThanOrEqual(10)
    expect(s.nodes.every((n) => n.kind === "domain")).toBe(true)
    expect(s.nodes.flatMap((n) => n.members).some((id) => categoryOf(L, id) === "engineering")).toBe(false)
    expect(s.nodes.flatMap((n) => n.members).some((id) => categoryOf(L, id) === "shared")).toBe(false)
  })
  it("labels Claude-assigned domains, and directory-based ones without Claude", () => {
    expect(buildScene(L, { mode: "system" }).nodes[0].tag).toBe("由 Claude 歸類")
    const noSem = buildModel({ ...sample.map, domains: [{ id: "dom:app", technical_name: "app", evidence_refs: ["ev:system"], source: "static", order: 0 }],
      capabilities: sample.map.capabilities.map((c) => ({ ...c, domain_id: c.category === "business" ? "dom:app" : undefined })) })
    expect(buildScene(noSem, { mode: "system" }).nodes[0].tag).toBe("依目錄分組")
  })
  it("engineering / shared appear only when switched on", () => {
    const s = buildScene(L, { mode: "system", showEngineering: true, showShared: true })
    expect(s.nodes.map((n) => n.id)).toEqual(expect.arrayContaining([SHARED_BUCKET, ENGINEERING_BUCKET]))
  })
})

describe("node budget", () => {
  it.each([["default", {}], ["all toggles on", { showShared: true, showEngineering: true }]] as const)("no view of large exceeds %s", (_n, extra) => {
    const views = allViews(L, extra)
    expect(views.length).toBeGreaterThan(130)
    for (const [name, s] of views) {
      expect(s.nodes.length, name).toBeLessThanOrEqual(NODE_BUDGET)
      expect(s.nodes.length, name).toBeGreaterThan(0)
      expect(new Set(s.nodes.map((n) => n.id)).size, name).toBe(s.nodes.length)
    }
  })
  it("overflow becomes a summary node that pages on click", () => {
    const many = buildModel({ ...large().map, domains: Array.from({ length: 30 }, (_, i) => ({ ...large().map.domains[0], id: `dom:x${i}`, order: i })),
      capabilities: large().map.capabilities.map((c, i) => (c.category === "business" ? { ...c, domain_id: `dom:x${i % 30}` } : c)) })
    const p0 = buildScene(many, { mode: "system" }), p1 = buildScene(many, { mode: "system", pages: { domains: 1 } })
    expect(p0.nodes.length).toBe(NODE_BUDGET)
    const summary = p0.nodes.find((n) => n.kind === "summary")!
    expect(summary.action).toEqual({ kind: "page", key: "domains" })
    expect(p1.nodes.map((n) => n.id)).not.toEqual(p0.nodes.map((n) => n.id))
  })
  it("split children appear under their parent", () => {
    const d = buildScene(L, { mode: "system", level: { domainId: "dom:acct" } })
    expect(capNodes(d)).toContain("cap:finance")
    expect(capNodes(d).some((id) => id.startsWith("cap:finance."))).toBe(false)
    const c = buildScene(L, { mode: "system", level: { capId: "cap:finance" } })
    expect(capNodes(c).filter((id) => id.startsWith("cap:finance.")).length).toBe(8)
    expect(crumbsFor(L, "system", { capId: "cap:finance.gl" }).map((x) => x.label)).toEqual(["系統", "會計財務", "會計", "總帳"])
  })
})

describe("edges", () => {
  it("aggregate to the visible level, keep the top few per node, and list the rest", () => {
    const s = buildScene(L, { mode: "system" })
    const merged = s.edges.find((e) => e.relationIds.length > 1)!
    expect(merged).toBeDefined()
    expect(merged.weight).toBe(merged.relationIds.reduce((w, id) => w + L.relations.find((r) => r.id === id)!.weight, 0))
    const deg = new Map<string, number>()
    for (const e of s.edges) for (const n of [e.source, e.target]) deg.set(n, (deg.get(n) ?? 0) + 1)
    expect(Math.max(...deg.values())).toBeLessThanOrEqual(EDGES_PER_NODE)
    expect(s.hidden.length).toBeGreaterThan(0)
    expect(s.hidden.every((e) => e.hiddenReason === "top_k" || e.hiddenReason === "shared")).toBe(true)
    const ids = new Set(s.nodes.map((n) => n.id))
    expect([...s.edges, ...s.hidden].every((e) => ids.has(e.source) && ids.has(e.target))).toBe(true) // only between visible nodes
  })
  it("edges to shared capabilities are hidden by default and shown when switched on", () => {
    const touchesShared = (s: Scene) => s.edges.some((e) => [e.source, e.target].some((id) => s.nodes.find((n) => n.id === id)!.members.some((m) => categoryOf(S, m) === "shared")))
    for (const [, s] of allViews(S)) if (!s.level.domainId?.startsWith("bucket:") && !(s.level.capId && categoryOf(S, s.level.capId) !== "business")) expect(touchesShared(s)).toBe(false)
    expect(touchesShared(buildScene(S, { mode: "system", showShared: true }))).toBe(true)
    expect(touchesShared(buildScene(S, { mode: "system", level: { capId: "cap:orders" }, showShared: true }))).toBe(true)
  })
})

describe("PR view", () => {
  it.each([["sample", S], ["large", L]] as const)("%s: impacted + ≤ 2 strongest neighbours each, everything else in per-domain summaries", (_n, m) => {
    const s = buildScene(m, { mode: "pr" })
    const impacted = new Set([...prCaps(m).keys()].filter((id) => categoryOf(m, id) !== "engineering"))
    const drawn = new Set(capNodes(s))
    for (const id of impacted) if (categoryOf(m, id) === "business") expect(drawn.has(id), id).toBe(true)
    const context = [...drawn].filter((id) => !impacted.has(id))
    for (const id of context) {
      const hop = m.relations.some((r) => { const a = capabilityOf(m, r.source)!, b = capabilityOf(m, r.target)!; return (a === id && impacted.has(b)) || (b === id && impacted.has(a)) })
      expect(hop, `${id} is a 1-hop neighbour`).toBe(true)
      expect(categoryOf(m, id), `${id}: shared capabilities are never context`).not.toBe("shared")
    }
    expect(context.length).toBeLessThanOrEqual(PR_NEIGHBOURS_PER_CAP * impacted.size)
    const summaries = s.nodes.filter((n) => n.kind === "summary")
    if (m === L) expect(summaries.length).toBeGreaterThan(0)
    for (const sm of summaries) expect(sm.label).toMatch(/：(其餘 )?\d+ 個能力，(未受影響|\d+ 個受影響)$/)
    expect(s.nodes.length).toBeLessThanOrEqual(NODE_BUDGET)
    // every business capability is drawn or folded into exactly one summary
    const covered = new Set([...drawn, ...summaries.flatMap((x) => x.members), ...s.nodes.filter((n) => n.kind === "capability").flatMap((n) => n.members)])
    for (const c of m.caps) if (c.category === "business") expect(covered.has(c.id), c.id).toBe(true)
  })
  it("hub regression: a few impacted capabilities next to hubs stay ≤ PR_TARGET nodes, ≤ 2 drawn edges per node", () => {
    // every capability relates to the impacted one: the old 1-hop rule drew 25 / 25 nodes
    const impactedId = "cap:cart"
    const others = large().map.capabilities.filter((c) => c.category === "business" && c.id !== impactedId && !c.parent_capability_id).slice(0, 30)
    const hubby = buildModel({ ...large().map, relations: [...large().map.relations, ...others.map((c, i) => ({
      ...large().map.relations[0], id: `rel:hub${i}`, source: impactedId, target: c.id, weight: 100 - i,
    }))] }, large().overlay)
    const s = buildScene(hubby, { mode: "pr" })
    const impacted = new Set([...prCaps(hubby).keys()].filter((id) => categoryOf(hubby, id) === "business"))
    const context = capNodes(s).filter((id) => !impacted.has(id))
    expect(s.nodes.length).toBeLessThanOrEqual(PR_TARGET)
    expect(buildScene(L, { mode: "pr" }).nodes.length).toBeLessThanOrEqual(PR_TARGET)
    expect(context.length).toBeLessThanOrEqual(PR_NEIGHBOURS_PER_CAP * impacted.size)
    const deg = new Map<string, number>()
    for (const e of s.edges) if (!e.violations.length) for (const n of [e.source, e.target]) deg.set(n, (deg.get(n) ?? 0) + 1)
    expect(Math.max(0, ...deg.values())).toBeLessThanOrEqual(PR_EDGES_PER_NODE)
  })

  it("large: an unaffected domain reads like 「會計財務：12 個能力，未受影響」", () => {
    const labels = buildScene(L, { mode: "pr" }).nodes.filter((n) => n.kind === "summary").map((n) => n.label)
    expect(labels).toContain("定價：4 個能力，未受影響")
    expect(labels.some((l) => /^會計財務：其餘 \d+ 個能力，未受影響$/.test(l))).toBe(true)
  })
  it("draws the architecture-check violating relation, highlighted, even if it is beyond the top few", () => {
    const s = buildScene(S, { mode: "pr" })
    const v = s.edges.find((e) => e.violations.includes("ac:discounts-orders"))!
    expect([v.source, v.target]).toEqual(["cap:discounts", "cap:orders"])
    expect(s.edges.find((e) => e.violations.includes("ac:payments-api"))).toMatchObject({ source: "cap:orders", target: "cap:payments" })
    expect(buildScene(L, { mode: "pr" }).edges.find((e) => e.violations.includes("ac:ops-gl"))).toMatchObject({ source: "cap:shipment", target: "cap:finance.gl" })
    expect(buildScene(S, { mode: "system" }).edges.some((e) => e.violations.length)).toBe(false) // no PR, no violation
  })
})
