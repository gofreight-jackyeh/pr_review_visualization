from __future__ import annotations

from dataclasses import dataclass, field

from .graph import CodeRef


@dataclass
class Finding:
    id: str
    source: str  # static | semantic
    title: str
    description: str
    severity: str  # info | attention | warning
    status: str  # verified | needs_confirmation
    changed_file_ids: list[str]
    node_ids: list[str]  # internal: graph nodes the finding is about (mapped to capabilities / flows)
    refs: list[CodeRef]
    evidence_refs: list[str]
    rule: str | None = None
    capability_ids: list[str] = field(default_factory=list)
    flow_ids: list[str] = field(default_factory=list)
    business_impact: str | None = None

    def __post_init__(self) -> None:
        # spec §8: Claude's findings can never be presented as verified.
        if self.source == "semantic":
            self.status = "needs_confirmation"
