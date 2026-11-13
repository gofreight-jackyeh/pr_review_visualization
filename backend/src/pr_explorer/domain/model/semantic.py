from __future__ import annotations

from dataclasses import dataclass, field


ENGINES = ("claude", "codex")
# reasoning effort levels both CLIs accept (`claude --effort`, codex `model_reasoning_effort`)
EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass
class AiChoice:
    """Which AI writes the texts: engine, and optionally its model / reasoning effort (None = that engine's default,
    i.e. the operator's own CLI settings)."""

    engine: str = "claude"
    model: str | None = None
    effort: str | None = None

    def profile(self) -> str:
        """Cache-key part; "" for the plain default, so keys made before engines could be chosen stay valid."""
        parts = ([f"engine:{self.engine}"] if self.engine != "claude" else []) + \
            [f"{k}:{v}" for k, v in (("model", self.model), ("effort", self.effort)) if v]
        return "|".join(parts)

    def over(self, default: "AiChoice") -> "AiChoice":
        """This choice with unset parts taken from the server default (only when it is the same engine)."""
        same = self.engine == default.engine
        return AiChoice(self.engine, self.model or (default.model if same else None),
                        self.effort or (default.effort if same else None))


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
    model: str | None = None  # the model that answered, when the CLI reports it
