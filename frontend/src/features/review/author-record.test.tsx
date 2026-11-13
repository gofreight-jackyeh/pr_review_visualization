// @vitest-environment happy-dom
import { afterEach, expect, it } from "vitest"
import { mount } from "@/app/test-utils"

afterEach(() => { document.body.innerHTML = "" })

it("shows the author's own record apart from the AI's questions, with the author's words", async () => {
  const root = await mount("sample", "#review")
  const sec = root.querySelector("[data-testid=review-author]")!
  expect(sec, "author record section").toBeTruthy()
  expect(sec.querySelector("[data-testid=author-settled_decision] h4")?.textContent).toContain("作者記為已定案的決策")
  expect(sec.querySelector("blockquote")?.textContent).toBe("並移除固定 10% 會員折扣")
  expect(root.querySelector("[data-testid=index-author]")?.textContent).toBe("作者的記錄 1 項")
  // after the questions (they stay first), not counted among them
  expect(sec.compareDocumentPosition(root.querySelector("[data-testid=review-focus]")!) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy()
})

it("has no section when the description records nothing", async () => {
  const root = await mount("sample-nosemantic", "#review")
  expect(root.querySelector("[data-testid=review-author]")).toBeNull()
})
