// @vitest-environment happy-dom
import { act } from "react"
import { afterEach, describe, expect, it } from "vitest"
import { button, click, goNav, mount, nav } from "./test-utils"

const tid = (root: ParentNode, id: string) => root.querySelector(`[data-testid="${id}"]`)
const pop = async () => { await act(async () => { history.back(); await new Promise((r) => setTimeout(r, 20)) }) }
afterEach(() => { document.body.innerHTML = ""; history.replaceState(null, "", "/") })

describe("變更與影響: change cards lead into flows", () => {
  it("a card offers its flows and related features; the flow opens right under the card with why it is related", async () => {
    const root = await mount("sample", "#changes")
    const card = tid(root, "card-bc:code")!
    expect([...card.querySelectorAll(".card-actions button")].map((b) => b.textContent)).toEqual(["查看「結帳」流程", "查看「輸入折扣碼」流程", "查看相關功能"])
    await click(tid(card, "card-flow-flow:checkout"))
    expect(card.className).toContain("on")
    const focus = card.nextElementSibling!
    expect(focus.getAttribute("data-testid")).toBe("flow-focus")
    expect(tid(focus, "flow-why")!.textContent).toContain("這項前後差異的分析結果列出了這條流程")
    expect(focus.textContent).toContain("流程：可辨識的步驟順序")
    expect(focus.textContent).toContain("不是實際執行紀錄")
    expect(document.activeElement?.id).toBe("flow-focus")
    // the animation is optional: no player controls until the map is opened
    expect(button(focus, "逐段播放")).toBeUndefined()
    await click(button(focus, "在地圖上看這條流程（含動畫）"))
    expect(button(focus, "逐段播放")).toBeTruthy()
  })

  it("related flows are listed with their data-backed reason; unrelated flows sit behind a clear entry", async () => {
    const root = await mount("sample", "#changes")
    const rel = tid(root, "related-flows")!
    expect(rel.textContent).toContain("取消訂單流程")
    expect(rel.textContent).toContain("可能相關（潛在影響，未證實）")
    expect(tid(root, "other-flows")).toBeNull()
    await click(tid(root, "other-flows-toggle"))
    expect(tid(root, "other-flows")!.textContent).toContain("退款流程")
    await click(tid(root, "flow-flow:refund"))
    expect(tid(root, "flow-why")!.textContent).toBe("為什麼相關：與這次修改沒有已知關係，供了解背景。")
  })

  it("added / removed / possible steps are labelled by word and glyph", async () => {
    const root = await mount("sample", "#changes")
    await click(tid(root, "card-flow-flow:checkout"))
    const segs = [...root.querySelectorAll("[data-testid=flow-focus] .ax-seg")]
    const all: string[] = []
    for (const s of segs) { await click(s); all.push(...[...root.querySelectorAll("[data-testid=seg-detail] li.step")].map((li) => li.textContent ?? "")) }
    expect(all.some((t) => t.includes("+ 新增"))).toBe(true)
    expect(all.some((t) => t.includes("− 移除"))).toBe(true)
    expect(all.some((t) => t.includes("? 待確認"))).toBe(true)
  })

  it("without business texts: flow cards with step counts and technical cards, never a stitched before/after", async () => {
    const root = await mount("sample-nosemantic", "#changes")
    const flowCard = tid(root, "card-fc:flow:checkout")!
    expect(flowCard.textContent).toMatch(/新增 \d+ 步/)
    expect(flowCard.textContent).toContain("不描述業務差異")
    expect(root.querySelectorAll(".ba-tag").length).toBe(0)
  })

  it("back returns to the card with its flow and expanded segment", async () => {
    const root = await mount("sample", "#changes")
    await click(tid(root, "card-flow-flow:checkout"))
    await click(root.querySelector("[data-testid=flow-focus] .ax-seg"))
    const seg = root.querySelector("[data-testid=flow-focus] .ax-seg[aria-expanded=true]")!.textContent
    await goNav(root, "審查重點")
    await pop()
    expect(nav(root)).toBe("變更與影響")
    expect(tid(root, "card-bc:code")!.nextElementSibling?.getAttribute("data-testid")).toBe("flow-focus")
    expect(root.querySelector("[data-testid=flow-focus] .ax-seg[aria-expanded=true]")?.textContent).toBe(seg)
  })
})

