import type { Model } from "@/entities/artifact/model"
import type { ChangedFile } from "@/entities/artifact/types"

/** Infrastructure-as-code repository (backend repo_kind.py: no entry points, mostly Terraform / Helm / Kubernetes files):
 * the map and flows are empty by construction, so the workspace leads with the summary, review focus and changed files. */
export const isInfraRepo = (m: Model) => m.map.system.repo_kind === "infrastructure"

export type FileGroupKind = "terraform" | "helm" | "kubernetes" | "other"
export interface FileGroup { key: string; kind: FileGroupKind; label: string; files: ChangedFile[] }

const KIND_LABEL: Record<FileGroupKind, string> = { terraform: "Terraform 模組", helm: "Helm chart", kubernetes: "Kubernetes 資源", other: "其他檔案" }
const K8S_DIRS = new Set(["k8s", "kubernetes", "manifests", "kustomize", "overlays", "argocd"])
// a manifest's file name usually says its resource kind (deployment.yaml, ingress-api.yml); its content is not loaded here
const K8S_KINDS: [RegExp, string][] = [
  [/deploy/, "Deployment"], [/statefulset|sts/, "StatefulSet"], [/daemonset/, "DaemonSet"], [/cronjob/, "CronJob"], [/job/, "Job"],
  [/serviceaccount|\bsa\b/, "ServiceAccount"], [/service|svc/, "Service"], [/ingress/, "Ingress"], [/configmap|cm\b/, "ConfigMap"], [/secret/, "Secret"],
  [/hpa|autoscal/, "HorizontalPodAutoscaler"], [/pvc|volume/, "PersistentVolumeClaim"], [/role/, "RBAC"],
  [/namespace|\bns\b/, "Namespace"], [/kustomization/, "Kustomization"],
]

const dirOf = (p: string) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "")

/** Group key + label for one file. Path-based like the backend: Terraform by directory (= module), Helm by chart,
 * Kubernetes manifests by directory and the resource kind their file name names. */
export function fileGroup(path: string): { key: string; kind: FileGroupKind; label: string } {
  const parts = path.toLowerCase().split("/")
  const name = parts[parts.length - 1]
  const dir = dirOf(path)
  if (/\.(tf|tfvars|hcl)$|\.tf\.json$/.test(name)) return { key: `tf:${dir}`, kind: "terraform", label: dir || "（根模組）" }
  const chart = parts.findIndex((p, i) => i < parts.length - 1 && (p === "charts" || p === "helm"))
  const tpl = parts.lastIndexOf("templates")
  if (name === "chart.yaml" || name === "values.yaml" || (chart >= 0 && tpl > chart)) {
    const root = tpl > 0 ? path.split("/").slice(0, tpl).join("/") : dir
    return { key: `helm:${root}`, kind: "helm", label: root || "（根目錄）" }
  }
  if (/\.ya?ml$/.test(name) && (name.startsWith("kustomization.") || parts.slice(0, -1).some((p) => K8S_DIRS.has(p)))) {
    const kind = K8S_KINDS.find(([rx]) => rx.test(name.replace(/\.ya?ml$/, "")))?.[1]
    return { key: `k8s:${dir}:${kind ?? ""}`, kind: "kubernetes", label: `${dir || "（根目錄）"}${kind ? ` · ${kind}（依檔名）` : ""}` }
  }
  return { key: `other:${dir}`, kind: "other", label: dir || "（根目錄）" }
}

/** Changed files grouped by module / chart / manifest directory: IaC groups first (by size), other files last. */
export function groupChangedFiles(files: ChangedFile[]): FileGroup[] {
  const by = new Map<string, FileGroup>()
  for (const f of files) {
    const g = fileGroup(f.path)
    const cur = by.get(g.key) ?? { ...g, files: [] }
    cur.files.push(f)
    by.set(g.key, cur)
  }
  const rank = (k: FileGroupKind) => (k === "other" ? 1 : 0)
  return [...by.values()].sort((a, b) => rank(a.kind) - rank(b.kind) || b.files.length - a.files.length || a.label.localeCompare(b.label))
}

export const groupKindLabel = (k: FileGroupKind) => KIND_LABEL[k]
