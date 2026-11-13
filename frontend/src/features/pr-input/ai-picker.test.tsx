// @vitest-environment happy-dom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { afterEach, expect, it, vi } from "vitest"
import type { AnalysisRequest, ServerConfig } from "@/shared/api/client"
import { aiLabel } from "@/shared/ai/ai"
import { PrInput } from "./PrInput"

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const CONFIG: ServerConfig = {
  ask: { mode: "local", allowed: true, reason: null },
  ai: { default: { engine: "claude", effort: "high" }, engines: [
    { engine: "claude", efforts: ["low", "medium", "high", "xhigh", "max"], models: ["opus", "sonnet"] },
    { engine: "codex", efforts: ["low", "medium", "high", "xhigh", "max"], models: ["gpt-x"] },
  ] },
}

afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = "" })

async function submitWith(pick: (root: HTMLElement) => void): Promise<AnalysisRequest> {
  const posts: AnalysisRequest[] = []
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } })
    if (url === "/api/config") return json(CONFIG)
    if (url === "/api/analyses" && init?.method === "POST") { posts.push(JSON.parse(String(init.body))); return json({ analysis_id: "a_1", status: "queued", cached: false }, 202) }
    if (url === "/api/analyses") return json([])
    return json({ analysis_id: "a_1", status: "queued", stages: [], error: null, overlay_id: null, system_map_id: null })
  }))
  const root = document.createElement("div")
  document.body.appendChild(root)
  await act(async () => { createRoot(root).render(<PrInput initialRequest={{ pr_url: "https://github.com/o/r/pull/1" }} onLoaded={() => {}} poll={async () => new Promise(() => {})} />) })
  await act(async () => { await Promise.resolve() })
  await act(async () => { pick(root) })
  await act(async () => { root.querySelector("form")!.requestSubmit() })
  await act(async () => { await Promise.resolve() })
  return posts[0]
}

const choose = (el: HTMLSelectElement | HTMLInputElement, value: string) => {
  // the prototype setter: React ignores an input event whose value it already tracks
  Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value")!.set!.call(el, value)
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }))
}

it("sends no ai when nothing is chosen, and the chosen engine / model / effort otherwise", async () => {
  expect((await submitWith(() => {})).ai).toBeUndefined()
  document.body.innerHTML = ""
  const req = await submitWith((root) => {
    choose(root.querySelector<HTMLSelectElement>("[data-testid=ai-engine]")!, "codex")
    choose(root.querySelector<HTMLInputElement>("[data-testid=ai-model]")!, "gpt-x")
    choose(root.querySelector<HTMLSelectElement>("[data-testid=ai-effort]")!, "medium")
  })
  expect(req.ai).toEqual({ engine: "codex", model: "gpt-x", effort: "medium" })
})

it("labels the AI that wrote an overlay", () => {
  expect(aiLabel({ engine: "codex", model: "gpt-x", effort: "high" })).toBe("Codex · gpt-x · effort high")
  expect(aiLabel({ engine: "claude" })).toBe("Claude")
  expect(aiLabel(undefined)).toBeNull()
})
