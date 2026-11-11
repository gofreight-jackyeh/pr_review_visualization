// @vitest-environment happy-dom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { renderToStaticMarkup } from "react-dom/server"
import { afterEach, describe, expect, it, vi } from "vitest"
import { FAILED_ANALYSIS, sample, sampleNoSemantic } from "@/fixtures"
import { buildModel, flowSteps, stepView } from "@/entities/artifact/model"
import type { PrOverlay, SystemMap } from "@/entities/artifact/types"
import { StepCard } from "@/features/detail/DetailPanel"
import { fitTargets, placeNodes, getLayout } from "@/features/system-map/graph"
import { buildScene } from "@/features/system-map/scene"
import { App } from "./App"
import { ExplorerProvider } from "./store"

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/** The no-Claude fixture as a pending document of the same analysis as `sample`. */
function pendingPair(): { map: SystemMap; overlay: PrOverlay } {
  const map = structuredClone(sampleNoSemantic.map), overlay = structuredClone(sampleNoSemantic.overlay)
  map.system_map_id = sample.map.system_map_id
  overlay.overlay_id = sample.overlay.overlay_id
  overlay.system_map_id = sample.map.system_map_id
  map.semantic = { status: "pending", tier: "standard" }
  overlay.semantic = { status: "pending", tier: "standard" }
  return { map, overlay }
}

const flush = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }

describe("progressive results", () => {
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); document.body.innerHTML = ""; history.replaceState(null, "", "/") })

  it("backfill swaps in the final documents without resetting the view, selection, expansion or scroll", async () => {
    vi.useFakeTimers()
    const pend = pendingPair()
    let status = "running"
    const fetchMock = vi.fn(async (url: string) => {
      const final = status === "done"
      const body = url.startsWith("/api/analyses/") ? { analysis_id: "a_1", status, stages: [], error: null, overlay_id: sample.overlay.overlay_id, system_map_id: sample.map.system_map_id }
        : url.startsWith("/api/overlays/") && url.endsWith("/freshness") ? { stale: false, analyzed_head_sha: "x", current_head_sha: "x" }
        : url.startsWith("/api/overlays/") ? (final ? sample.overlay : pend.overlay)
        : url.startsWith("/api/system-maps/") ? (final ? sample.map : pend.map) : {}
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })
    })
    vi.stubGlobal("fetch", fetchMock)
    history.replaceState(null, "", `/?overlay=${sample.overlay.overlay_id}&analysis=a_1`)
    const root = document.createElement("div")
    document.body.appendChild(root)
    await act(async () => { createRoot(root).render(<App />) })
    await flush(10)
    expect(root.querySelector("[data-testid=state-pending]")?.textContent).toContain("業務說明產生中")

    // the reader moves to 專案背景, picks a capability (technical name only so far) and scrolls
    await act(async () => { ([...root.querySelectorAll(".main-nav button")].find((b) => b.textContent === "專案背景") as HTMLElement).click() })
    await act(async () => { (root.querySelector("[data-testid=context-list-toggle]") as HTMLElement).click() })
    const chip = [...root.querySelectorAll("button.chip-btn")].find((b) => b.textContent === "app.orders") as HTMLButtonElement
    await act(async () => { chip.click() })
    expect(root.querySelector("#detail-title")?.textContent).toBe("app.orders")
    const content = root.querySelector("[data-testid=content]") as HTMLElement
    content.scrollTop = 60

    await flush(2100)  // still running: no reload
    expect(root.querySelector("[data-testid=state-pending]")).not.toBeNull()
    status = "done"
    await flush(2100)
    await flush(10)
    expect(root.querySelector("[data-testid=state-pending]")).toBeNull()
    const orders = sample.map.capabilities.find((c) => c.id === "cap:orders")!
    expect(root.querySelector(".main-nav [aria-current=page]")?.textContent).toBe("專案背景") // same view
    expect(root.querySelector("#detail-title")?.textContent).toBe(orders.business_name!.text)  // same selection, panel still open, new names
    expect(root.querySelector("[data-testid=context-list]")).not.toBeNull() // expanded block kept
    expect(root.querySelector("[data-testid=content]")).toBe(content) // not remounted
    expect(content.scrollTop).toBe(60)
    expect(fetchMock.mock.calls.some(([u]) => String(u) === "/api/analyses/a_1")).toBe(true)
  })
})

