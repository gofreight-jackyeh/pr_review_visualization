"""Sends each semantic call to the engine its request chose (claude | codex); requests without a choice go to the
server default. Only engines the operator enabled (PR_EXPLORER_ENGINES) are reachable."""

from __future__ import annotations

from ...application.ports import SemanticAnalyzer, SemanticRequest
from ...domain.model.semantic import AiChoice, SemanticDraft


class EngineRouter:
    def __init__(self, engines: dict[str, SemanticAnalyzer], default: AiChoice):
        self.engines = engines
        self.default = default

    def analyze(self, req: SemanticRequest) -> SemanticDraft:
        engine = (req.ai or self.default).engine
        analyzer = self.engines.get(engine)
        if analyzer is None:
            return SemanticDraft(status="unavailable", error=f"engine {engine!r} is not enabled on this server "
                                                             "(PR_EXPLORER_ENGINES)")
        return analyzer.analyze(req)
