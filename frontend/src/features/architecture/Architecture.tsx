import type { ReactNode } from "react"
import { nameOfId, namesOf } from "@/entities/artifact/model"
import { statusOf } from "@/entities/artifact/provenance"
import type { ArchCheck } from "@/entities/artifact/types"
import { useExplorer } from "@/app/store"
import { Chip } from "@/shared/ui/ui"
import { ReviewCard } from "@/features/review/ReviewCard"
import { relationSentence } from "@/features/relations/wording"
import { ARCH_GROUPS, groupChecks, ORIGIN_LABEL, archGroupOf } from "./arch"
import { splitDetail, CHANGE_LABEL, CHECKLIST, COMPAT, interfaceChanges, groupByRing, KIND_LABEL, needsChecklist, RING } from "./interfaces"

const CHECK_LABEL: Record<ArchCheck["check"], string> = {
  interface_change: "對外介面變動", dependency_direction: "依賴方向", interface: "對外介面", placement: "放置位置", shared_mechanism: "共用機制", new_debt: "架構債", cycle: "循環依賴",
}

export function RingChip({ ring }: { ring: ArchCheck["ring"] }) {
  const r = RING[ring]
  return <span className="chip-tag ring-chip" style={{ background: r.bg, color: r.fg, borderColor: r.color }} data-ring={ring}>{r.label}</span>
}

/** What to confirm about an interface change: the spec §8 checklist for outer modified / removed, else by compatibility. */
function interfaceConfirm(c: ArchCheck): string {
  if (needsChecklist(c)) return CHECKLIST.replace(/^需要確認：/, "")
  const compat = c.interface_change!.compatibility
  return compat === "breaking" ? "這個改動可能破壞呼叫端：是否需要同步修改呼叫端或版本控管？"
    : compat === "unknown" ? "相容性無法由靜態分析判定：這個改動是否影響使用這個介面的地方？"
    : "新增或相容的介面：命名、權限與既有介面是否一致？"
}

/** One interface change (spec §8) as a review card: ring, name, change, compatibility, detail, PR location. */
export function InterfaceCard({ c, embedded }: { c: ArchCheck; embedded?: boolean }) {
  const ex = useExplorer()
  const m = ex.model
  const ch = c.interface_change!
  const iface = m.ifaceById.get(ch.interface_id)
  const name = iface ? namesOf(iface) : { primary: c.title, secondary: undefined, business: false }
  const compat = COMPAT[ch.compatibility]
  const capId = iface?.capability_id ?? c.capability_ids[0]
  const st = statusOf({ arch: c })
  const detail = splitDetail(ch.detail)
  return (
    <ReviewCard id={c.id} testid={embedded ? `panel-arch-${c.id}` : `arch-${c.id}`} embedded={embedded} className="arch iface" attrs={{ "data-ring": c.ring, "data-compat": ch.compatibility, "data-change": ch.change }}
      status={st} source={c.source === "semantic" ? "semantic" : "static"} confirm={<span data-testid={needsChecklist(c) ? `checklist-${c.id}` : undefined}>{interfaceConfirm(c)}</span>}
      title={<><RingChip ring={c.ring} />{" "}
        <button className="link iface-name" data-testid={`iface-focus-${c.id}`} title="在專案背景查看這個功能" onClick={() => capId && ex.focusCapability(capId)}><b>{name.primary}</b></button>
        {name.secondary && <code className="tech">{name.secondary}</code>}</>}
      caps={capId ? [capId] : []}
      facts={[
        ...(detail.facts.length ? detail.facts : [""]).map((d) => <>{iface ? KIND_LABEL[iface.kind] : "介面"}{CHANGE_LABEL[ch.change]}{d && `：${d}`}</>),
        ...(ch.compatibility !== "unknown" ? [<>相容性：<Chip tone={compat.tone}>{ch.compatibility === "breaking" ? "⚠ " : "✓ "}{compat.label}</Chip></>] : []),
      ]}
      uncertain={[
        ...(ch.compatibility === "unknown" ? [<>相容性：<Chip tone={compat.tone}>– {compat.label}</Chip>{detail.uncertain.length > 0 && `（${detail.uncertain.join("；")}）`}</>] : []),
        ...(ch.compatibility !== "unknown" ? detail.uncertain.map((u) => <>{u}</>) : []),
        ...(c.business_impact ? [<>業務影響（推論，需確認）：{c.business_impact}</>] : []),
      ]}
      refs={[{ label: "PR 位置", refs: c.pr_refs }]} />
  )
}

