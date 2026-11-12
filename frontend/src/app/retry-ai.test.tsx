// @vitest-environment happy-dom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { afterEach, describe, expect, it, vi } from "vitest"
import { sample } from "@/fixtures"
import type { PrOverlay, SystemMap } from "@/entities/artifact/types"
import { App } from "./App"
import { button, click } from "./test-utils"

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

type Sem = { status: string; decision?: { score: number; reasons: string[]; mode: string } }
function docs(sem: Sem): { map: SystemMap; overlay: PrOverlay } {
  const map = structuredClone(sample.map), overlay = structuredClone(sample.overlay)
  overlay.pr.url = "https://github.com/o/r/pull/1"
  map.semantic = { ...map.semantic, status: "done" } as SystemMap["semantic"]
  overlay.semantic = { ...overlay.semantic, ...sem } as PrOverlay["semantic"]
  return { map, overlay }
}

async function open(d: { map: SystemMap; overlay: PrOverlay }, posted: unknown[] = []) {
  vi.useFakeTimers()
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") { posted.push(url); return new Response(JSON.stringify({ analysis_id: "a_2", status: "queued", cached: false }), { status: 202 }) }
    const body = url.startsWith("/api/analyses/") ? { analysis_id: "a_2", status: "running", stages: [], error: null, overlay_id: d.overlay.overlay_id, system_map_id: d.map.system_map_id }
      : url.endsWith("/freshness") ? { stale: false, analyzed_head_sha: "x", current_head_sha: "x" }
      : url.startsWith("/api/overlays/") ? d.overlay : d.map
    return new Response(JSON.stringify(body), { status: 200 })
  }))
  history.replaceState(null, "", `/?overlay=${d.overlay.overlay_id}&analysis=a_1`)
  const root = document.createElement("div")
  document.body.appendChild(root)
  await act(async () => { createRoot(root).render(<App />) })
  await act(async () => { await vi.advanceTimersByTimeAsync(10) })
  return root
}
const retry = (root: HTMLElement) => root.querySelector("[data-testid=retry-ai]")

describe("重新產生 AI 說明", () => {
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); document.body.innerHTML = ""; history.replaceState(null, "", "/") })

  it("is offered when the AI texts failed or are unavailable", async () => {
    expect(retry(await open(docs({ status: "failed" })))).not.toBeNull()
    document.body.innerHTML = ""
    expect(retry(await open(docs({ status: "unavailable" })))).not.toBeNull()
  })

  it("is hidden when everything is done, when Claude is off by choice, and while a run is in progress", async () => {
    expect(retry(await open(docs({ status: "done" })))).toBeNull()
    document.body.innerHTML = ""
    expect(retry(await open(docs({ status: "skipped", decision: { score: 0, reasons: [], mode: "off" } })))).toBeNull()
    document.body.innerHTML = ""
    expect(retry(await open(docs({ status: "pending" })))).toBeNull()
  })

  it("click asks the server to repeat the original request, switches to pending and keeps the view", async () => {
    const posted: unknown[] = []
    const d = docs({ status: "failed" })
    const root = await open(d, posted)
    await click([...root.querySelectorAll(".main-nav button")].find((b) => b.textContent === "專案背景"))
    await click(button(root, "重新產生 AI 說明"))
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(posted).toEqual([`/api/overlays/${d.overlay.overlay_id}/regenerate`])
    expect(root.querySelector("[data-testid=state-pending]")).not.toBeNull()
    expect(retry(root)).toBeNull()
    expect(root.querySelector(".main-nav [aria-current=page]")?.textContent).toBe("專案背景")
  })
})