describe("complete failure", () => {
  afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ""; history.replaceState(null, "", "/") })

  it("names the reason, keeps the input, and retries the same request", async () => {
    const posts: unknown[] = []
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/analyses" && init?.method === "POST") { posts.push(JSON.parse(String(init.body))); return Response.json({ analysis_id: "a_9", status: "queued", cached: false }) }
      if (url === "/api/analyses") return Response.json([])
      if (url === "/api/analyses/a_9") return Response.json({ ...FAILED_ANALYSIS, analysis_id: "a_9" })
      return Response.json({})
    }))
    history.replaceState(null, "", "/")
    const root = document.createElement("div")
    document.body.appendChild(root)
    await act(async () => { createRoot(root).render(<App />) })
    const input = root.querySelector("input[placeholder^='https://github.com']") as HTMLInputElement
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "https://github.com/acme/shop/pull/999")
      input.dispatchEvent(new Event("input", { bubbles: true }))
    })
    await act(async () => { (root.querySelector("form") as HTMLFormElement).requestSubmit() })
    await act(async () => { await new Promise((r) => setTimeout(r, 30)) })
    const fail = root.querySelector("[data-testid=analysis-failed]")!
    expect(fail.textContent).toContain(FAILED_ANALYSIS.error!)
    expect(fail.textContent).toContain("取得 PR 與原始碼")
    await act(async () => { ([...fail.querySelectorAll("button")].find((b) => b.textContent === "重試") as HTMLElement).click() })
    await act(async () => { await new Promise((r) => setTimeout(r, 30)) })
    expect(posts).toHaveLength(2)
    expect(posts[1]).toEqual(posts[0])
    await act(async () => { ([...root.querySelectorAll("[data-testid=analysis-failed] button")].find((b) => b.textContent === "修改輸入") as HTMLElement).click() })
    expect((root.querySelector("input[placeholder^='https://github.com']") as HTMLInputElement).value).toBe("https://github.com/acme/shop/pull/999")
  })
})

describe("step card", () => {
  it("shows the step, its status and provenance, but no evidence lines (owner: evidence is noise for review)", () => {
    const m = buildModel(sample.map, sample.overlay)
    const s = flowSteps(m, "flow:checkout").steps[1]
    const html = renderToStaticMarkup(<ExplorerProvider model={m} fixture><StepCard c={stepView(s)} index={1} total={5} m={m} /></ExplorerProvider>)
    expect(html).toContain(s.business_label!.text)
    expect(html).toContain("已確認的結構事實")
    expect(html).toContain("來源：AI 說明")
    expect(html).not.toContain("app/orders/services.py:11")
    expect(html).not.toContain("ev:step:checkout:1")
  })
})

describe("map framing", () => {
  it("frames the whole overview, and only what the PR changed in the PR view", async () => {
    const m = buildModel(sample.map, sample.overlay)
    const sysScene = buildScene(m, { mode: "system" })
    const sys = fitTargets(m, placeNodes(m, sysScene, await getLayout(m, sysScene), "system"), "system")
    expect(new Set(sys)).toEqual(new Set(sysScene.nodes.map((n) => n.id)))
    const prScene = buildScene(m, { mode: "pr" })
    const ov = fitTargets(m, placeNodes(m, prScene, await getLayout(m, prScene), "overlay"), "overlay")
    expect(ov.length).toBeGreaterThan(0)
    expect(ov.length).toBeLessThan(prScene.nodes.length)
    expect(ov.every((id) => prScene.nodes.find((n) => n.id === id)!.kind !== "summary")).toBe(true)
  })
})

describe("PR updated", () => {
  afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ""; history.replaceState(null, "", "/") })

  it("labels the analysed version and offers 分析新版 instead of mixing versions", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      const body = url.endsWith("/freshness") ? { stale: true, analyzed_head_sha: sample.overlay.pr.head_sha, current_head_sha: "1234567890abcdef" }
        : url.startsWith("/api/overlays/") ? sample.overlay : url.startsWith("/api/system-maps/") ? sample.map : {}
      return Response.json(body)
    }))
    history.replaceState(null, "", `/?overlay=${sample.overlay.overlay_id}`)
    const root = document.createElement("div")
    document.body.appendChild(root)
    await act(async () => { createRoot(root).render(<App />) })
    await act(async () => { await new Promise((r) => setTimeout(r, 30)) })
    expect(root.querySelector("[data-testid=analyzed-sha]")!.textContent).toBe(sample.overlay.pr.head_sha.slice(0, 7))
    const stale = root.querySelector("[data-testid=state-stale]")!
    expect(stale.textContent).toContain("1234567")
    expect(stale.textContent).toContain("不會混用兩個版本")
    expect([...stale.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["分析新版"])
  })
})