/** A dependency-rule check (spec §8) as a review card: rule, its source, the PR change and the established way. */
export function ArchCard({ c, embedded }: { c: ArchCheck; embedded?: boolean }) {
  const ex = useExplorer()
  if (c.check === "interface_change" && c.interface_change) return <InterfaceCard c={c} embedded={embedded} />
  const m = ex.model
  const rule = c.rule_id ? m.rules.get(c.rule_id) : undefined
  const g = ARCH_GROUPS.find((x) => x.key === archGroupOf(c))!
  const v = c.violating_relation
  const st = statusOf({ arch: c })
  const facts: ReactNode[] = [<>判定：<Chip tone={g.tone}>{g.label}</Chip>　{ORIGIN_LABEL[c.origin]} · {CHECK_LABEL[c.check]}{rule ? <> · 規則：<b>{rule.title}</b></> : null}</>]
  const uncertain: ReactNode[] = []
  ;(st === "confirmed" ? facts : uncertain).push(c.description)
  if (rule?.statistic) facts.push(<>統計：{rule.statistic}</>)
  if (rule?.exception_count) facts.push(<>這條規則目前已有 {rule.exception_count} 個例外。</>)
  if (v) facts.push(<>違反的依賴：{relationSentence(nameOfId(m, v.source).primary, nameOfId(m, v.target).primary, ["depends_on"])}{" "}
    <button className="mini" onClick={() => ex.showArch(c.id)} data-testid={`arch-show-${c.id}`}>在地圖上標出</button></>)
  if (c.origin === "hypothesis") uncertain.push("AI 提出的假設，尚無文件規則或系統統計支持。")
  if (c.business_impact) uncertain.push(<>業務影響（推論，需確認）：{c.business_impact}</>)
  return (
    <ReviewCard id={c.id} testid={embedded ? `panel-arch-${c.id}` : `arch-${c.id}`} embedded={embedded} className="arch" attrs={{ "data-group": g.key }} status={st} source={c.source === "semantic" ? "semantic" : "static"}
      confirm={c.title} caps={c.capability_ids} facts={facts} uncertain={uncertain}
      refs={[{ label: "規則出處", refs: rule?.source_ref ? [rule.source_ref] : [] }, { label: "這次 PR 的改動", refs: c.pr_refs }, { label: "既有的正確做法", refs: c.existing_refs }]} />
  )
}

/** 對外介面變動 section: grouped 外圈 → 中圈 → 內圈 (spec §8 呈現順序). */
export function InterfaceChanges() {
  const { model: m } = useExplorer()
  const list = interfaceChanges(m.archChecks)
  return (
    <section data-testid="iface-section">
      {list.length === 0 && <p className="empty-result">本次分析未發現外圈或中圈介面的變動。</p>}
      {groupByRing(list).map(([ring, items]) => (
        <div key={ring} data-testid={`iface-ring-${ring}`}>
          <h5><RingChip ring={ring} /> {items.length} 項</h5>
          {items.map((c) => <InterfaceCard key={c.id} c={c} />)}
        </div>
      ))}
    </section>
  )
}

/** Dependency-rule checks (middle / inner ring), grouped by verdict; 符合 collapses behind an expand button. */
export function DependencyChecks() {
  const ex = useExplorer()
  const m = ex.model
  const groups = groupChecks(m.archChecks)
  const open = ex.expanded.has("arch-follows")
  return (
    <div className="arch-block" data-testid="arch-block">
      {ARCH_GROUPS.map((g) => {
        const list = groups[g.key]
        if (!list.length) return null
        const hide = g.key === "follows" && !open
        return (
          <section key={g.key} data-testid={`arch-group-${g.key}`}>
            <h4><Chip tone={g.tone}>{g.label}</Chip> {list.length} 項
              {g.key === "follows" && <button className="link small" aria-expanded={open} onClick={() => ex.toggle("arch-follows")}> {open ? "收合" : "展開"}</button>}</h4>
            {!hide && list.map((c) => <ArchCard key={c.id} c={c} />)}
          </section>
        )
      })}
    </div>
  )
}
