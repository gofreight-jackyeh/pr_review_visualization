import type { ReactElement } from "react"
import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { FlowHeading, ViewFlowLabel } from "./FlowName"

const html = (el: ReactElement) => renderToStaticMarkup(el)
const text = (el: ReactElement) => html(el).replace(/<[^>]+>/g, "")

describe("flow labels", () => {
  it("business name: 查看「<name>」流程, no doubled 流程", () => {
    expect(text(<ViewFlowLabel n={{ primary: "結帳", secondary: "/api/checkout", business: true }} />)).toBe("查看「結帳」流程")
    expect(text(<ViewFlowLabel n={{ primary: "結帳流程", business: true }} />)).toBe("查看「結帳」流程")
    expect(text(<FlowHeading n={{ primary: "結帳", business: true }} />)).toBe("結帳流程")
  })
  it("technical name only: spaced, monospace, never glued to the sentence", () => {
    const n = { primary: "/api/orders/export", business: false }
    expect(html(<ViewFlowLabel n={n} />)).toBe('查看 <code class="tech">/api/orders/export</code> 的流程')
    expect(html(<FlowHeading n={n} />)).toBe('<code class="tech">/api/orders/export</code> 的流程')
  })
})
