// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { sampleNoSemantic } from "@/fixtures"
import { buildModel } from "@/entities/artifact/model"
import { renderMarkdown } from "@/shared/ui/Markdown"
import { StaticGroups } from "@/features/changes/StaticChange"
import { mount } from "@/app/test-utils"

const tid = (root: ParentNode, id: string) => root.querySelector(`[data-testid="${id}"]`)
afterEach(() => { document.body.innerHTML = ""; history.replaceState(null, "", "/") })

describe("這次修改 from the PR description (no AI summary)", () => {
  it("renders Markdown headings and lists, labelled 不可用 when Claude is off", async () => {
    const root = await mount("sample-nosemantic")
    const box = tid(root, "purpose-md")!
    expect(tid(box, "purpose-label")!.textContent).toBe("PR 描述（AI 摘要不可用）")
    expect(box.querySelector(".markdown h2")?.textContent).toBe("變更內容")
    expect(box.querySelectorAll(".markdown li").length).toBeGreaterThan(2)
    expect(box.querySelector(".markdown a")?.getAttribute("rel")).toBe("noreferrer noopener")
    expect(tid(box, "purpose-more")!.textContent).toBe("展開全文")
  })
  it("is labelled 尚未產生 while the AI summary is pending", async () => {
    const root = await mount("pending")
    expect(tid(root, "purpose-label")!.textContent).toBe("PR 描述（AI 摘要尚未產生）")
  })
  it("never renders raw HTML or unsafe links from the body", () => {
    const html = renderMarkdown("# T\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[x](javascript:alert(1)) [ok](https://example.com)")
    expect(html).not.toMatch(/<script|<img|href="javascript/i)
    expect(html).toContain("&lt;script&gt;")
    expect(html).toContain('<a href="https://example.com" target="_blank" rel="noreferrer noopener">ok</a>')
    expect(html).toContain("<h1>T</h1>")
  })
  it("an AI purpose stays plain text", async () => {
    const root = await mount("sample")
    expect(tid(root, "purpose-md")).toBeNull()
    expect(tid(root, "summary")!.textContent).toContain("來源：AI 說明")
  })
})

describe("修改前／修改後 without Claude text", () => {
  it("shows changed steps and files with PR diff links, not the static pseudo-sentences", async () => {
    const root = await mount("sample-nosemantic", "#changes")
    expect(tid(root, "card-bc:static:flow:checkout")).toBeNull()
    const card = tid(root, "card-fc:flow:checkout")!
    const groups = tid(card, "static-flow:checkout")!
    expect(groups.textContent).toContain("app/payments/services.py")
    expect(groups.textContent).toContain("charge(cart.total)")
    const links = [...groups.querySelectorAll("[data-testid=diff-link]")].map((a) => a.getAttribute("href"))
    expect(links).toContain("https://github.com/acme/shop/pull/482/files#diff-8be37ad304dda2e424dd74596b2767343a31a443d4429d9f382895f1f3d5416b")
    expect(links.some((h) => /#diff-[0-9a-f]{64}[RL]\d+$/.test(h!))).toBe(true)
    expect(root.textContent).not.toContain("[added]")
    expect(root.textContent).not.toContain("修改後：[")
    expect(card.textContent).toContain("AI 說明不可用")
  })
  it("the summary's static list carries the same files and links", async () => {
    const root = await mount("sample-nosemantic")
    const st = tid(root, "main-diff-static")!
    expect(st.querySelectorAll("[data-testid=diff-link]").length).toBeGreaterThan(0)
    expect(st.textContent).not.toContain("[changed]")
  })
  it("local-repo mode: same files, no links", () => {
    const ov = structuredClone(sampleNoSemantic.overlay)
    ov.pr = { ...ov.pr, source: "local", url: undefined }
    const html = renderToStaticMarkup(<StaticGroups m={buildModel(sampleNoSemantic.map, ov)} flowId="flow:checkout" />)
    expect(html).toContain("app/payments/services.py")
    expect(html).not.toContain("diff-link")
    expect(html).not.toContain("github.com")
  })
  it("the AI before/after is unchanged when Claude text exists", async () => {
    const root = await mount("sample", "#changes")
    const card = tid(root, "card-bc:code")!
    expect(card.querySelectorAll(".ba-tag").length).toBe(2)
    expect(card.querySelector("[data-testid^=static-]")).toBeNull()
  })
})
