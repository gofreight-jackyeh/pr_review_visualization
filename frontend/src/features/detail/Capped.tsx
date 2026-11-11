import type { ReactNode } from "react"
import { useExplorer } from "@/app/store"

/** A list that shows the first `limit` items and a 「顯示全部」 button; the expansion is kept in history (back restores it). */
export function Capped<T>({ id, items, limit = 12, render }: { id: string; items: T[]; limit?: number; render: (t: T) => ReactNode }) {
  const ex = useExplorer()
  const all = ex.expanded.has(`all:${id}`)
  return <>{(all ? items : items.slice(0, limit)).map(render)}{items.length > limit && !all && <button className="link small" onClick={() => ex.toggle(`all:${id}`, true)}>顯示全部 {items.length} 項</button>}</>
}
