// Types come from the v2 schema via `pnpm gen:types` (json-schema-to-typescript); do not hand-edit types.gen.ts.
import type { Capability, Component, Entity, EntryPoint, Flow, Impact, OverlayStep, PrOverlay, SystemMap } from "./types.gen"
export type * from "./types.gen"

export type ElementKind = "capability" | "entity" | "entry_point" | "component" | "flow"
export type ChangeStatus = Impact["change_status"]
export type ImpactStatus = Impact["impact_status"] | "none"
/** system = base map only; overlay = base + PR; before/after = one side of the PR. */
export type MapView = "system" | "overlay" | "before" | "after"
/** Flow views; "side" renders before and after next to each other. */
export type FlowView = "overlay" | "before" | "after" | "side"
export type StepStatus = OverlayStep["step_change_status"]
export type MapElement = Capability | Entity | EntryPoint | Component | Flow
export type Doc = SystemMap | PrOverlay
