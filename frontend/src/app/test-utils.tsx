import { act } from "react"
import { createRoot } from "react-dom/client"
import { expect } from "vitest"
import { App } from "./App"

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

export async function mount(fixture: string, hash = "") {
  history.replaceState(null, "", `/?fixture=${fixture}${hash}`)
  const root = document.createElement("div")
  document.body.appendChild(root)
  await act(async () => { createRoot(root).render(<App />) })
  await act(async () => { await new Promise((r) => setTimeout(r, 30)) })
  return root
}
export const click = async (el: Element | null | undefined) => { expect(el, "element to click").toBeTruthy(); await act(async () => { (el as HTMLElement).click() }) }
export const button = (root: ParentNode, text: string | RegExp) => [...root.querySelectorAll("button")].find((b) => (typeof text === "string" ? b.textContent?.trim() === text : text.test(b.textContent ?? "")))
export const nav = (root: HTMLElement) => root.querySelector(".main-nav [aria-current=page]")?.textContent
export const goNav = (root: HTMLElement, label: string) => click(button(root.querySelector(".main-nav")!, label))
