import type { PrOverlay, SystemMap } from "@/entities/artifact/types"
import type { AnalysisStatus } from "@/shared/api/client"
import map from "./sample-system-map.json"
import overlay from "./sample-overlay.json"
import mapNoSem from "./sample-system-map-nosemantic.json"
import overlayNoSem from "./sample-overlay-nosemantic.json"
import { largeFixture } from "./large"

export interface FixturePair { map: SystemMap; overlay: PrOverlay }
export const sample: FixturePair = { map: map as unknown as SystemMap, overlay: overlay as unknown as PrOverlay }
export const sampleNoSemantic: FixturePair = { map: mapNoSem as unknown as SystemMap, overlay: overlayNoSem as unknown as PrOverlay }
let largeCache: FixturePair | null = null
export const large = (): FixturePair => (largeCache ??= largeFixture())

const derive = (base: FixturePair, f: (p: FixturePair) => void): FixturePair => { const p = structuredClone(base); f(p); return p }

/** Analysis-state variants of the sample PR (plan「分析狀態與可用路徑」), derived so they stay in step with the sample. */
/** 業務說明產生中: the static documents of the sample analysis, stored before Claude finished. */
export const pending = (): FixturePair => derive(sampleNoSemantic, ({ map: m, overlay: o }) => {
  m.system_map_id = sample.map.system_map_id
  o.overlay_id = sample.overlay.overlay_id
  o.system_map_id = sample.map.system_map_id
  m.semantic = { status: "pending", tier: "standard" }
  o.semantic = { status: "pending", tier: "standard" }
})
/** 局部失敗: the cached system map has its business texts, but Claude failed on this PR's overlay. */
export const partial = (): FixturePair => ({
  map: sample.map,
  overlay: derive(sampleNoSemantic, ({ overlay: o }) => {
    o.overlay_id = "ov_sample_partial"
    o.system_map_id = sample.map.system_map_id
    o.semantic = { status: "failed", tier: "standard", error: "Claude 呼叫逾時（120 秒）" }
  }).overlay,
})
/** 本次分析未發現 / 沒有可辨識流程: a CI and shared-tooling change with no flow, finding or interface change. */
export const empty = (): FixturePair => ({
  map: sample.map,
  overlay: derive(sample, ({ overlay: o }) => {
    o.overlay_id = "ov_sample_empty"
    o.pr = { ...o.pr, number: 483, title: "Cache dependencies in CI", body: "CI 改用相依套件快取，並調整共用的設定讀取工具。", head_ref: "chore/ci-cache" }
    o.purpose = { text: "Cache dependencies in CI。CI 改用相依套件快取，並調整共用的設定讀取工具。", source: "pr_description", evidence_refs: ["ev:pr:meta"] }
    delete o.design_rationale
    o.changes.files = [
      { id: "f:ci", path: ".github/workflows/ci.yml", status: "modified", language: "yaml", additions: 6, deletions: 2, capability_id: null },
      { id: "f:core-settings", path: "app/core/settings_loader.py", status: "modified", language: "python", additions: 4, deletions: 1, capability_id: "cap:core" },
    ]
    o.behavior_changes = []
    o.impacts = [{ target_id: "cap:core", target_kind: "capability", change_status: "modified", impact_status: "direct", impact_depth: 0, evidence_refs: ["ev:pr:meta"] }]
    o.flow_changes = []
    o.head_additions = { capabilities: [], entities: [], entry_points: [], components: [], relations: [], flows: [], interfaces: [] }
    o.findings = []
    o.architecture_checks = []
    o.walkthrough = { steps: o.walkthrough.steps.slice(0, 1), questions: [] }
    o.evidence = o.evidence.filter((e) => e.id === "ev:pr:meta")
  }).overlay,
})

/** 基礎設施 repo: a Terraform / Helm / Kubernetes PR (system.repo_kind = infrastructure): no flows, files by module. */
export const infra = (): FixturePair => derive(sample, ({ map: m, overlay: o }) => {
  m.system.repo_kind = "infrastructure"
  o.pr = { ...o.pr, number: 512, title: "Tighten VPC egress and bump API replicas", head_ref: "infra/egress" }
  const f = (id: string, path: string, language: string, additions: number, deletions: number) =>
    ({ id, path, status: "modified" as const, language, additions, deletions, capability_id: null })
  o.changes.files = [
    f("f:vpc-main", "modules/vpc/main.tf", "unknown", 12, 3), f("f:vpc-vars", "modules/vpc/variables.tf", "unknown", 4, 0),
    f("f:prod", "envs/prod/main.tf", "unknown", 2, 2), f("f:chart-deploy", "charts/api/templates/deployment.yaml", "yaml", 3, 1),
    f("f:chart-values", "charts/api/values.yaml", "yaml", 1, 1), f("f:ingress", "k8s/base/ingress.yaml", "yaml", 5, 0),
    f("f:readme", "README.md", "markdown", 2, 0),
  ]
  o.behavior_changes = []
  o.flow_changes = []
})

/** A real-size repo: multi-sentence system summary and very long before / after texts (the first screen must stay compact). */
export const longtext = (): FixturePair => derive(sample, ({ map: m, overlay: o }) => {
  m.system.summary!.text = "這是一個線上商店後端，負責結帳與付款。" + "它也處理庫存、通知、報表與排程，並與多個外部系統整合。".repeat(6)
  const filler = "訂單金額會依折扣、稅額與運費重新計算，並同步更新庫存與通知。".repeat(10)
  o.behavior_changes[0].before = { ...o.behavior_changes[0].before!, text: "修改前：" + filler }
  o.behavior_changes[0].after = { ...o.behavior_changes[0].after, text: "修改後：" + filler }
})

/** 完全失敗: the status an analysis reports when it could not produce any document. */
export const FAILED_ANALYSIS: AnalysisStatus = {
  analysis_id: "fixture-failed", status: "failed", overlay_id: null, system_map_id: null,
  error: "無法取得 PR：gh 回傳 404（PR 不存在，或目前的 GitHub 帳號沒有讀取權限）",
  stages: [{ name: "acquire", status: "failed", message: "gh pr view 失敗" }, { name: "system_map", status: "skipped", message: "" }],
}
export const FAILED_INPUT = { pr_url: "https://github.com/acme/shop/pull/999" }

/** `?fixture=<name>` opens these without a backend (`failed` opens the input page with a failed analysis). Generated lazily. */
export const FIXTURES: Record<string, () => FixturePair> = {
  sample: () => sample, "sample-nosemantic": () => sampleNoSemantic, large, pending, partial, empty, longtext, infra,
}
