import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import { sample, sampleNoSemantic } from "@/fixtures"
import { SOURCE_LABEL, STATUS, statusOf } from "@/entities/artifact/provenance"
import type { ArchCheck, Finding } from "@/entities/artifact/types"
import { SourceNote, StatusBadge, TextBlock } from "./ui"

const finding = (source: Finding["source"], status: Finding["status"]) => ({ ...sample.overlay.findings[0], source, status })

describe("source vs interpretation status", () => {
  it("names the three sources and the three statuses separately", () => {
    expect(Object.values(SOURCE_LABEL)).toEqual(["AI 說明", "PR 描述", "靜態分析"])
    expect(Object.values(STATUS).map((s) => s.label)).toEqual(["已確認的結構事實", "待確認", "無法判定"])
  })

  it("a static source with needs_confirmation is NOT 已確認", () => {
    expect(statusOf({ finding: finding("static", "needs_confirmation") })).toBe("needs_confirmation")
    expect(statusOf({ finding: finding("static", "verified") })).toBe("confirmed")
    expect(statusOf({ finding: finding("semantic", "needs_confirmation") })).toBe("needs_confirmation")
    const check = (p: Partial<ArchCheck>) => ({ ...sample.overlay.architecture_checks.find((a) => a.id === "ac:discounts-orders")!, ...p })
    expect(statusOf({ arch: check({ source: "static", status: "needs_confirmation" }) })).toBe("needs_confirmation")
    expect(statusOf({ arch: check({ source: "static", status: "verified", verdict: "unclear" }) })).toBe("undetermined")
    expect(statusOf({ arch: check({ source: "static", status: "verified" }) })).toBe("confirmed")
  })

  it("steps, relations and impacts map to a status, never to a source", () => {
    expect(statusOf({ step: { evidence_status: "confirmed" } })).toBe("confirmed")
    expect(statusOf({ step: { evidence_status: "possible" } })).toBe("needs_confirmation")
    expect(statusOf({ step: { evidence_status: "unknown" } })).toBe("undetermined")
    expect(statusOf({ relation: { confidence: "unknown" } })).toBe("undetermined")
    expect(statusOf({ impact: "potential" })).toBe("needs_confirmation")
    expect(statusOf({ impact: "confirmed_dependency" })).toBe("confirmed")
  })

  it("text carries its source as low-key text; status badges carry a glyph as well as a word", () => {
    expect(renderToStaticMarkup(<TextBlock t={sampleNoSemantic.overlay.purpose} />)).toContain("來源：PR 描述")
    expect(renderToStaticMarkup(<TextBlock t={sample.overlay.purpose} />)).toContain("來源：AI 說明")
    expect(renderToStaticMarkup(<SourceNote source="static" />)).toContain('class="source-note"')
    const badge = renderToStaticMarkup(<StatusBadge s="needs_confirmation" />)
    expect(badge).toContain("?")
    expect(badge).toContain("待確認")
  })
})
