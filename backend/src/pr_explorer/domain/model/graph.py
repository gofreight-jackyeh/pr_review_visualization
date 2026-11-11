"""Internal analysis graph (one per side). Never serialized as-is: the system map / overlay are built from it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CodeRef:
    side: str  # base | head
    path: str
    start_line: int
    end_line: int | None = None
    symbol: str | None = None  # internal only (not in schema v2)


@dataclass(frozen=True)
class Producer:
    name: str
    version: str


@dataclass
class Evidence:
    id: str
    kind: str  # ast | import | diff | framework_rule | ts_compiler | doc | pr_metadata
    description: str
    refs: list[CodeRef]
    producer: Producer


@dataclass
class Node:
    id: str
    kind: str  # module | class | function | method | view | serializer | form | model | task | command | route | component | external
    level: str  # module | symbol
    label: str
    parent_id: str | None
    code_refs: list[CodeRef] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    change_status: str = "unchanged"
    impact_status: str = "none"
    impact_depth: int | None = None
    language: str | None = None
    tags: list[str] = field(default_factory=list)
    # Analyzer facts (hash, signature, decorators, bases, fields...).
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    id: str
    source: str
    target: str
    kind: str  # imports | calls | inherits | routes_to | uses_model | renders | emits
    confidence: str  # confirmed | unknown
    evidence_refs: list[str] = field(default_factory=list)
    change_status: str = "unchanged"


@dataclass(frozen=True)
class CallShape:
    positional: int
    keywords: tuple[str, ...]
    star_args: bool = False
    star_kwargs: bool = False


@dataclass
class FlowStep:
    """One statement-level fact inside a function body, in source order (input to business-step building)."""

    path: str
    line: int
    end_line: int
    kind: str  # call | query | raise | return | branch | emit | external
    label: str
    target_ids: list[str] = field(default_factory=list)
    edge_ids: list[str] = field(default_factory=list)
    confidence: str = "confirmed"
    conditions: tuple[str, ...] = ()  # enclosing unconfirmed branch conditions
    call_shape: CallShape | None = None
    evidence_refs: list[str] = field(default_factory=list)
    orm_action: str | None = None  # query: create | read | write | delete


@dataclass
class EntryFact:
    """A statically detected entry point: URL route, DRF registration, task, schedule, command, UI route."""

    id: str  # node id of the declaration (route node) or of the handler (task/command)
    kind: str  # route | api | task | schedule | command | ui_route
    label: str
    handler_id: str | None
    ref: CodeRef
    evidence_id: str


@dataclass(frozen=True)
class UnresolvedRef:
    """A reference to a project symbol that does not resolve on this side."""

    target: str  # dotted qualname, e.g. orders.services.old_fn
    code_ref: CodeRef
    from_node: str


@dataclass
class SideGraph:
    side: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[str, Edge] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    flows: dict[str, list[FlowStep]] = field(default_factory=dict)
    entry_points: list[EntryFact] = field(default_factory=list)
    unresolved: list[UnresolvedRef] = field(default_factory=list)
    files: set[str] = field(default_factory=set)
    notes: dict[str, list[str]] = field(default_factory=dict)  # language -> coverage notes
    ordered_languages: set[str] = field(default_factory=set)  # languages whose flows carry statement order

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing is None:
            self.nodes[node.id] = node
            return node
        for ref in node.evidence_refs:
            if ref not in existing.evidence_refs:
                existing.evidence_refs.append(ref)
        return existing

    def add_edge(self, edge: Edge) -> Edge:
        existing = self.edges.get(edge.id)
        if existing is None:
            self.edges[edge.id] = edge
            return edge
        if len(existing.evidence_refs) < 8:
            for ref in edge.evidence_refs:
                if ref not in existing.evidence_refs:
                    existing.evidence_refs.append(ref)
        if edge.confidence == "confirmed":
            existing.confidence = "confirmed"
        return existing

    def add_evidence(self, ev: Evidence) -> str:
        self.evidence.setdefault(ev.id, ev)
        return ev.id

    def merge(self, other: "SideGraph") -> None:
        for ev in other.evidence.values():
            self.add_evidence(ev)
        for n in other.nodes.values():
            self.add_node(n)
        for e in other.edges.values():
            self.add_edge(e)
        for k, v in other.flows.items():
            self.flows.setdefault(k, v)
        known = {e.id for e in self.entry_points}
        self.entry_points.extend(e for e in other.entry_points if e.id not in known)
        self.unresolved.extend(other.unresolved)
        self.ordered_languages |= other.ordered_languages
        for lang, notes in other.notes.items():
            self.notes.setdefault(lang, []).extend(n for n in notes if n not in self.notes.get(lang, []))


@dataclass
class Graph:
    """Merged base+head graph with change_status on every element."""

    nodes: dict[str, Node]
    edges: dict[str, Edge]
    evidence: dict[str, Evidence]
    base: SideGraph
    head: SideGraph

    def changed_node_ids(self) -> set[str]:
        return {n.id for n in self.nodes.values() if n.change_status != "unchanged"}
