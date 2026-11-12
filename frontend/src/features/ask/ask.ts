import { useEffect, useState } from "react"
import { nameOfId, type Model } from "@/entities/artifact/model"
import { api } from "@/shared/api/client"
import type { Selection } from "@/app/store"
import { selectionTitle } from "@/features/detail/DetailPanel"

/**
 * Whether this client may ask (server PR_EXPLORER_ASK + its address): hidden = no entry at all (mode off, no overlay,
 * or a backend without the feature); denied = history readable, input replaced by the reason; fixture = sample data.
 */
export type AskAccess = { state: "loading" | "hidden" | "fixture" | "ok" } | { state: "denied"; mode: string }

export function useAskAccess(model: Model, fixture: boolean): AskAccess {
  const [access, setAccess] = useState<AskAccess>(() => (!model.overlay ? { state: "hidden" } : fixture ? { state: "fixture" } : { state: "loading" }))
  useEffect(() => {
    if (!model.overlay || fixture) return
    let live = true
    api.getConfig().then((c) => {
      if (!live) return
      const a = c?.ask
      setAccess(!a || a.mode === "off" ? { state: "hidden" } : a.allowed ? { state: "ok" } : { state: "denied", mode: a.mode })
    }, () => { if (live) setAccess({ state: "hidden" }) })
    return () => { live = false }
  }, [model.overlay, fixture])
  return access
}

const kindOfId = (m: Model, id: string): Selection["kind"] | null => {
  if (m.overlay?.findings.some((f) => f.id === id)) return "finding"
  if (m.archChecks.some((a) => a.id === id)) return "arch"
  if (m.domains.some((d) => d.id === id)) return "domain"
  if (id.startsWith("step:")) return "step"
  return m.kindOf.get(id) ?? null
}

/** Readable title of an item id the reviewer can ask about (selection, change card, changed file). */
export function contextTitle(m: Model, id: string): string {
  const k = kindOfId(m, id)
  if (k) return selectionTitle(m, { kind: k, id })
  const bc = m.overlay?.behavior_changes.find((b) => b.id === id)
  if (bc) { const t = bc.after.text; return t.length > 40 ? t.slice(0, 39) + "…" : t }
  const f = m.overlay?.changes.files.find((x) => x.id === id)
  return f ? f.path : nameOfId(m, id).primary
}

/** What 「關於」 refers to: the item in the detail panel, else the change card being read. */
export function askContext(m: Model, selection: Selection | null, card: string | null): { id: string; title: string } | null {
  if (selection) return { id: selection.id, title: contextTitle(m, selection.id) }
  if (!card) return null
  const id = card.startsWith("fc:") ? card.slice(3) : card.startsWith("tech:") ? card.slice(5) : card
  if (id === "none") return null
  return { id, title: contextTitle(m, id) }
}

/** A cite the detail panel can show (one layer: it replaces the ask panel). */
export function citeSelection(m: Model, kind: string, id: string): Selection | null {
  if (kind === "finding" || kind === "arch" || kind === "step") return { kind, id }
  const k = m.kindOf.get(id)
  return k ? { kind: k, id } : null
}

export const CITE_KIND: Record<string, string> = {
  file: "變更檔案", evidence: "證據", flow: "流程", step: "流程步驟", capability: "功能", entity: "核心資料", entry_point: "入口",
  component: "技術元件", interface: "介面", finding: "待確認事項", arch: "審查重點", behavior: "行為改變", pr: "PR 描述",
}
