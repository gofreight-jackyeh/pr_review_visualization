import { nameOfId, refsOf, type Model } from "@/entities/artifact/model"
import { FileRow, RefRow } from "@/shared/ui/ui"
import { STEP_COUNT_LABEL, staticGroups } from "./cards"

/**
 * A changed flow without business texts: what is concrete instead of sentences made of code identifiers —
 * per capability, the changed steps with their code location and the capability's changed files, each linked to the PR diff.
 */
export function StaticGroups({ m, flowId }: { m: Model; flowId: string }) {
  const groups = staticGroups(m, flowId)
  if (!groups.length) return null
  return (
    <div className="static-groups" data-testid={`static-${flowId}`}>
      {groups.map((g) => (
        <div key={g.capId} className="sg">
          <h5>{nameOfId(m, g.capId).primary}</h5>
          <ul className="plain sg-steps">{g.steps.map((s) => {
            const st = s.step_change_status as keyof typeof STEP_COUNT_LABEL
            const r = refsOf(m, s.evidence_refs)[0]
            return (
              <li key={s.id}><span className={"step-count sc-" + st}>{STEP_COUNT_LABEL[st]}</span> <code className="tech">{s.technical_label}</code>
                {r && <RefRow r={r} ov={m.overlay} />}</li>
            )
          })}</ul>
          {g.files.length > 0 && <ul className="plain files">{g.files.map((f) => <FileRow key={f.id} f={f} ov={m.overlay} />)}</ul>}
        </div>
      ))}
    </div>
  )
}
