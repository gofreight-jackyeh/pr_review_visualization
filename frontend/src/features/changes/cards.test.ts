import { describe, expect, it } from "vitest"
import { sample } from "@/fixtures"
import { buildModel } from "@/entities/artifact/model"
import type { ChangedFile } from "@/entities/artifact/types"
import { changedModules } from "./cards"

describe("changedModules (變更與影響 without flow changes)", () => {
  it("groups changed files by directory, largest module first, root files under '.'", () => {
    const ov = structuredClone(sample.overlay)
    const f = (path: string): ChangedFile => ({ ...ov.changes.files[0], id: `file:${path}`, path })
    ov.flow_changes = []
    ov.changes.files = [f("app/a/views.py"), f("README.md"), f("app/a/models.py"), f("lib/util.py")]
    const mods = changedModules(buildModel(sample.map, ov))
    expect(mods.map((g) => [g.dir, g.files.map((x) => x.path)])).toEqual([
      ["app/a", ["app/a/views.py", "app/a/models.py"]], [".", ["README.md"]], ["lib", ["lib/util.py"]],
    ])
  })
})
