import { type Names } from "@/entities/artifact/model"
import { flowTitle } from "./cards"

/** 「查看「結帳」流程」 with a business name; 「查看 <code>/api/x</code> 的流程」 with only a technical name (never glued into the sentence). */
export function ViewFlowLabel({ n }: { n: Names }) {
  return n.business ? <>查看「{n.primary.replace(/流程$/, "")}」流程</> : <>查看 <code className="tech">{n.primary}</code> 的流程</>
}

/** Flow heading: 「結帳流程」, or 「<code>/api/x</code> 的流程」. */
export function FlowHeading({ n }: { n: Names }) {
  return n.business ? <>{flowTitle(n.primary)}</> : <><code className="tech">{n.primary}</code> 的流程</>
}
