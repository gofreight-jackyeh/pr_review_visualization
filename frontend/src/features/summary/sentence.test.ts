import { describe, expect, it } from "vitest"
import { firstSentence } from "./sentence"

describe("firstSentence", () => {
  it("cuts at the first CJK terminator and keeps the rest", () => {
    expect(firstSentence("這是一個商店後端。負責結帳與付款。")).toEqual({ first: "這是一個商店後端。", rest: "負責結帳與付款。" })
    expect(firstSentence("能用嗎？可以！")).toEqual({ first: "能用嗎？", rest: "可以！" })
    expect(firstSentence("他說「好。」然後離開。").first).toBe("他說「好。」")
  })
  it("ASCII terminators only end a sentence before whitespace / end; never cuts mid-sentence", () => {
    expect(firstSentence("Handles checkout v1.2 flows. Sends mail.")).toEqual({ first: "Handles checkout v1.2 flows.", rest: "Sends mail." })
    expect(firstSentence("沒有句號的長描述 a.b.c").first).toBe("沒有句號的長描述 a.b.c")
    expect(firstSentence("單句。")).toEqual({ first: "單句。", rest: "" })
  })
})
