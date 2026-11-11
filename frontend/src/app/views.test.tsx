// @vitest-environment happy-dom
import { act } from "react"
import { afterEach, describe, expect, it } from "vitest"
import { large, sample } from "@/fixtures"
import { buildModel } from "@/entities/artifact/model"
import { archGroupOf, groupChecks } from "@/features/architecture/arch"
import { focusQuestion } from "@/features/focus/focus"
import { button, click, goNav, mount, nav } from "./test-utils"

const tid = (root: ParentNode, id: string) => root.querySelector(`[data-testid="${id}"]`)
const pop = async (dir: "back" | "forward") => {
  await act(async () => { history[dir](); await new Promise((r) => setTimeout(r, 20)) })
}

afterEach(() => { document.body.innerHTML = ""; history.replaceState(null, "", "/") })

describe("architecture consistency", () => {
  const checks = sample.overlay.architecture_checks
  it("groups checks 違反 / 新增架構債 / 需要確認 / 符合", () => {
    const g = groupChecks(checks)
    expect(g.violates.map((c) => c.id)).toEqual(["ac:discounts-orders", "ac:payments-api"]) // documented + inferred deviation, both verified
    expect(g.new_debt.map((c) => c.id)).toEqual(["ac:il-ignore"])
    expect(g.confirm.map((c) => c.id)).toEqual(["ac:audit-signal"]) // Claude hypothesis
    expect(g.follows.map((c) => c.id)).toEqual(["ac:notify"])
    expect(archGroupOf({ ...checks[0], status: "needs_confirmation" })).toBe("confirm") // unverified never reads as a verdict
  })
})

describe("main navigation", () => {
  it("opens on PR 摘要 with one navigation of four entries (no left/right tab sets)", async () => {
    const root = await mount("sample")
    expect([...root.querySelectorAll(".main-nav button")].map((b) => b.textContent)).toEqual(["PR 摘要", "變更與影響", "審查重點", "專案背景"])
    expect(nav(root)).toBe("PR 摘要")
    expect(root.querySelectorAll("[role=tablist]").length).toBe(0)
    for (const [label, testid] of [["變更與影響", "changes"], ["審查重點", "review"], ["專案背景", "context"], ["PR 摘要", "summary"]] as const) {
      await goNav(root, label)
      expect(nav(root)).toBe(label)
      expect(tid(root, testid), label).not.toBeNull()
    }
  })

  it("a plain #name hash opens that entry", async () => {
    const root = await mount("sample", "#review")
    expect(nav(root)).toBe("審查重點")
  })
})

describe("PR summary first screen", () => {
  it("follows the wireframe: title + GitHub, background + 更多背景, purpose, one main difference, action, separate counts", async () => {
    const root = await mount("sample")
    const s = tid(root, "summary")!
    expect(s.querySelector("h2")!.textContent).toBe("PR：Add discount code to checkout")
    expect(s.querySelector("a.btn")!.getAttribute("href")).toBe(sample.overlay.pr.url)
    const headings = [...s.querySelectorAll("h3")].map((h) => h.textContent)
    expect(headings.slice(0, 3)).toEqual(["相關背景", "這次修改", "主要差異"])
    expect(s.textContent).toContain(sample.map.system.summary!.text)
    expect(s.textContent).toContain(sample.overlay.purpose.text)
    expect(s.querySelectorAll("[data-testid=main-diff]").length).toBe(1)
    expect(tid(s, "main-diff")!.textContent).toContain(sample.overlay.behavior_changes[0].after.text)
    // counts are separate per category, never summed
    expect(tid(s, "count-iface")!.textContent).toBe("介面變動：5 項（其中 2 項可能破壞客戶端）")
    expect(tid(s, "count-findings")!.textContent).toBe("待確認事項：4 項")
    expect(s.textContent).not.toMatch(/共 \d+ 項風險|風險分數/)
  })

  it("several differences: a visible choice that says its order is not importance", async () => {
    const root = await mount("sample")
    const pick = tid(root, "diff-pick")!
    expect(pick.textContent).toContain("不代表重要性")
    const radios = [...pick.querySelectorAll("[role=radio]")]
    expect(radios.map((r) => r.getAttribute("aria-checked"))).toEqual(["true", "false"])
    await click(radios[1])
    expect(tid(root, "main-diff")!.textContent).toContain(sample.overlay.behavior_changes[1].after.text)
    // 查看這次修改 opens 變更與影響 with that card selected and focused
    await click(tid(root, "go-changes"))
    expect(nav(root)).toBe("變更與影響")
    expect(root.querySelector(".change-card.on")?.getAttribute("data-testid")).toBe("card-bc:charge")
    expect(document.activeElement?.getAttribute("data-testid")).toBe("card-bc:charge")
  })

  it("without business texts: no stitched difference, the static changes instead, and the action still leads somewhere", async () => {
    const root = await mount("sample-nosemantic")
    expect(tid(root, "main-diff")).toBeNull()
    const st = tid(root, "main-diff-static")!
    expect(st.textContent).toContain("業務說明不可用，這裡不拼湊業務差異")
    expect(st.textContent).toContain("checkout（流程）")
    expect(tid(root, "state-unavailable")!.textContent).toContain("semantic_mode=off")
    expect(tid(root, "summary")!.textContent).toContain("PR 描述") // purpose source
    await click(tid(root, "go-changes"))
    expect(root.querySelectorAll(".change-card").length).toBeGreaterThan(0)
  })

  it("reading path: an item only expands in place; its button navigates", async () => {
    const root = await mount("sample")
    const item = tid(root, "guide-wt:3")!
    await click(item.querySelector("button"))
    expect(item.textContent).toContain("結帳流程被修改")
    expect(nav(root)).toBe("PR 摘要")
    await click(item.querySelector("button"))
    expect(item.querySelector("[data-testid=guide-go]")).toBeNull()
    const fd = tid(root, "guide-wt:6")!
    await click(fd.querySelector("button"))
    await click(fd.querySelector("[data-testid=guide-go]"))
    expect(nav(root)).toBe("審查重點")
    expect(document.activeElement?.id).toBe("review-fd_race")
    expect(root.querySelector(".review-card.on")?.getAttribute("data-testid")).toBe("finding-fd:race")
  })
})

