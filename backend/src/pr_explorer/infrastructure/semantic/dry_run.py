"""`pr-explorer analyze --dry-run-semantic DIR`: build every Claude prompt the pipeline would send and write it to DIR
instead of calling Claude (nothing leaves the machine). Each call answers "done" with an empty reply, so the pipeline
walks every step (capability batches, summary, domains incl. its one retry, phases, PR) and the stage messages carry
the prompt sizes. Used to measure and cap prompt sizes on large repositories."""

from __future__ import annotations

import itertools
import threading
from pathlib import Path

from ...application.ports import SemanticRequest
from ...domain.model.semantic import SemanticDraft
from ..security.secret_scrubber import scrub
from .prompt_builder import build_user_prompt, system_prompt


class DryRunSemanticAnalyzer:
    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._n = itertools.count(1)
        self._lock = threading.Lock()

    def analyze(self, req: SemanticRequest) -> SemanticDraft:
        sp, up = system_prompt(req.kind), scrub(build_user_prompt(req))
        with self._lock:
            n = next(self._n)
        path = self.out_dir / f"{n:03d}-{req.kind}.txt"
        path.write_text(f"{sp}\n\n----- user prompt -----\n{up}", "utf-8")
        return SemanticDraft(status="done", payload={}, error=f"dry run: prompt written to {path.name}", calls=1,
                             prompt_chars=len(sp) + len(up), seconds=0.0)
