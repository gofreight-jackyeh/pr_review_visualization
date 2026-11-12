/**
 * `?fixture=large`: a generated system map + overlay shaped like a large monorepo (spec §4/§13): ~60 capabilities including shared and
 * engineering ones, one capability split into 8 children, ~900 entry points, ~400 relations and a 40-step flow.
 * Deterministic (seeded) so tests can assert the node budgets against it.
 */
import type {
  ArchCheck, ArchRule, Capability, Domain, Entity, EntryPoint, Evidence, Flow, FlowChange, Impact, Interface, OverlayStep, Phase, PrOverlay, Relation, Step, SystemMap, Text,
} from "@/entities/artifact/types"

const PROD = { name: "static-analyzer", version: "2.2.0" }
const MAP_SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c", HEAD_SHA = "9a8b7c6d5e4f30211203f4e5d6c7b8a99a8b7c6d"

function rng(seed: number) {
  let s = seed >>> 0
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 2 ** 32 }
}

const DOMAINS: [string, string, string[]][] = [
  ["ops", "訂單出貨", ["shipment", "checkout", "package", "tracking", "dispatch", "courier", "delivery", "returns", "notification"]],
  ["pricing", "定價", ["cart", "price", "promotion", "coupon"]],
  ["acct", "會計財務", ["finance", "currency", "credit", "budget", "settlement"]],
  ["crm", "客戶管理", ["customer", "contact", "lead", "contract"]],
  ["wms", "倉儲", ["warehouse", "inventory", "picking", "receiving", "location"]],
  ["catalog", "商品目錄", ["product", "category", "brand", "review"]],
  ["docs", "文件", ["receipt", "invoice_doc", "edi", "template"]],
  ["report", "報表", ["dashboard", "kpi", "export"]],
]
const SPLIT_PARENT = "finance"
const SPLIT_CHILDREN = ["ar", "ap", "gl", "invoice", "payment", "tax", "reconcile", "costing"]
const SHARED = ["core", "utils", "permissions", "settings", "logging"]
const ENGINEERING = ["tests", "deploy", "scripts", "docs_site", "ci", "build"]
const EP_KINDS: EntryPoint["kind"][] = ["route", "api", "api", "route", "task", "command", "schedule", "event"]

const T = (text: string, refs: string[]): Text => ({ text, source: "semantic", evidence_refs: refs as Text["evidence_refs"] })
const capId = (name: string) => `cap:${name}`