describe("browser back / forward", () => {
  it("restores the entry, the source card, expanded blocks and the scroll position", async () => {
    const root = await mount("sample")
    const content = () => tid(root, "content") as HTMLElement
    // summary: expand a reading-path item and scroll
    await click(tid(root, "guide-wt:2")!.querySelector("button"))
    content().scrollTop = 140
    await click(tid(root, "go-changes"))
    expect(nav(root)).toBe("變更與影響")
    await goNav(root, "審查重點")
    await click(root.querySelector("[data-testid=arch-group-follows] button"))
    expect(tid(root, "arch-ac:notify")).not.toBeNull() // 符合 expanded
    content().scrollTop = 90
    await pop("back")
    expect(nav(root)).toBe("變更與影響")
    expect(root.querySelector(".change-card.on")?.getAttribute("data-testid")).toBe("card-bc:code")
    await pop("back")
    expect(nav(root)).toBe("PR 摘要")
    expect(tid(root, "guide-wt:2")!.querySelector("[data-testid=guide-go]")).not.toBeNull()
    expect(content().scrollTop).toBe(140)
    await pop("forward")
    await pop("forward")
    expect(nav(root)).toBe("審查重點")
    expect(tid(root, "arch-ac:notify")).not.toBeNull()
    expect(content().scrollTop).toBe(90)
  })
})

describe("browser back / forward (reading path)", () => {
  it("an opened FAQ answer is still open after 「看…」 and Back", async () => {
    const root = await mount("sample")
    await click(button(root, "付款會受到影響嗎？"))
    const answer = () => root.querySelector("[data-testid=reading-path] .notice.info")
    expect(answer()!.textContent).toContain("付款會受到影響嗎？")
    await click(answer()!.querySelector("[data-testid=guide-go]"))
    expect(nav(root)).not.toBe("PR 摘要")
    await pop("back")
    expect(nav(root)).toBe("PR 摘要")
    expect(answer()!.textContent).toContain("付款會受到影響嗎？")
  })
})

describe("analysis states", () => {
  it("pending: says what is still being written and offers the recognised changes", async () => {
    const root = await mount("pending")
    expect(tid(root, "state-pending")!.textContent).toContain("業務說明產生中")
    expect(tid(root, "count-findings")!.textContent).toContain("AI 疑點產生中")
    await click(button(tid(root, "state-pending")!, "查看已辨識的修改"))
    expect(nav(root)).toBe("變更與影響")
  })

  it("partial failure: keeps the static results, names what is missing and why", async () => {
    const root = await mount("partial")
    const n = tid(root, "state-partial")!.textContent!
    expect(n).toContain("這次修改的行為差異與 AI 疑點")
    expect(n).toContain("Claude 呼叫逾時（120 秒）")
    expect(n).toContain("回到首頁以相同 PR 重新分析") // fixtures cannot re-run; a real GitHub PR gets a 重新分析 button
    expect(tid(root, "count-iface")!.textContent).toContain("5 項")
  })

  it("no flows / empty results: 本次分析未發現 with its scope, never 「沒有風險」, and the files instead of a flow", async () => {
    const root = await mount("empty")
    expect(tid(root, "state-no-flows")).not.toBeNull()
    expect(tid(root, "count-iface")!.textContent).toBe("介面變動：本次分析未發現")
    expect(tid(root, "count-findings")!.textContent).toBe("待確認事項：本次分析未發現")
    expect(tid(root, "count-scope")!.textContent).toContain("分析範圍：這個 PR 修改的 2 個檔案")
    await click(tid(root, "go-changes"))
    expect(root.querySelector("[data-testid='card-tech:none']")!.textContent).toContain(".github/workflows/ci.yml")
    expect(root.querySelector("[data-testid='card-tech:cap:core']")!.textContent).toContain("app/core/settings_loader.py")
    await goNav(root, "審查重點")
    const review = tid(root, "review")!.textContent!
    expect(review).toContain("本次分析未發現對外介面變動")
    expect(review).toContain("分析範圍：這個 PR 修改的 2 個檔案")
    expect(review).not.toMatch(/沒有風險|無風險/)
  })
})

