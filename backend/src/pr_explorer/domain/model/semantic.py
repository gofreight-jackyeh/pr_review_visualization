from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SemanticDraft:
    """Raw, untrusted reply from the semantic analyzer: text claims only, never structure.

    `payload` is the parsed JSON object; the evidence gate (domain/services/semantic_gate.py) decides what survives."""

    status: str  # done | failed | unavailable
    error: str | None = None
    payload: dict = field(default_factory=dict)
    knowledge_sources: list[str] = field(default_factory=list)
    calls: int = 0
    prompt_chars: int = 0  # size of the prompt(s) sent (system + user), summed over attempts
    seconds: float = 0.0  # wall time of the call(s), summed over attempts
