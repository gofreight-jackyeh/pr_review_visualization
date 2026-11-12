import { describe, expect, it } from "vitest"
import { sample } from "@/fixtures"
import { buildModel } from "@/entities/artifact/model"
import type { Finding } from "@/entities/artifact/types"
import { focusOf, inFacts, isInstructionPath, reviewLayers, ruleRefs } from "./layers"

const finding = (id: string, severity: Finding["severity"], extra: Partial<Finding> = {}): Finding => ({
  id, source: "semantic", title: id, description: "", severity, status: "needs_confirmation", changed_file_ids: ["file:x"],
  capability_ids: [], refs: [{ side: "head", path: "x.py", start_line: 1 }], evidence_refs: ["ev:diff:discounts"], ...extra,
})

describe("審查重點 layers", () => {
  const m = buildModel(sample.map, sample.overlay)
  const l = reviewLayers(m)

  it("puts what needs a person on top and verified static facts in the second layer, with separate counts", () => {
    expect(l.top.map((x) => x.id)).toEqual(["ac:if-cancel", "ac:if-charge", "fd:stack", "ac:audit-signal", "ac:if-receipt",
      "fd:receipt", "ac:if-checkout", "ac:if-apply"])
    expect(l.facts.findings.map((f) => f.id)).toEqual(["fd:race", "fd:migration"])
    expect(l.facts.checks.map((c) => c.id)).toEqual(["ac:discounts-orders", "ac:il-ignore", "ac:payments-api", "ac:notify"])
    expect(l.counts).toEqual({ findings: 2, hypotheses: 1, ifaces: 5, checks: 0 })
    expect(inFacts(l, "fd:race") && !inFacts(l, "fd:stack") && !inFacts(l, null)).toBe(true)
  })

  it("orders by severity, then rule-backed, then human decisions / release context, then AI before static", () => {
    const ov = structuredClone(sample.overlay)
    ov.architecture_checks = []
    ov.findings = [
      finding("static-q", "attention", { source: "static", rule: "view_missing_sibling_decorator" }),
      finding("plain", "attention"),
      finding("pm", "attention", { rule: "semantic_review:human_decision" }),
      finding("ruled", "attention", { evidence_refs: ["ev:doc:rule-discounts"] }),
      finding("info", "info", { evidence_refs: ["ev:doc:rule-discounts"] }),
      finding("warn", "warning"),
      finding("fact", "warning", { source: "static", status: "verified" }),
    ]
    const got = reviewLayers(buildModel(sample.map, ov))
    expect(got.top.map((x) => x.id)).toEqual(["warn", "ruled", "pm", "plain", "static-q", "info"])
    expect(got.facts.findings.map((f) => f.id)).toEqual(["fact"])
  })

  it("reads focus and written-rule backing from the data", () => {
    expect(focusOf({ source: "semantic", rule: "semantic_review:release_dependency" })).toBe("release_dependency")
    expect(focusOf({ source: "semantic", rule: "semantic_review" })).toBeUndefined()
    expect(focusOf({ source: "static", rule: "semantic_review:human_decision" })).toBeUndefined()
    expect(ruleRefs(m, ["ev:doc:rule-discounts", "ev:diff:discounts"]).map((r) => `${r.ref.path}:${r.ref.start_line}`)).toEqual([".claude/rules/discounts.md:8"])
    for (const p of ["CLAUDE.md", "svc/AGENTS.md", ".claude/CLAUDE.md", ".claude/rules/analytics/analytics.md"]) expect(isInstructionPath(p), p).toBe(true)
    for (const p of ["README.md", "docs/CLAUDE.txt", ".github/AGENTS.md", "web/node_modules/x/CLAUDE.md", ".claude/settings.json"]) expect(isInstructionPath(p), p).toBe(false)
  })
})
