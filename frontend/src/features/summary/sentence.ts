/**
 * Split text at the end of its first sentence. CJK terminators (。！？) end a sentence anywhere; ASCII . ! ? only when
 * followed by whitespace or the end (so "v1.2" or "a.b" are not cut). Closing quotes / brackets stay with the sentence.
 * Never cuts mid-sentence: without a terminator the whole text is the first sentence.
 */
export function firstSentence(text: string): { first: string; rest: string } {
  const t = text.trim()
  const re = /[。！？]+[」』）)”’"']*|[.!?]+[」』）)”’"']*(?=\s|$)/g
  const m = re.exec(t)
  if (!m) return { first: t, rest: "" }
  const end = m.index + m[0].length
  return { first: t.slice(0, end), rest: t.slice(end).trim() }
}

/** Texts longer than this are clamped to ~4 lines with an in-place 「顯示全文」. */
export const LONG_TEXT = 160
