import { describe, expect, it } from "vitest"
import Ajv2020 from "ajv/dist/2020.js"
import addFormats from "ajv-formats"
import schema from "../../../../schemas/artifact/v2/artifact.schema.json"
import { empty, large, partial, pending, sample, sampleNoSemantic, type FixturePair } from "../../fixtures"
import { buildModel, deriveSteps, flowList, flowSteps, githubEvidenceUrl, namesOf, prDiffUrl, stepView } from "./model"
import { sha256Hex } from "@/shared/lib/sha256"
import { INSUFFICIENT, matchQuestion } from "@/features/guide/matcher"
import { buildScene } from "@/features/system-map/scene"
import { getLayout, placeNodes } from "@/features/system-map/graph"

const ajv = new Ajv2020({ allErrors: true, strict: false })
addFormats(ajv)
const validate = ajv.compile(schema)
const check = (doc: unknown) => { const ok = validate(doc); return { ok, errors: validate.errors ?? [] } }

const pairs: [string, FixturePair][] = [["sample", sample], ["sample-nosemantic", sampleNoSemantic], ["large", large()]]

describe.each(pairs)("fixture %s", (_name, fx) => {
  it("system map and overlay validate against the v2 schema (draft 2020-12)", () => {
    expect(check(fx.map).errors).toEqual([])
    expect(check(fx.overlay).errors).toEqual([])
  })
  it("has consistent ids and evidence references", () => {
    const m = buildModel(fx.map, fx.overlay)
    const ev = (ids: string[], where: string) => ids.forEach((id) => expect(m.evidence.has(id), `${where} -> ${id}`).toBe(true))
    for (const e of m.byId.values()) {
      ev(e.evidence_refs, e.id)
      if (e.business_name) ev(e.business_name.evidence_refs, e.id)
      if (e.description) ev(e.description.evidence_refs, e.id)
    }
    for (const i of fx.overlay.impacts) { expect(m.byId.has(i.target_id), i.target_id).toBe(true); ev(i.evidence_refs, i.target_id) }
    for (const fc of fx.overlay.flow_changes) {
      expect(m.flows.some((f) => f.id === fc.flow_id)).toBe(true)
      for (const s of fc.steps) { expect(m.byId.has(s.capability_id), s.id).toBe(true); ev(s.evidence_refs, s.id) }
    }
    for (const r of m.relations) { expect(m.byId.has(r.source) && m.byId.has(r.target), r.id).toBe(true); ev(r.evidence_refs, r.id) }
    // 2.1.0: domains, split parents, phases cover their steps exactly once, arch checks point at rules / capabilities
    const domIds = new Set(fx.map.domains.map((d) => d.id))
    for (const c of m.caps) {
      if (c.category === "business") expect(domIds.has(c.domain_id!), `${c.id} domain`).toBe(true)
      if (c.parent_capability_id) expect(m.capById.has(c.parent_capability_id), c.id).toBe(true)
    }
    for (const f of [...m.flows.map((x) => ({ id: x.id, phases: x.phases, steps: x.timeline.steps })), ...fx.overlay.flow_changes.map((x) => ({ id: x.flow_id, phases: x.phases, steps: x.steps }))]) {
      if (!f.phases) continue
      expect(f.phases.flatMap((p) => p.step_ids).sort(), f.id).toEqual(f.steps.map((s) => s.id).sort())
    }
    for (const a of fx.overlay.architecture_checks) {
      if (a.rule_id) expect(m.rules.has(a.rule_id), a.id).toBe(true)
      if (a.violating_relation) expect(m.capById.has(a.violating_relation.source) && m.capById.has(a.violating_relation.target), a.id).toBe(true)
      ev(a.evidence_refs, a.id)
      if (a.interface_change) expect(m.ifaceById.has(a.interface_change.interface_id), a.id).toBe(true)
    }
    // 2.2.0: interfaces point at capabilities / entry points, and every interface-change check says which ring it concerns
    expect(m.interfaces.length).toBeGreaterThan(0)
    for (const i of m.interfaces) {
      expect(m.capById.has(i.capability_id), i.id).toBe(true)
      if (i.entry_point_id) expect(m.byId.has(i.entry_point_id), i.id).toBe(true)
      ev(i.evidence_refs, i.id)
    }
    const kinds = new Set(m.interfaces.map((i) => i.kind))
    expect(kinds.has("http_api") && kinds.has("web_route") && kinds.has("outbound_format") && kinds.has("module_api")).toBe(true)
    const changes = fx.overlay.architecture_checks.filter((a) => a.check === "interface_change")
    expect(new Set(changes.map((a) => a.interface_change!.compatibility))).toEqual(new Set(["compatible", "breaking", "unknown"]))
    expect(changes.some((a) => a.interface_change!.change === "added")).toBe(true)
  })
})