describe("re-homed views", () => {
  it("審查重點 leads with 對外介面變動 (outer → middle), checklist on outer modified, separate counts", async () => {
    const root = await mount("sample")
    await goNav(root, "審查重點")
    const r = tid(root, "review")!
    expect([...r.querySelectorAll("h3")].map((h) => h.textContent)).toEqual(["對外介面變動 5 項", "架構一致性 5 項", "待確認事項 4 項"])
    expect(tid(r, "arch-summary")!.textContent).toContain("外圈 4 項變動（1 項可能破壞）")
    expect([...r.querySelectorAll("[data-testid^=iface-ring-]")].map((e) => e.getAttribute("data-testid"))).toEqual(["iface-ring-outer", "iface-ring-middle"])
    const cancel = tid(r, "arch-ac:if-cancel")!
    for (const t of ["取消訂單", "對外 API", "修改", "可能破壞客戶端", "app/orders/views.py:97", "能否改在更內層"]) expect(cancel.textContent, t).toContain(t)
    expect(tid(r, "checklist-ac:if-apply")).toBeNull()
    expect(tid(r, "checklist-ac:if-checkout")).not.toBeNull()
    expect(tid(r, "arch-ac:discounts-orders")!.textContent).toContain(".importlinter.ini:12-18")
    expect(tid(r, "arch-ac:payments-api")!.textContent).toContain("7 次跨能力呼叫付款中有 6 次")
  })

  it("在地圖上標出 opens 專案背景 on the PR map with the check selected", async () => {
    const root = await mount("sample")
    await goNav(root, "審查重點")
    await click(tid(root, "arch-show-ac:discounts-orders"))
    expect(nav(root)).toBe("專案背景")
    expect(root.querySelector("[data-testid=focus-question] h2")!.textContent).toBe("這個 PR 動到系統的哪裡？")
  })

  it("專案背景: domain → capability drill-down with breadcrumb", async () => {
    const root = await mount("sample")
    await goNav(root, "專案背景")
    const crumbs = () => root.querySelector("nav.crumbs")!.textContent!.replace(/\s+\d+ \/ 25.*/, "")
    expect(crumbs()).toBe("系統")
    await click(tid(root, "context-list-toggle")) // the map's keyboard / text equivalent
    await click(button(root, "交易"))
    expect(crumbs()).toBe("系統›交易")
    await click([...root.querySelectorAll("button.chip-btn")].find((b) => b.textContent === "訂單"))
    expect(crumbs()).toBe("系統›交易›訂單")
    expect(tid(root, "detail-panel")!.querySelector("#detail-title")!.textContent).toBe("訂單")
    expect(root.querySelector(".cap-lists")?.textContent).toContain("入口（3）") // incl. the one the PR adds
  })

  it("變更與影響: flow axis keeps 3–7 segments and ≤ 7 steps per expanded segment", async () => {
    const root = await mount("large")
    await goNav(root, "變更與影響")
    await click(tid(root, "card-flow-flow:ship_to_invoice"))
    expect(root.textContent).toContain("依能力分段")
    expect(root.querySelectorAll("[data-testid=axis-collapsed]").length).toBeGreaterThan(0)
    await click(root.querySelector("[data-testid^=seg-] .ax-seg"))
    const shown = root.querySelectorAll("[data-testid=seg-detail] li.step").length
    expect(shown).toBeGreaterThan(0)
    expect(shown).toBeLessThanOrEqual(7)
    await click(tid(root, "card-flow-flow:cart_to_checkout"))
    expect(root.textContent).toContain("由 Claude 分段")
  })
})

describe("focus questions (pure)", () => {
  const m = buildModel(large().map, large().overlay)
  it("covers each map level and the flow view", () => {
    expect(focusQuestion(m, { view: "map", mode: "system", level: { domainId: "dom:acct" } })).toBe("「會計財務」由哪些業務能力組成？")
    expect(focusQuestion(m, { view: "map", mode: "pr", level: { domainId: "dom:acct" } })).toBe("這個 PR 動到「會計財務」的哪些能力？")
    expect(focusQuestion(m, { view: "map", mode: "system", level: { capId: "cap:finance.gl" } })).toBe("「總帳」負責什麼、和誰往來？")
    expect(focusQuestion(m, { view: "flow", flowId: "flow:ship_to_invoice" })).toBe("這個 PR 讓「出貨到開立發票」有什麼不同？")
    expect(focusQuestion(buildModel(large().map), { view: "flow", flowId: "flow:ship_to_invoice" })).toBe("「出貨到開立發票」怎麼運作？")
  })
})