describe("relation view + detail panel", () => {
  it("relation sentences with an equivalent text list, dependency told apart from flow", async () => {
    const root = await mount("sample", "#changes")
    await click(tid(root, "card-rel-bc:charge"))
    const v = tid(root, "relation-view")!
    expect(v.textContent).toContain("依賴關係圖：誰依賴誰")
    expect(v.textContent).toContain("不是執行順序，也不代表一定影響業務")
    const list = [...tid(v, "relation-list")!.querySelectorAll("li")].map((li) => li.textContent)
    expect(list.some((t) => /^(⚠ )?訂單呼叫付款（/.test(t ?? ""))).toBe(true)
  })

  it("large: at most 7 drawn nodes, the rest grouped with a total and an expandable list", async () => {
    const root = await mount("large", "#changes")
    await click(tid(root, "card-rel-bc:auto-invoice"))
    const v = tid(root, "relation-view")!
    const nodes = tid(v, "relation-nodes")!.querySelectorAll("li").length
    expect(nodes).toBeLessThanOrEqual(6) // + the group node = 7
    const group = tid(v, "relation-group")!
    const total = Number(group.textContent!.match(/其他 (\d+) 個相關功能/)![1])
    expect(total).toBeGreaterThan(0)
    await click(button(group, "展開清單"))
    expect(tid(v, "relation-group-nodes")!.querySelectorAll("li").length).toBe(total)
  })

  it("the panel is one level, titled by the selection; closing restores focus and the source card stays marked", async () => {
    const root = await mount("sample", "#changes")
    await click(tid(root, "card-rel-bc:charge"))
    const trigger = [...tid(root, "relation-nodes")!.querySelectorAll("button")].find((b) => b.textContent === "付款")!
    trigger.focus()
    await click(trigger)
    const panel = () => root.querySelectorAll("[data-testid=detail-panel]")
    expect(panel().length).toBe(1)
    expect(root.querySelector("#detail-title")!.textContent).toBe("付款")
    expect(document.activeElement?.id).toBe("detail-title")
    expect(tid(root, "card-bc:charge")!.className).toContain("on")
    // a reference inside the panel replaces its content: still one panel
    await click([...panel()[0].querySelectorAll("button.chip-btn")].find((b) => b.textContent === "退款"))
    expect(panel().length).toBe(1)
    expect(root.querySelector("#detail-title")!.textContent).toBe("退款")
    await click(button(panel()[0], "關閉 ✕"))
    expect(panel().length).toBe(0)
    expect(document.activeElement).toBe(trigger)
  })
})

describe("審查重點 cards", () => {
  it("lead with what to confirm; one status from the data; source is separate", async () => {
    const root = await mount("sample", "#review")
    const race = tid(root, "finding-fd:race")!
    expect(race.querySelector(".rc-confirm")!.textContent).toBe("需確認：折扣碼使用次數的更新沒有包在交易裡")
    expect(race.querySelectorAll(".status-badge").length).toBe(1)
    expect(race.querySelector(".status-badge")!.textContent).toContain("已確認的結構事實")
    expect(race.querySelector(".source-note")!.textContent).toBe("來源：靜態分析")
    expect(race.textContent).toContain("app/discounts/services.py")
    expect(race.querySelector("a")?.getAttribute("href")).toContain("github.com/acme/shop/blob/")
    // findings grouped by status, not by source
    expect(tid(root, "findings-needs_confirmation")!.querySelectorAll(".review-card").length).toBe(2)
    expect(tid(root, "findings-confirmed")!.querySelectorAll(".review-card").length).toBe(2)
    // a static interface change whose compatibility is unknown is 無法判定, not 已確認
    const receipt = tid(root, "arch-ac:if-receipt")!
    expect(receipt.querySelector(".source-note")!.textContent).toBe("來源：靜態分析")
    expect(receipt.querySelector(".status-badge")!.textContent).toContain("無法判定")
    expect(tid(root, "arch-ac:audit-signal")!.querySelector(".status-badge")!.textContent).toContain("待確認")
  })

  it("涉及功能 opens the panel and marks the source card", async () => {
    const root = await mount("sample", "#review")
    const card = tid(root, "finding-fd:stack")!
    await click([...card.querySelectorAll(".rc-caps button")].find((b) => b.textContent === "訂單"))
    expect(root.querySelector("#detail-title")!.textContent).toBe("訂單")
    expect(card.className).toContain("on")
  })
})