describe.each([["pending", pending()], ["partial", partial()], ["empty", empty()]] as [string, FixturePair][])("state fixture %s", (_name, fx) => {
  it("validates against the v2 schema and references only existing ids", () => {
    expect(check(fx.map).errors).toEqual([])
    expect(check(fx.overlay).errors).toEqual([])
    expect(fx.overlay.system_map_id).toBe(fx.map.system_map_id)
    const m = buildModel(fx.map, fx.overlay)
    for (const i of fx.overlay.impacts) { expect(m.byId.has(i.target_id), i.target_id).toBe(true); i.evidence_refs.forEach((e) => expect(m.evidence.has(e), e).toBe(true)) }
    for (const f of fx.overlay.changes.files) if (f.capability_id) expect(m.capById.has(f.capability_id), f.path).toBe(true)
    expect(m.evidence.has(fx.overlay.purpose.evidence_refs[0])).toBe(true)
  })
})

describe("schema guard", () => {
  it("rejects a Text without evidence and an unknown extra field on a capability", () => {
    const bad = structuredClone(sample.map) as unknown as { system: { summary: { evidence_refs: string[] } }; capabilities: Record<string, unknown>[] }
    bad.system.summary.evidence_refs = []
    expect(check(bad).ok).toBe(false)
    const bad2 = structuredClone(sample.map) as unknown as { capabilities: Record<string, unknown>[] }
    bad2.capabilities[0].surprise = 1
    expect(check(bad2).ok).toBe(false)
  })
})

describe("no-semantic variant", () => {
  const m = buildModel(sampleNoSemantic.map, sampleNoSemantic.overlay)
  it("has no business names, purpose comes from the PR description, banner flag is off", () => {
    expect(m.semanticAvailable).toBe(false)
    expect([...m.byId.values()].some((e) => e.business_name)).toBe(false)
    expect(sampleNoSemantic.overlay.purpose.source).toBe("pr_description")
    expect(namesOf(m.caps[0])).toEqual({ primary: "app.orders", business: false })
  })
  it("steps fall back to the technical label", () => {
    const s = flowSteps(m, "flow:checkout").steps[0]
    expect(stepView(s)).toMatchObject({ label: s.technical_label, fromBusiness: false })
  })
})

describe("overlay step derivation", () => {
  const m = buildModel(sample.map, sample.overlay)
  const { steps, mode } = flowSteps(m, "flow:checkout")
  it("covers added, removed, changed and unchanged steps plus a possible branch", () => {
    expect(mode).toBe("animation")
    expect(new Set(steps.map((s) => s.step_change_status))).toEqual(new Set(["added", "removed", "changed", "unchanged"]))
    expect(steps.some((s) => s.evidence_status === "possible" && s.branch)).toBe(true)
  })
  it("before view excludes added steps, after view excludes removed steps, overlay keeps all", () => {
    const before = deriveSteps(steps, "before"), after = deriveSteps(steps, "after")
    expect(before.some((s) => s.step_change_status === "added")).toBe(false)
    expect(before.some((s) => s.step_change_status === "removed")).toBe(true)
    expect(after.some((s) => s.step_change_status === "removed")).toBe(false)
    expect(after.some((s) => s.step_change_status === "added")).toBe(true)
    expect(deriveSteps(steps, "overlay")).toHaveLength(steps.length)
    expect(deriveSteps(steps, "side")).toHaveLength(steps.length)
  })
  it("a flow without flow_changes uses its map timeline with every step unchanged", () => {
    expect(flowSteps(m, "flow:refund").steps.every((s) => s.step_change_status === "unchanged")).toBe(true)
  })
  it("static_flow flows report their mode", () => {
    expect(flowSteps(m, "flow:apply_code").mode).toBe("static_flow")
  })
  it("lists PR-related flows first", () => {
    const list = flowList(m)
    expect(list.slice(0, 2).map((i) => i.flow.id).sort()).toEqual(["flow:apply_code", "flow:checkout"])
    expect(list.every((i, k) => k === 0 || (i.change !== "unchanged" ? 0 : i.related ? 1 : 2) >= (list[k - 1].change !== "unchanged" ? 0 : list[k - 1].related ? 1 : 2))).toBe(true)
  })
})

describe("animation stepper", () => {
  const m = buildModel(sample.map, sample.overlay)
  const steps = flowSteps(m, "flow:checkout").steps
  it("maps a step to its capability / entity highlight and business label", () => {
    const added = steps.find((s) => s.step_change_status === "added")!
    expect(stepView(added)).toMatchObject({ capabilityId: "cap:discounts", entityId: "ent:discount_code", fromBusiness: true, status: "added", evidenceStatus: "possible", branch: "cart.discount_code is not None" })
    expect(stepView(added).label).toBe(added.business_label!.text)
  })
  it("falls back to the technical label when a step has no business label", () => {
    const s = { ...steps[0], business_label: undefined }
    expect(stepView(s)).toMatchObject({ label: s.technical_label, fromBusiness: false })
  })
})

