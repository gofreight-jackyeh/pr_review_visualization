"""Schema v2 documents (schemas/artifact/v2): system_map and pr_overlay. Field names mirror the schema exactly;
fields named in the serializer's INTERNAL set (meta, hunks, ...) are never written."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .changes import ChangedFile, Coverage
from .finding import Finding
from .graph import CodeRef, Evidence

SCHEMA_VERSION = "2.5.0"

DISCLAIMER = (
    "Structure comes from static analysis (Python AST / TypeScript compiler) of the repository at one commit; "
    "the code was never executed. Business flows are reconstructed statically and are NOT runtime traces: "
    "'possible' steps sit inside conditions that may not run, 'unknown' steps are reached only through dynamic calls. "
    "Business names and descriptions are written by Claude and each cites evidence; where they are absent, "
    "the business explanation is unavailable and technical names are shown."
)


@dataclass
class Text:
    text: str
    source: str  # static | semantic | pr_description
    evidence_refs: list[str]

    def __post_init__(self) -> None:
        if not self.evidence_refs:
            raise ValueError("a Text statement must cite at least one evidence id")


@dataclass
class Repo:
    name: str
    owner: str | None = None


@dataclass
class TextBasis:
    """Incremental-reuse bookkeeping of one capability (static, schema 2.5.0): the fingerprint of the business-facing
    surface Claude is shown at this map's commit, and where its Claude texts come from (when / at which commit they were
    written, how many later base commits reused them, how many lines of its code files changed since)."""

    surface: str
    written_at: str | None = None
    written_commit: str | None = None
    base_changes: int = 0
    changed_lines: int = 0


@dataclass
class Capability:
    id: str
    technical_name: str
    path: str
    evidence_refs: list[str]
    entity_ids: list[str] = field(default_factory=list)
    entry_point_ids: list[str] = field(default_factory=list)
    category: str = "business"  # business | shared | engineering (static, spec v2 Layer 4)
    parent_capability_id: str | None = None  # set when an oversized capability was split by sub-directory
    domain_id: str | None = None  # business capabilities only
    language: str | None = None
    component_count: int | None = None
    business_name: Text | None = None
    description: Text | None = None
    text_basis: TextBasis | None = None


@dataclass
class Domain:
    """Business domain above capabilities: Claude-assigned (semantic) or one per top-level directory (static)."""

    id: str
    technical_name: str
    source: str  # static | semantic
    order: int
    evidence_refs: list[str]
    business_name: Text | None = None
    description: Text | None = None


@dataclass
class Phase:
    """Contiguous steps of one flow grouped into a business phase (Claude cuts; the backend validates)."""

    id: str
    label: Text
    step_ids: list[str]


@dataclass
class Entity:
    id: str
    technical_name: str
    capability_id: str
    refs: list[CodeRef]
    evidence_refs: list[str]
    business_name: Text | None = None
    description: Text | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class EntryPoint:
    id: str
    technical_name: str
    capability_id: str
    kind: str  # route | api | task | command | schedule | event | ui_route
    refs: list[CodeRef]
    evidence_refs: list[str]
    handler_component_id: str | None = None
    business_name: Text | None = None
    description: Text | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Component:
    id: str
    technical_name: str
    capability_id: str
    kind: str
    refs: list[CodeRef]
    evidence_refs: list[str]
    business_name: Text | None = None
    description: Text | None = None


@dataclass
class Relation:
    id: str
    source: str
    target: str
    kind: str  # depends_on | calls | uses_entity
    weight: int
    confidence: str  # confirmed | unknown
    evidence_refs: list[str]


@dataclass
class Branch:
    condition: str


@dataclass
class Step:
    id: str
    order: int
    kind: str  # enter | call | read | write | create | delete | emit | external | raise
    capability_id: str
    component_ids: list[str]
    technical_label: str
    evidence_status: str  # confirmed | possible | unknown
    evidence_refs: list[str]
    entity_id: str | None = None
    business_label: Text | None = None
    branch: Branch | None = None
    step_change_status: str | None = None  # overlay only: added | removed | changed | unchanged
    meta: dict[str, Any] = field(default_factory=dict)  # internal: diff keys


@dataclass
class Timeline:
    mode: str  # animation | static_flow
    steps: list[Step]
    mode_reason: str | None = None


@dataclass
class Flow:
    id: str
    technical_name: str
    entry_point_id: str
    capability_ids: list[str]
    entity_ids: list[str]
    timeline: Timeline
    evidence_refs: list[str]
    business_name: Text | None = None
    description: Text | None = None
    phases: list[Phase] | None = None


@dataclass
class FlowBudget:
    entry_points_total: int
    flows_built: int


@dataclass
class Decision:
    score: float
    reasons: list[str]
    mode: str  # auto | off | light | standard | deep


@dataclass
class SemanticStatus:
    status: str  # skipped | done | failed | unavailable
    tier: str | None = None  # none | light | standard | deep
    decision: Decision | None = None
    dropped_claims: int | None = None
    knowledge_sources: list[str] | None = None
    error: str | None = None


@dataclass
class SystemInfo:
    technical_name: str
    summary: Text | None = None
    repo_kind: str = "application"  # application | infrastructure (Terraform / Helm / Kubernetes only; repo_kind.py)


@dataclass
class ArchRule:
    id: str
    origin: str  # documented | inferred
    kind: str  # layers | forbidden | independence | interface_only | placement | shared_mechanism | acyclic
    title: str
    description: str
    scope_capability_ids: list[str]
    evidence_refs: list[str]
    source_ref: CodeRef | None = None
    statistic: str | None = None
    exception_count: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)  # internal: what the checker needs (never written)


@dataclass
class Interface:
    """A contract other code depends on (spec §8): outer = used directly by users / clients (public HTTP API, web app
    routes, published SDK exports, outbound formats); middle = between capabilities (module APIs, task signatures).
    `meta["contract"]` holds what the PR diff compares (methods, serializer fields, signatures...); never written."""

    id: str
    technical_name: str
    ring: str  # outer | middle
    kind: str  # http_api | web_route | sdk_export | outbound_format | module_api | task | event
    capability_id: str
    detected_by: str  # static | config (.pr-explorer.yml)
    refs: list[CodeRef]
    evidence_refs: list[str]
    entry_point_id: str | None = None
    business_name: Text | None = None
    description: Text | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SystemMap:
    analyzer_version: str
    system_map_id: str
    generated_at: str
    repo: Repo
    commit_sha: str
    system: SystemInfo
    coverage: list[Coverage]
    capabilities: list[Capability]
    entities: list[Entity]
    entry_points: list[EntryPoint]
    components: list[Component]
    relations: list[Relation]
    flows: list[Flow]
    flow_budget: FlowBudget
    evidence: list[Evidence]
    semantic: SemanticStatus
    domains: list[Domain] = field(default_factory=list)
    architecture_rules: list[ArchRule] = field(default_factory=list)
    interfaces: list[Interface] = field(default_factory=list)
    disclaimer: str = DISCLAIMER
    kind: str = "system_map"
    schema_version: str = SCHEMA_VERSION


# ---------------- PR overlay ----------------

@dataclass
class PullRequest:
    source: str  # github | local
    title: str
    body: str
    repo: Repo
    base_ref: str
    head_ref: str
    base_sha: str
    head_sha: str
    merge_base_sha: str
    url: str | None = None
    number: int | None = None
    author: str | None = None


@dataclass
class Impact:
    target_id: str
    target_kind: str  # capability | entity | entry_point | component | flow
    change_status: str  # added | modified | removed | unchanged
    impact_status: str  # direct | confirmed_dependency | evidence_supported | potential | unknown
    evidence_refs: list[str]
    impact_depth: int | None = None


@dataclass
class FlowChange:
    flow_id: str
    change_status: str  # added | modified | removed
    mode: str
    steps: list[Step]
    evidence_refs: list[str]
    mode_reason: str | None = None
    phases: list[Phase] | None = None


@dataclass
class BehaviorChange:
    id: str
    after: Text
    flow_ids: list[str] = field(default_factory=list)
    capability_ids: list[str] = field(default_factory=list)
    before: Text | None = None


@dataclass
class HeadAdditions:
    capabilities: list[Capability] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    entry_points: list[EntryPoint] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)
    interfaces: list[Interface] = field(default_factory=list)


@dataclass
class WalkTarget:
    view: str  # system | overlay | flow | findings | architecture | evidence
    capability_ids: list[str] | None = None
    flow_id: str | None = None
    finding_id: str | None = None


@dataclass
class WalkStep:
    id: str
    title: str
    body: Text
    target: WalkTarget


@dataclass
class WalkQuestion:
    id: str
    question: str
    answer: Text
    target: WalkTarget


@dataclass
class Walkthrough:
    steps: list[WalkStep] = field(default_factory=list)
    questions: list[WalkQuestion] = field(default_factory=list)


@dataclass
class ViolatingRelation:
    source: str
    target: str


@dataclass
class InterfaceChange:
    interface_id: str
    change: str  # added | modified | removed
    compatibility: str  # compatible | breaking | unknown
    detail: str


@dataclass
class ArchCheck:
    """Architecture consistency of a PR (spec §8): an interface change observed in the diff, a documented/inferred rule
    decided statically, or a Claude hypothesis. `ring` = the outermost interface ring it concerns (UI orders outer → inner)."""

    id: str
    origin: str  # documented | inferred | observed | hypothesis
    source: str  # static | semantic
    check: str  # interface_change | dependency_direction | interface | placement | shared_mechanism | new_debt | cycle
    ring: str  # outer | middle | inner
    verdict: str  # follows | violates | new_debt | unclear
    status: str  # verified | needs_confirmation
    title: str
    description: str
    capability_ids: list[str]
    pr_refs: list[CodeRef]
    existing_refs: list[CodeRef]
    evidence_refs: list[str]
    rule_id: str | None = None
    business_impact: str | None = None
    violating_relation: ViolatingRelation | None = None
    interface_change: InterfaceChange | None = None

    def __post_init__(self) -> None:
        if self.source == "semantic" or self.origin == "hypothesis":  # spec §8: never verified
            self.origin, self.status = "hypothesis", "needs_confirmation"


@dataclass
class ChangesPayload:
    files: list[ChangedFile]


@dataclass
class PrOverlay:
    analyzer_version: str
    overlay_id: str
    system_map_id: str
    generated_at: str
    pr: PullRequest
    coverage: list[Coverage]
    changes: ChangesPayload
    purpose: Text
    behavior_changes: list[BehaviorChange]
    impacts: list[Impact]
    flow_changes: list[FlowChange]
    head_additions: HeadAdditions
    findings: list[Finding]
    architecture_checks: list[ArchCheck]
    evidence: list[Evidence]
    semantic: SemanticStatus
    walkthrough: Walkthrough
    design_rationale: Text | None = None
    kind: str = "pr_overlay"
    schema_version: str = SCHEMA_VERSION
