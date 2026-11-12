// @vitest-environment happy-dom
import { act } from "react"
import { afterEach, describe, expect, it } from "vitest"
import { infra } from "@/fixtures"
import { click, goNav, mount, nav } from "@/app/test-utils"
import { fileGroup, groupChangedFiles } from "./infra"

const tid = (root: ParentNode, id: string) => root.querySelector(`[data-testid="${id}"]`)
afterEach(() => { document.body.innerHTML = ""; history.replaceState(null, "", "/") })

describe("infrastructure repositories", () => {
  it("groups changed files by Terraform module, Helm chart and manifest directory", () => {
    expect(fileGroup("modules/vpc/main.tf")).toMatchObject({ kind: "terraform", label: "modules/vpc" })
    expect(fileGroup("main.tf")).toMatchObject({ kind: "terraform", label: "（根模組）" })
    expect(fileGroup("charts/api/templates/deployment.yaml")).toMatchObject({ kind: "helm", label: "charts/api" })
    expect(fileGroup("charts/api/values.yaml").key).toBe(fileGroup("charts/api/templates/svc.yaml").key) // one chart
    expect(fileGroup("k8s/base/ingress.yaml")).toMatchObject({ kind: "kubernetes", label: "k8s/base · Ingress（依檔名）" })
    expect(fileGroup("k8s/base/serviceaccount.yaml").label).toContain("ServiceAccount")
    expect(fileGroup("README.md").kind).toBe("other")
    const g = groupChangedFiles(infra().overlay.changes.files)
    expect(g.map((x) => [x.kind, x.label, x.files.length])).toEqual([
      ["helm", "charts/api", 2], ["terraform", "modules/vpc", 2], ["terraform", "envs/prod", 1],
      ["kubernetes", "k8s/base · Ingress（依檔名）", 1], ["other", "（根目錄）", 1],
    ])
  })

  it("leads with the summary and review focus, shows files by module, de-emphasises the map; back still works", async () => {
    const root = await mount("infra")
    expect([...root.querySelectorAll(".main-nav button")].map((b) => b.textContent)).toEqual(["PR 摘要", "審查重點", "變更檔案", "專案背景（有限）"])
    expect(nav(root)).toBe("PR 摘要")
    expect(root.querySelector(".main-nav button.dim")?.textContent).toBe("專案背景（有限）")
    expect(tid(root, "infra-notice")).not.toBeNull()
    await click(tid(root, "infra-go-review"))
    expect(nav(root)).toBe("審查重點")
    expect(document.activeElement?.id).toBe("view-title") // keyboard focus lands on the view it opened
    await goNav(root, "變更檔案")
    expect(tid(root, "infra-files")).not.toBeNull()
    expect(root.querySelectorAll('[data-testid="infra-group"]').length).toBe(5)
    expect(root.querySelectorAll('[data-testid="infra-files"] [data-testid="diff-link"]').length).toBe(7) // every file links to the PR diff
    await act(async () => { history.back(); await new Promise((r) => setTimeout(r, 20)) })
    expect(nav(root)).toBe("審查重點")
    await goNav(root, "專案背景（有限）")
    expect(tid(root, "infra-context-note")).not.toBeNull()
  })

  it("application repositories keep the usual navigation", async () => {
    const root = await mount("sample")
    expect(tid(root, "infra-notice")).toBeNull()
    expect(root.querySelector(".main-nav button.dim")).toBeNull()
  })
})