describe("GitHub evidence links", () => {
  const pr = sample.overlay.pr
  it("uses merge_base_sha for base refs and head_sha for head refs", () => {
    expect(githubEvidenceUrl(pr, { side: "base", path: "app/a.py", start_line: 7 })).toBe(`https://github.com/acme/shop/blob/${pr.merge_base_sha}/app/a.py#L7`)
    expect(githubEvidenceUrl(pr, { side: "head", path: "app/a.py", start_line: 9 })).toBe(`https://github.com/acme/shop/blob/${pr.head_sha}/app/a.py#L9`)
    expect(pr.merge_base_sha).not.toBe(pr.head_sha)
  })
  it("returns null for local PRs", () => {
    expect(githubEvidenceUrl({ ...pr, source: "local" }, { side: "head", path: "x", start_line: 1 })).toBeNull()
    expect(githubEvidenceUrl(null, { side: "head", path: "x", start_line: 1 })).toBeNull()
  })
})

describe("question matcher", () => {
  const qs = sample.overlay.walkthrough.questions
  it("matches an existing question", () => {
    const hit = matchQuestion("付款會受到影響嗎", qs)
    expect(hit.kind === "match" && hit.question.id).toBe("q:payment")
  })
  it("answers unknown questions with the insufficient-data message", () => {
    expect(matchQuestion("資料庫用了幾個分片", qs).kind).toBe("insufficient")
    expect(matchQuestion("", qs).kind).toBe("insufficient")
    expect(INSUFFICIENT).toBe("這份分析的資料不足以回答這個問題")
  })
})

describe("layout", () => {
  const m = buildModel(sample.map, sample.overlay)
  it("gives identical positions for the same id across system / overlay / before / after", async () => {
    const scene = buildScene(m, { mode: "pr", level: { domainId: "dom:commerce" } })
    const layout = await getLayout(m, scene)
    const pos = (v: "system" | "overlay" | "before" | "after") => new Map(placeNodes(m, scene, layout, v).map((p) => [p.id, `${p.x},${p.y}`]))
    const [sys, ov, before, after] = [pos("system"), pos("overlay"), pos("before"), pos("after")]
    expect(ov.size).toBe(scene.nodes.length)
    for (const view of [sys, before, after]) for (const [id, p] of view) expect(ov.get(id), id).toBe(p)
    expect(sys.has("cap:audit")).toBe(false) // head-only: hidden in system / before, present in overlay / after
    expect(ov.has("cap:audit") && after.has("cap:audit") && !before.has("cap:audit")).toBe(true)
    expect(await getLayout(m, scene)).toBe(layout) // cached
  })
})

describe("PR diff links (GitHub Files changed anchors)", () => {
  const ov = sample.overlay
  // expected hashes from Python hashlib.sha256
  const ORDERS = "538ef80e5a25a1a6579efb8635941b79ebc2396e34e67ab038033cd3494e237b"
  it("sha256 matches known vectors (empty, multi-block, UTF-8)", () => {
    expect(sha256Hex("")).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    expect(sha256Hex("a".repeat(1000))).toBe("41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3")
    expect(sha256Hex("src/路徑/檔案.ts")).toBe("d72ad7b318c575e4097e84f2f2b5861723e50ce90e780def0059e9da62b8e819")
  })
  it("head side → R<line>, base side → L<line>, no line → the file", () => {
    expect(prDiffUrl(ov, { side: "head", path: "app/orders/views.py", start_line: 12 })).toBe(`https://github.com/acme/shop/pull/482/files#diff-${ORDERS}R12`)
    expect(prDiffUrl(ov, { side: "base", path: "app/orders/views.py", start_line: 3 })).toBe(`https://github.com/acme/shop/pull/482/files#diff-${ORDERS}L3`)
    expect(prDiffUrl(ov, { path: "app/orders/views.py" })).toBe(`https://github.com/acme/shop/pull/482/files#diff-${ORDERS}`)
  })
  it("a renamed file's base path anchors on its new path", () => {
    const o = structuredClone(ov)
    o.changes.files[0] = { ...o.changes.files[0], status: "renamed", old_path: "app/orders/old_views.py" }
    expect(prDiffUrl(o, { side: "base", path: "app/orders/old_views.py", start_line: 3 })).toBe(`https://github.com/acme/shop/pull/482/files#diff-${ORDERS}L3`)
  })
  it("no link for local PRs, a PR without URL, or a file this PR does not change", () => {
    expect(prDiffUrl({ ...ov, pr: { ...ov.pr, source: "local" } }, { side: "head", path: "app/orders/views.py", start_line: 12 })).toBeNull()
    expect(prDiffUrl({ ...ov, pr: { ...ov.pr, url: undefined } }, { path: "app/orders/views.py" })).toBeNull()
    expect(prDiffUrl(ov, { side: "base", path: "app/inventory/never_changed.py", start_line: 40 })).toBeNull()
    expect(prDiffUrl(null, { path: "app/orders/views.py" })).toBeNull()
  })
})
