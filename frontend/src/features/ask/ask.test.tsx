// @vitest-environment happy-dom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { afterEach, describe, expect, it, vi } from "vitest"
import { sample } from "@/fixtures"
import { buildModel, nameOfId } from "@/entities/artifact/model"
import type { Question, ServerConfig } from "@/shared/api/client"
import { App } from "@/app/App"
import { POLL_MS } from "./AskPanel"

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const OV = sample.overlay.overlay_id
const q = (over: Partial<Question>): Question => ({
  question_id: "q_1", overlay_id: OV, created_at: "2026-11-12T00:00:00Z", question: "為什麼要改？", selected_id: null,
  status: "done", answer: null, error: null, ...over,
})
const DONE = q({
  question_id: "q_done", question: "折扣碼怎麼驗證？",
  answer: {
    text: "先檢查 **有效期限**，再檢查使用次數。", unsupported: false, dropped: [],
    cites: [
      { id: "ev:comp:validate_code", kind: "evidence", label: "validate_code 新增於 discounts services", refs: [{ side: "head", path: "app/discounts/services.py", start_line: 55 }] },
      { id: "cf:orders-views", kind: "file", label: "app/orders/views.py", refs: [{ side: "head", path: "app/orders/views.py" }] },
      { id: "fd:race", kind: "finding", label: "折扣碼使用次數的更新沒有包在交易裡", refs: [] },
    ],
  },
})
const UNSUPPORTED = q({ question_id: "q_uns", question: "會不會很慢？", answer: { text: "可能不會。", unsupported: true, dropped: ["ev:ghost"], cites: [] } })

type Server = { config: ServerConfig | null; questions: Question[]; posts: { question: string; selected_id: string | null }[] }

function serve(s: Server) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } })
    if (url === "/api/config") return s.config ? json(s.config) : json({ error: { code: "not_found", message: "no" } }, 404)
    if (url.endsWith("/questions") && init?.method === "POST") {
      s.posts.push(JSON.parse(String(init.body)))
      s.questions = [...s.questions, q({ question_id: "q_new", question: s.posts.at(-1)!.question, selected_id: s.posts.at(-1)!.selected_id, status: "running" })]
      return json({ question_id: "q_new", status: "queued" }, 202)
    }
    if (url.endsWith("/questions")) return json(s.questions)
    if (url.endsWith("/freshness")) return json({ stale: false, analyzed_head_sha: "x", current_head_sha: "x" })
    if (url.startsWith("/api/overlays/")) return json(sample.overlay)
    if (url.startsWith("/api/system-maps/")) return json(sample.map)
    return json({})
  }))
}

const flush = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const click = async (el: Element | null | undefined) => { expect(el, "element to click").toBeTruthy(); await act(async () => { (el as HTMLElement).click() }) }
const byText = (root: ParentNode, sel: string, text: string) => [...root.querySelectorAll(sel)].find((b) => b.textContent?.trim() === text)

async function mount() {
  history.replaceState(null, "", `/?overlay=${OV}`)
  const root = document.createElement("div")
  document.body.appendChild(root)
  await act(async () => { createRoot(root).render(<App />) })
  await flush(10)
  return root
}

async function type(root: HTMLElement, text: string) {
  const ta = root.querySelector("[data-testid=ask-input]") as HTMLTextAreaElement
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(ta, text)
    ta.dispatchEvent(new Event("input", { bubbles: true }))
  })
}