export function largeFixture(): { map: SystemMap; overlay: PrOverlay } {
  const r = rng(42)
  const evidence: Evidence[] = []
  const evi = (id: string, desc: string, path: string, line = 1, side: "base" | "head" = "base", kind: Evidence["kind"] = "ast") => {
    evidence.push({ id, kind, description: desc, refs: [{ side, path, start_line: line }], producer: PROD })
    return id
  }
  const domains: Domain[] = DOMAINS.map(([k, name], i) => {
    const e = evi(`ev:dom:${k}`, `Claude 歸類：${name}`, `app/${DOMAINS[i][2][0]}/apps.py`)
    return { id: `dom:${k}`, technical_name: k, business_name: T(name, [e]), description: T(`${name}相關的業務能力。`, [e]), evidence_refs: [e], source: "semantic", order: i }
  })
  const caps: Capability[] = []
  const mkCap = (name: string, category: Capability["category"], path: string, domainKey?: string, parent?: string): Capability => {
    const e = evi(`ev:cap:${name}`, `${path} 是一個模組`, `${path}/__init__.py`)
    const c: Capability = {
      id: capId(name), technical_name: path.replace(/\//g, "."), evidence_refs: [e], path, category, language: "python", entity_ids: [], entry_point_ids: [], component_count: 5 + Math.floor(r() * 40),
      ...(category === "business" ? { business_name: T(`${name}`, [e]), domain_id: `dom:${domainKey}` } : {}),
      ...(parent ? { parent_capability_id: capId(parent) } : {}),
    }
    caps.push(c)
    return c
  }
  for (const [k, , names] of DOMAINS) for (const n of names) {
    mkCap(n, "business", `app/${n}`, k)
    if (n === SPLIT_PARENT) for (const ch of SPLIT_CHILDREN) mkCap(`${n}.${ch}`, "business", `app/${n}/${ch}`, k, n)
  }
  for (const n of SHARED) mkCap(n, "shared", `app/${n}`)
  for (const n of ENGINEERING) mkCap(n, "engineering", n === "tests" ? "tests" : n)
  // business names a reader understands (Claude would write these); children get "會計：應收" style names
  const zh: Record<string, string> = {
    shipment: "出貨單", checkout: "結帳", package: "包裹", tracking: "物流追蹤", dispatch: "出貨排程", courier: "物流商", delivery: "配送", returns: "退貨", notification: "通知",
    cart: "購物車", price: "商品價格", promotion: "促銷活動", coupon: "優惠券", finance: "會計", currency: "匯率", credit: "信用額度", budget: "預算", settlement: "結算",
    customer: "客戶", contact: "聯絡人", lead: "潛在客戶", contract: "合約", warehouse: "倉庫", inventory: "庫存", picking: "揀貨", receiving: "收貨", location: "儲位",
    product: "商品", category: "商品分類", brand: "品牌", review: "商品評價", receipt: "收據", invoice_doc: "發票文件", edi: "EDI 交換", template: "文件範本",
    dashboard: "儀表板", kpi: "績效指標", export: "資料匯出",
    "finance.ar": "應收帳款", "finance.ap": "應付帳款", "finance.gl": "總帳", "finance.invoice": "開立發票", "finance.payment": "收付款",
    "finance.tax": "稅務", "finance.reconcile": "對帳", "finance.costing": "成本計算",
  }
  for (const c of caps) if (c.business_name) c.business_name = T(zh[c.id.slice(4)] ?? c.id.slice(4), c.evidence_refs)

  const byName = new Map(caps.map((c) => [c.id, c]))
  const business = caps.filter((c) => c.category === "business")
  const leaves = business.filter((c) => !business.some((x) => x.parent_capability_id === c.id))

  // entities: 3 per business leaf
  const entities: Entity[] = []
  for (const c of leaves) for (let i = 0; i < 3; i++) {
    const id = `ent:${c.id.slice(4)}:${i}`
    entities.push({ id, technical_name: `${c.technical_name}.Model${i}`, evidence_refs: c.evidence_refs, capability_id: c.id, refs: [{ side: "base", path: `${c.path}/models.py`, start_line: 10 + i * 20 }] })
    c.entity_ids.push(id)
  }
  // entry points: ~900 over business leaves (split children are dense, parent keeps a handful), a few commands in engineering
  const entryPoints: EntryPoint[] = []
  const addEp = (c: Capability, n: number) => {
    for (let i = 0; i < n; i++) {
      const id = `ep:${c.id.slice(4)}:${i}`, kind = EP_KINDS[(i + c.id.length) % EP_KINDS.length]
      entryPoints.push({ id, technical_name: kind === "route" || kind === "api" ? `GET /${c.path.slice(4)}/${i}` : `${c.technical_name}.${kind}_${i}`, evidence_refs: c.evidence_refs, capability_id: c.id, kind,
        refs: [{ side: "base", path: `${c.path}/${kind === "route" || kind === "api" ? "urls" : "tasks"}.py`, start_line: 5 + i }] })
      c.entry_point_ids.push(id)
    }
  }
  for (const c of leaves) addEp(c, c.parent_capability_id ? 38 + Math.floor(r() * 10) : 10 + Math.floor(r() * 10))
  addEp(byName.get(capId(SPLIT_PARENT))!, 6)
  for (const n of ["scripts", "deploy"]) addEp(byName.get(capId(n))!, 4)

  // relations: business↔business, business→shared, engineering→business (~400)
  const relations: Relation[] = []
  const seen = new Set<string>()
  const addRel = (s: string, t: string, kind: Relation["kind"], w: number) => {
    const key = `${s}>${t}`
    if (s === t || seen.has(key)) return
    seen.add(key)
    const id = `rel:${s.slice(4)}>${t.slice(4)}`
    relations.push({ id, source: s, target: t, kind, weight: w, confidence: r() < 0.08 ? "unknown" : "confirmed", evidence_refs: [byName.get(s)!.evidence_refs[0]] })
  }
  while (relations.length < 220) {
    const a = business[Math.floor(r() * business.length)], b = business[Math.floor(r() * business.length)]
    const sameDomain = a.domain_id === b.domain_id
    if (!sameDomain && r() < 0.35) continue
    addRel(a.id, b.id, r() < 0.6 ? "calls" : "depends_on", 1 + Math.floor(r() * (sameDomain ? 30 : 12)))
  }
  for (const c of business) for (const s of SHARED) if (r() < 0.55) addRel(c.id, capId(s), "depends_on", 5 + Math.floor(r() * 60))
  for (const c of business) if (r() < 0.75) addRel(capId("tests"), c.id, "depends_on", 1 + Math.floor(r() * 20))
  for (const c of business.slice(0, 12)) addRel(capId(ENGINEERING[1 + (c.id.length % 5)]), c.id, "depends_on", 1)
  addRel(capId("shipment"), capId("finance.invoice"), "calls", 14)
  addRel(capId("shipment"), capId("checkout"), "calls", 22)
  addRel(capId("cart"), capId("shipment"), "calls", 9)

  // flows: a 40-step flow (no phases → capability runs), a phased flow, and a set of short flows
  const evFlow = (id: string, path: string) => evi(`ev:flow:${id}`, `${id} 的呼叫路徑`, path, 12)
  const step = (fid: string, i: number, cap: string, kind: Step["kind"] = "call", status: Step["evidence_status"] = "confirmed"): Step => ({
    id: `step:${fid}:${i}`, order: i, kind, capability_id: cap, component_ids: [], technical_label: `${cap.slice(4)}.step_${i}()`,
    business_label: T(`${zh[cap.slice(4)] ?? cap.slice(4)}：處理第 ${i + 1} 步`, [`ev:flow:${fid}`]), evidence_status: status, evidence_refs: [`ev:flow:${fid}`],
  })
  const bigCaps = ["shipment", "shipment", "permissions", "checkout", "checkout", "package", "logging", "courier", "courier", "dispatch", "delivery", "utils",
    "returns", "notification", "notification", "tracking", "core", "cart", "price", "price", "coupon", "currency", "credit", "finance.ar", "finance.invoice",
    "finance.invoice", "finance.invoice", "finance.tax", "finance.gl", "finance.gl", "logging", "settlement", "receipt", "receipt", "edi", "invoice_doc",
    "customer", "contact", "kpi", "dashboard"].map(capId)
  const flows: Flow[] = []
  const mkFlow = (fid: string, name: string, epCap: Capability, capsSeq: string[], phases?: { label: string; n: number }[]): Flow => {
    const e = evFlow(fid, `${epCap.path}/views.py`)
    const steps = capsSeq.map((c, i) => step(fid, i, c, i === 0 ? "enter" : i % 7 === 3 ? "write" : "call", i % 11 === 5 ? "possible" : "confirmed"))
    const f: Flow = { id: `flow:${fid}`, technical_name: fid, business_name: T(name, [e]), evidence_refs: [e], entry_point_id: epCap.entry_point_ids[0],
      capability_ids: [...new Set(capsSeq)], entity_ids: [], timeline: { mode: "animation", steps } }
    if (phases) {
      let k = 0
      f.phases = phases.map((p, i) => ({ id: `ph:${fid}:${i}`, label: T(p.label, [e]), step_ids: steps.slice(k, (k += p.n)).map((s) => s.id) as Phase["step_ids"] }))
    }
    flows.push(f)
    return f
  }
  const ship = byName.get(capId("shipment"))!
  mkFlow("ship_to_invoice", "出貨到開立發票", ship, bigCaps)
  mkFlow("cart_to_checkout", "購物車到結帳", byName.get(capId("cart"))!,
    ["cart", "price", "permissions", "promotion", "coupon", "cart", "customer", "credit", "checkout", "checkout", "logging", "courier", "delivery", "checkout"].map(capId),
    [{ label: "建立購物車", n: 4 }, { label: "計算價格", n: 2 }, { label: "檢查客戶信用", n: 3 }, { label: "確認結帳", n: 5 }])
  for (const c of leaves.slice(0, 18)) {
    if (c.id === ship.id) continue
    const peers = relations.filter((x) => x.source === c.id && byName.get(x.target)?.category === "business").slice(0, 3).map((x) => x.target)
    mkFlow(`f_${c.id.slice(4)}`, `${zh[c.id.slice(4)] ?? c.id}主流程`, c, [c.id, c.id, ...peers])
  }

  const rules: ArchRule[] = [
    { id: "rule:il:acct-independence", origin: "documented", kind: "independence", title: "會計子模組互相獨立",
      description: "import-linter 契約 finance-independence：finance.* 之間只能透過 finance.api 互相呼叫。",
      source_ref: { side: "base", path: ".importlinter.ini", start_line: 40, end_line: 58 }, scope_capability_ids: SPLIT_CHILDREN.map((c) => capId(`${SPLIT_PARENT}.${c}`)),
      exception_count: 14, evidence_refs: [evi("ev:rule:il", ".importlinter.ini contracts", ".importlinter.ini", 1, "base", "doc")] },
    { id: "rule:il:ops-no-gl", origin: "documented", kind: "forbidden", title: "出貨作業不可直接寫總帳",
      description: "import-linter 契約 ops-forbidden-gl：app.shipment 等出貨作業模組不可 import app.finance.gl。",
      source_ref: { side: "base", path: ".importlinter.ini", start_line: 61, end_line: 70 }, scope_capability_ids: [capId("shipment"), capId("finance.gl")], exception_count: 2, evidence_refs: ["ev:rule:il"] },
    { id: "rule:inf:invoice-api", origin: "inferred", kind: "interface_only", title: "開立發票一律透過 finance.invoice.services",
      description: "其他能力開立發票時都呼叫 finance.invoice.services。", statistic: "41 次跨能力呼叫中有 39 次透過 finance.invoice.services",
      scope_capability_ids: [capId("finance.invoice")], evidence_refs: [evi("ev:rule:inf:invoice", "跨能力呼叫統計", "app/finance/invoice/services.py", 1, "base", "import")] },
  ]

  // interfaces (spec §8): outer = client-facing, middle = between capabilities
  const mkIf = (id: string, tech: string, biz: string, kind: Interface["kind"], ring: Interface["ring"], cap: string, path: string, line: number, side: "base" | "head" = "base", detected_by: Interface["detected_by"] = "static"): Interface => {
    const e = evi(`ev:if:${id}`, `${tech} 是${ring === "outer" ? "對外" : "能力之間"}的介面`, path, line, side)
    return { id: `if:${id}`, technical_name: tech, business_name: T(biz, [e]), evidence_refs: [e], ring, kind, capability_id: capId(cap), detected_by, refs: [{ side, path, start_line: line }] }
  }
  const interfaces: Interface[] = [
    mkIf("shipment-complete", "POST /api/shipments/<id>/complete", "完成出貨", "http_api", "outer", "shipment", "app/shipment/urls.py", 18),
    mkIf("shipment-web", "/shipments/:id (web app route)", "出貨單頁面", "web_route", "outer", "shipment", "web/src/routes/shipment.tsx", 9, "base", "config"),
    mkIf("invoice-pdf", "invoice.pdf (outbound)", "發票 PDF 格式", "outbound_format", "outer", "finance.invoice", "app/finance/invoice/pdf.py", 30),
    mkIf("invoice-issue", "finance.invoice.services.issue_invoice", "開立發票介面", "module_api", "middle", "finance.invoice", "app/finance/invoice/services.py", 44),
    mkIf("checkout-event", "checkout.events.CheckoutCompleted", "結帳完成事件", "event", "middle", "checkout", "app/checkout/events.py", 5),
  ]
  const headInterface = mkIf("shipment-webhook", "POST /api/shipments/webhook", "出貨狀態回呼", "http_api", "outer", "shipment", "app/shipment/urls.py", 40, "head")

  const map: SystemMap = {
    kind: "system_map", schema_version: "2.4.0", analyzer_version: "2.2.0-fixture", system_map_id: "sm_large", generated_at: "2026-11-11T09:00:00Z",
    repo: { owner: "acme", name: "bigapp" }, commit_sha: MAP_SHA,
    system: { technical_name: "bigapp", summary: T("電商營運系統：從購物車、結帳、出貨到開立發票與對帳。", [evi("ev:system", "README", "README.md", 1, "base", "doc")]) },
    coverage: [{ language: "python", level: "deep", file_count: 5400 }], domains, capabilities: caps, entities, entry_points: entryPoints, components: [], relations, flows,
    flow_budget: { entry_points_total: entryPoints.length, flows_built: flows.length }, architecture_rules: rules, interfaces, evidence,
    semantic: { status: "done", tier: "standard" }, disclaimer: "流程由靜態分析重建，不是實際執行紀錄。",
  }

  // ---- overlay: the PR changes shipment → invoice (and adds a forbidden shipment → gl dependency)
  const oev: Evidence[] = []
  const oe = (id: string, path: string, line: number, desc = "diff") => { oev.push({ id, kind: "diff", description: desc, refs: [{ side: "head", path, start_line: line }], producer: PROD }); return id }
  const dShip = oe("ev:diff:shipment", "app/shipment/services.py", 120), dInv = oe("ev:diff:invoice", "app/finance/invoice/services.py", 44)
  const dGl = oe("ev:diff:gl", "app/shipment/services.py", 7, "新增 import app.finance.gl")
  const big = flows[0]
  const bigSteps: OverlayStep[] = []
  big.timeline.steps.forEach((s, i) => {
    if (i === 25) bigSteps.push({ ...s, id: `${s.id}:removed`, step_change_status: "removed", evidence_refs: [dInv] })
    bigSteps.push({ ...s, step_change_status: i === 24 || i === 26 ? "changed" : "unchanged" })
    if (i === 24) bigSteps.push({ ...s, id: `${s.id}b`, technical_label: "finance.gl.post_entry()", capability_id: capId("finance.gl"), step_change_status: "added", evidence_refs: [dGl], business_label: T("直接寫入總帳分錄", [dGl]) })
  })
  bigSteps.forEach((s, i) => { s.order = i })
  const phased = flows[1]
  const fcs: FlowChange[] = [
    { flow_id: big.id, change_status: "modified", mode: "animation", steps: bigSteps, evidence_refs: [dShip] },
    { flow_id: phased.id, change_status: "modified", mode: "animation", evidence_refs: [dShip],
      steps: phased.timeline.steps.map((s, i) => ({ ...s, step_change_status: i === 9 ? "changed" : "unchanged" })), phases: phased.phases },
  ]
  const impacts: Impact[] = [
    { target_id: capId("shipment"), target_kind: "capability", change_status: "modified", impact_status: "direct", evidence_refs: [dShip] },
    { target_id: capId("finance.invoice"), target_kind: "capability", change_status: "modified", impact_status: "direct", evidence_refs: [dInv] },
    { target_id: capId("finance.gl"), target_kind: "capability", change_status: "unchanged", impact_status: "confirmed_dependency", evidence_refs: [dGl] },
    { target_id: capId("checkout"), target_kind: "capability", change_status: "unchanged", impact_status: "potential", evidence_refs: [dShip] },
    { target_id: big.id, target_kind: "flow", change_status: "modified", impact_status: "direct", evidence_refs: [dShip] },
    { target_id: phased.id, target_kind: "flow", change_status: "modified", impact_status: "direct", evidence_refs: [dShip] },
  ]
  const checks: ArchCheck[] = [
    { id: "ac:ops-gl", rule_id: "rule:il:ops-no-gl", origin: "documented", source: "static", check: "dependency_direction", ring: "middle", verdict: "violates", status: "verified",
      title: "出貨單直接寫總帳", description: "app/shipment/services.py 新增 import app.finance.gl，違反 ops-forbidden-gl。", capability_ids: [capId("shipment"), capId("finance.gl")],
      violating_relation: { source: capId("shipment"), target: capId("finance.gl") }, pr_refs: [{ side: "head", path: "app/shipment/services.py", start_line: 7 }],
      existing_refs: [{ side: "base", path: "app/finance/invoice/services.py", start_line: 80 }], evidence_refs: [dGl] },
    { id: "ac:invoice-api", rule_id: "rule:inf:invoice-api", origin: "inferred", source: "static", check: "interface", ring: "middle", verdict: "follows", status: "verified",
      title: "開立發票沿用對外介面", description: "PR 透過 finance.invoice.services 開立發票。", capability_ids: [capId("finance.invoice")],
      pr_refs: [{ side: "head", path: "app/shipment/services.py", start_line: 120 }], existing_refs: [{ side: "base", path: "app/checkout/services.py", start_line: 33 }], evidence_refs: [dInv] },
  ]
  const ifc = (id: string, ring: ArchCheck["ring"], ifid: string, cap: string, change: "added" | "modified" | "removed", compat: "compatible" | "breaking" | "unknown", title: string, detail: string,
    path: string, line: number, ev: string, status: ArchCheck["status"] = "verified"): ArchCheck => ({
    id, origin: "observed", source: "static", check: "interface_change", ring, interface_change: { interface_id: ifid, change, compatibility: compat, detail },
    verdict: compat === "breaking" ? "violates" : compat === "unknown" ? "unclear" : "follows", status, title, description: detail, capability_ids: [capId(cap)],
    pr_refs: [{ side: "head", path, start_line: line }], existing_refs: [], evidence_refs: [ev] as ArchCheck["evidence_refs"],
  })
  checks.unshift(
    ifc("ac:if-complete", "outer", "if:shipment-complete", "shipment", "modified", "breaking", "完成出貨 API 新增必填欄位", "請求新增必填欄位 invoice_mode；舊客戶端不送會被拒絕。", "app/shipment/views.py", 61, dShip),
    ifc("ac:if-pdf", "outer", "if:invoice-pdf", "finance.invoice", "modified", "unknown", "發票 PDF 內容有改動", "PDF 版面程式有改動；下游是否依賴原格式無法判定。", "app/finance/invoice/pdf.py", 33, dInv, "needs_confirmation"),
    ifc("ac:if-webhook", "outer", "if:shipment-webhook", "shipment", "added", "compatible", "新增出貨狀態回呼 API", "新增路由 POST /api/shipments/webhook。", "app/shipment/urls.py", 40, dShip),
    ifc("ac:if-issue", "middle", "if:invoice-issue", "finance.invoice", "modified", "compatible", "開立發票介面新增選填參數", "issue_invoice() 新增選填參數 auto=False。", "app/finance/invoice/services.py", 44, dInv),
  )
  const overlay: PrOverlay = {
    kind: "pr_overlay", schema_version: "2.4.0", analyzer_version: "2.2.0-fixture", overlay_id: "ov_large", system_map_id: "sm_large", generated_at: "2026-11-11T09:05:00Z",
    pr: { source: "github", url: "https://github.com/acme/bigapp/pull/4321", number: 4321, title: "出貨完成時自動開立發票", body: "出貨完成時直接開立發票並寫總帳。", author: "dev",
      repo: { owner: "acme", name: "bigapp" }, base_ref: "main", head_ref: "feat/auto-invoice", base_sha: MAP_SHA, head_sha: HEAD_SHA, merge_base_sha: MAP_SHA },
    coverage: map.coverage,
    changes: { files: [
      { id: "cf:ship", path: "app/shipment/services.py", status: "modified", language: "python", additions: 60, deletions: 4, capability_id: capId("shipment") },
      { id: "cf:inv", path: "app/finance/invoice/services.py", status: "modified", language: "python", additions: 22, deletions: 3, capability_id: capId("finance.invoice") },
    ] },
    purpose: T("出貨完成時自動開立發票，減少人工開票。", [dShip]),
    behavior_changes: [{ id: "bc:auto-invoice", before: T("出貨完成後由會計手動開立發票。", [dShip]), after: T("出貨完成時系統自動開立發票並寫入總帳。", [dInv]),
      flow_ids: [big.id], capability_ids: [capId("shipment"), capId("finance.invoice")] }],
    impacts, flow_changes: fcs, head_additions: { capabilities: [], entities: [], entry_points: [], components: [],
      interfaces: [headInterface], relations: [{ id: "rel:shipment>finance.gl:new", source: capId("shipment"), target: capId("finance.gl"), kind: "calls", weight: 1, confidence: "confirmed", evidence_refs: [dGl] }], flows: [] },
    findings: [{ id: "fd:double-invoice", source: "static", title: "重送出貨完成事件會重複開票", description: "開票前沒有檢查是否已開立。", severity: "warning", status: "verified",
      changed_file_ids: ["cf:ship"], capability_ids: [capId("shipment"), capId("finance.invoice")], flow_ids: [big.id], refs: [{ side: "head", path: "app/shipment/services.py", start_line: 130 }], evidence_refs: [dShip] }],
    architecture_checks: checks, evidence: oev, semantic: { status: "done", tier: "standard" },
    walkthrough: { steps: [
      { id: "wt:1", title: "這個系統是做什麼的", body: map.system.summary!, target: { view: "system" } },
      { id: "wt:2", title: "這個 PR 改變了什麼業務行為", body: T("出貨完成時自動開立發票。", [dShip]), target: { view: "overlay", capability_ids: [capId("shipment"), capId("finance.invoice")] } },
      { id: "wt:3", title: "架構一致性", body: T("PR 讓出貨單直接寫總帳，違反文件規則。", [dGl]), target: { view: "architecture" } },
    ], questions: [] },
  }
  return { map, overlay }
}
