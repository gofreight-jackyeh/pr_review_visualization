// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest"
import { sample } from "@/fixtures"
import { buildModel } from "@/entities/artifact/model"
import { localGraph, localScene } from "@/features/relations/local"
import { splitDetail } from "@/features/architecture/interfaces"
import { button, click, mount } from "./test-utils"

const tid = (root: ParentNode, id: string) => root.querySelector(`[data-testid="${id}"]`)
afterEach(() => { document.body.innerHTML = ""; history.replaceState(null, "", "/") })

describe("PR 摘要 first screen on real-size texts", () => {
  it("background shows only the first sentence; 更多背景 expands the full text in place", async () => {
    const root = await mount("longtext")
    const bg = tid(root, "bg-text")!
    expect(bg.textContent).toContain("這是一個線上商店後端，負責結帳與付款。")
    expect(bg.textContent).not.toContain("庫存")
    await click(tid(root, "bg-more"))
    expect(tid(root, "bg-text")!.textContent).toContain("庫存")
    expect(tid(root, "summary")).not.toBeNull() // expanded in place, still on the summary
  })

  it("the action row (primary + counts + 查看審查重點) comes before the difference card in DOM order", async () => {
    const root = await mount("longtext")
    const s = tid(root, "summary")!
    const row = tid(s, "next-row")!, diff = tid(s, "main-diff")!
    expect(row.compareDocumentPosition(diff) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(tid(row, "go-changes")).not.toBeNull()
    expect(tid(row, "count-iface")).not.toBeNull()
    expect(tid(row, "count-findings")).not.toBeNull()
    expect(button(row, "查看審查重點")).toBeTruthy()
    const purpose = [...s.querySelectorAll("h3")].find((h) => h.textContent === "這次修改")!
    expect(purpose.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it("very long before / after texts are clamped with an in-place 顯示全文", async () => {
    const root = await mount("longtext")
    const diff = tid(root, "main-diff")!
    const clamped = [...diff.querySelectorAll("[data-testid=clamped-text]")]
    expect(clamped.length).toBe(2)
    expect(clamped.every((p) => p.classList.contains("clamp4"))).toBe(true)
    await click(button(diff, "顯示全文"))
    expect([...diff.querySelectorAll("[data-testid=clamped-text]")].filter((p) => p.classList.contains("clamp4")).length).toBe(1)
  })
})

describe("local relation graph", () => {
  it("edge labels are off the canvas (sentence kept for hover / focus) and the equivalent text list is present", async () => {
    const m = buildModel(sample.map, sample.overlay)
    const c = sample.overlay.behavior_changes[0].capability_ids
    const scene = localScene(m, localGraph(m, c), "k")
    expect(scene.edges.length).toBeGreaterThan(0)
    expect(scene.edges.every((e) => e.label === undefined && !!e.sentence)).toBe(true)
    const root = await mount("sample", "#changes")
    await click(tid(root, "card-rel-bc:code"))
    expect(tid(root, "relation-list")!.querySelectorAll("li").length).toBeGreaterThan(0)
  })
})

describe("已知事實 vs 不確定之處", () => {
  it("splits a mixed detail: the established part is a fact, the 'cannot be determined' part is uncertainty", () => {
    expect(splitDetail("處理程式有改動，且請求 / 回應格式無法靜態判定")).toEqual({ facts: ["處理程式有改動"], uncertain: ["請求 / 回應格式無法靜態判定"] })
    expect(splitDetail("送往外部的內容有改動，無法靜態判定是否相容（移除 a；新增 b）")).toEqual({ facts: ["送往外部的內容有改動（移除 a；新增 b）"], uncertain: ["無法靜態判定是否相容"] })
    expect(splitDetail("改用不同的 Serializer；欄位 x 的設定改變")).toEqual({ facts: ["改用不同的 Serializer", "欄位 x 的設定改變"], uncertain: [] })
  })

  it("an unknown-compatibility interface card has no uncertainty phrase in 已知事實", async () => {
    const root = await mount("sample", "#review")
    const card = tid(root, "arch-ac:if-receipt")!
    const facts = card.querySelector(".rc-facts")!.textContent!
    expect(facts).toContain("收據文字範本內容有改動")
    expect(facts).not.toMatch(/無法|不確定|未知/)
    expect(card.querySelector(".rc-uncertain")!.textContent).toContain("靜態分析無法判定")
  })
})