describe("問 AI panel", () => {
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); document.body.innerHTML = ""; history.replaceState(null, "", "/") })

  it("shows the history: labelled AI answer as markdown, linked cites, and the unsupported warning", async () => {
    vi.useFakeTimers()
    serve({ config: { ask: { mode: "local", allowed: true, reason: null } }, questions: [DONE, UNSUPPORTED], posts: [] })
    const root = await mount()
    await click(root.querySelector("[data-testid=ask-open]"))
    await flush(10)
    const panel = root.querySelector("[data-testid=ask-panel]")!
    expect(panel.querySelector("[data-testid=ask-ai-label]")?.textContent).toContain("AI 解讀，需要自行確認")
    const turns = panel.querySelectorAll("[data-testid=ask-turn]")
    expect(turns).toHaveLength(2)
    expect(turns[0].querySelector(".ask-answer strong")?.textContent).toBe("有效期限")  // markdown rendered
    expect([...turns[0].querySelectorAll("[data-testid=ref-text]")].map((x) => x.textContent)).toEqual(["app/discounts/services.py:55"])
    expect(turns[0].querySelectorAll("[data-testid=diff-link]").length).toBeGreaterThanOrEqual(2)  // line ref + changed file
    expect(byText(turns[0], "button.chip-btn", "折扣碼使用次數的更新沒有包在交易裡")).toBeTruthy()
    expect(turns[0].querySelector("[data-testid=ask-unsupported]")).toBeNull()
    expect(turns[1].querySelector("[data-testid=ask-unsupported]")?.textContent).toContain("未附來源，請自行確認")
    expect(turns[1].querySelector("[data-testid=ask-cites]")).toBeNull()
    // a cited finding opens in the detail panel, which replaces the ask panel (one layer)
    await click(byText(turns[0], "button.chip-btn", "折扣碼使用次數的更新沒有包在交易裡"))
    expect(root.querySelector("[data-testid=ask-panel]")).toBeNull()
    expect(root.querySelector("#detail-title")?.textContent).toBe("折扣碼使用次數的更新沒有包在交易裡")
  })

  it("sends the question with the selected item, waits while it is answered, then shows the answer", async () => {
    vi.useFakeTimers()
    const s: Server = { config: { ask: { mode: "local", allowed: true, reason: null } }, questions: [], posts: [] }
    serve(s)
    const root = await mount()
    // select a capability in the detail panel first (專案背景 → list → chip)
    await click(byText(root, ".main-nav button", "專案背景"))
    await click(root.querySelector("[data-testid=context-list-toggle]"))
    const capName = nameOfId(buildModel(sample.map, sample.overlay), "cap:orders").primary
    await click(byText(root, "button.chip-btn", capName))
    expect(root.querySelector("#detail-title")?.textContent).toBe(capName)

    const opener = root.querySelector("[data-testid=ask-open]") as HTMLElement
    await click(opener)
    await flush(10)
    expect(root.querySelector("[data-testid=detail-panel]")).toBeNull()  // one layer
    expect(root.querySelector("[data-testid=ask-context]")?.textContent).toContain(`關於：${capName}`)
    await type(root, "這個功能會影響退款嗎？")
    await click(byText(root, "[data-testid=ask-form] button", "送出"))
    await flush(10)
    expect(s.posts).toEqual([{ question: "這個功能會影響退款嗎？", selected_id: "cap:orders" }])
    expect(root.querySelector("[data-testid=ask-waiting]")?.textContent).toContain("AI 正在閱讀")
    expect((byText(root, "[data-testid=ask-form] button", "送出") as HTMLButtonElement).disabled).toBe(true)

    s.questions = [{ ...s.questions[0], status: "done", answer: { text: "不會，只影響 *結帳*。", unsupported: true, dropped: [], cites: [] } }]
    await flush(POLL_MS + 10)
    expect(root.querySelector("[data-testid=ask-waiting]")).toBeNull()
    expect(root.querySelector(".ask-answer em")?.textContent).toBe("結帳")

    // the context chip is removable: the next question goes without it
    await click(root.querySelector("[data-testid=ask-context] button"))
    expect(root.querySelector("[data-testid=ask-context]")).toBeNull()
    await type(root, "那通知呢？")
    await click(byText(root, "[data-testid=ask-form] button", "送出"))
    await flush(10)
    expect(s.posts[1]).toEqual({ question: "那通知呢？", selected_id: null })

    // Esc closes and returns focus to the 問 AI button
    await act(async () => { root.querySelector("[data-testid=ask-panel]")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })) })
    expect(root.querySelector("[data-testid=ask-panel]")).toBeNull()
    expect(document.activeElement).toBe(opener)
  })

  it("is hidden when the server turns it off, and read-only for a client the server does not allow", async () => {
    vi.useFakeTimers()
    serve({ config: { ask: { mode: "off", allowed: false, reason: "off" } }, questions: [], posts: [] })
    let root = await mount()
    expect(root.querySelector("[data-testid=ask-open]")).toBeNull()
    document.body.innerHTML = ""

    serve({ config: { ask: { mode: "local", allowed: false, reason: "local only" } }, questions: [DONE], posts: [] })
    root = await mount()
    await click(root.querySelector("[data-testid=ask-open]"))
    await flush(10)
    expect(root.querySelector("[data-testid=ask-input]")).toBeNull()
    expect(root.querySelector("[data-testid=ask-denied]")?.textContent).toContain("PR_EXPLORER_ASK=local")
    expect(root.querySelectorAll("[data-testid=ask-turn]")).toHaveLength(1)  // earlier answers stay readable
    document.body.innerHTML = ""

    serve({ config: null, questions: [], posts: [] })  // a backend without the feature
    root = await mount()
    expect(root.querySelector("[data-testid=ask-open]")).toBeNull()
  })
})
