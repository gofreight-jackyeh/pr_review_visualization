"""Start (or reuse from cache) and run an analysis. Two caches (docs/api.md):

- system map: (repo, merge_base_sha, analyzer_version, schema_version, semantic_on) — shared by every PR on that commit;
  on a miss the static map is rebuilt and Claude texts are carried over from an earlier map (pipeline._reuse)
- overlay:    (repo, merge_base_sha, head_sha, analyzer_version, schema_version, semantic_mode[, context_docs hash])
"""

from __future__ import annotations

import dataclasses
import threading
import uuid

from pathlib import Path

from ...domain.model.document import SCHEMA_VERSION
from ...domain.model.ids import short_hash
from ...domain.services.complexity_score import MODES
from .. import ports
from ..pipeline import STAGES, Context, Pipeline
from ..ports import AnalysisRepository, AnalysisRequest, ResolvedPr, SourceError, SourceProvider
from .errors import AcquireFailed, InvalidInput


def cache_key(pr: ResolvedPr, analyzer_version: str, semantic_mode: str, context_docs=(), lang: str = "auto") -> str:
    """Overlay key. Attached docs change the overlay (knowledge sources, architecture context, the Claude prompt),
    so an order-independent hash of (name, content) is part of the key; omitted when there are none."""
    parts = [pr.repo_key, pr.merge_base_sha, pr.head_sha, analyzer_version, SCHEMA_VERSION, semantic_mode, f"lang:{lang}"]
    if context_docs:
        parts.append("docs:" + short_hash(*sorted(short_hash(d.name, d.content, length=64) for d in context_docs), length=20))
    return "|".join(parts)


def system_map_key(pr: ResolvedPr, analyzer_version: str, semantic_on: bool, lang: str = "auto") -> str:
    # the language sits before the last part: donor lookup matches keys ending in "|semantic"
    return "|".join([pr.repo_key, pr.merge_base_sha, analyzer_version, SCHEMA_VERSION, f"lang:{lang}",
                     "semantic" if semantic_on else "static"])


def validate_request(req: AnalysisRequest) -> None:
    if bool(req.pr_url) == bool(req.local):
        raise InvalidInput("exactly one of pr_url / local is required")
    if req.semantic_mode not in MODES:
        raise InvalidInput(f"semantic_mode must be one of {', '.join(MODES)}")
    if req.local and not (req.local.repo_path and req.local.base and req.local.head):
        raise InvalidInput("local requires repo_path, base and head")
    for d in req.context_docs:
        if not d.name or len(d.content) > 500_000:
            raise InvalidInput("context_docs entries need a name and content under 500 KB")


class AnalyzePr:
    def __init__(self, source: SourceProvider, repo: AnalysisRepository, pipeline: Pipeline):
        self.source = source
        self.repo = repo
        self.pipeline = pipeline
        self.analyzer_version = ports.ANALYZER_VERSION
        self.output_language = pipeline.d.config.output_language  # part of both cache keys
        self.pipeline_runs = 0
        self._pending: dict[str, tuple[AnalysisRequest, ResolvedPr, str]] = {}
        self._lock = threading.Lock()

    def start(self, req: AnalysisRequest) -> tuple[str, str, bool]:
        """Returns (analysis_id, status, cached). Resolves exact SHAs synchronously."""
        validate_request(req)
        try:
            pr = self.source.resolve(req)
        except SourceError as exc:
            raise AcquireFailed(str(exc)) from exc
        key = cache_key(pr, self.analyzer_version, req.semantic_mode, req.context_docs, self.output_language)
        aid = f"a_{uuid.uuid4().hex[:16]}"
        req_dict = dataclasses.asdict(req)
        req_dict["context_docs"] = [{"name": d.name} for d in req.context_docs]  # don't persist pasted contents
        hit = self.repo.find_overlay(key)
        if hit and Path(hit["path"]).is_file():
            self.repo.create(aid, req_dict, STAGES, status="done", cache_key=key, title=pr.title, repo=pr.repo_name,
                             head_sha=pr.head_sha, overlay_id=hit["id"], system_map_id=hit["system_map_id"])
            return aid, "done", True
        self.repo.create(aid, req_dict, STAGES, status="queued", cache_key=key, title=pr.title, repo=pr.repo_name,
                         head_sha=pr.head_sha)
        with self._lock:
            self._pending[aid] = (req, pr, key)
        return aid, "queued", False

    def run(self, analysis_id: str) -> Path | None:
        with self._lock:
            req, pr, key = self._pending.pop(analysis_id)
        self.pipeline_runs += 1
        sm_key = system_map_key(pr, self.analyzer_version, req.semantic_mode != "off", self.output_language)
        overlay_id = f"ov_{short_hash(key, length=20)}"
        system_map_id = f"sm_{short_hash(sm_key, length=20)}"
        ctx = Context(analysis_id, req, pr, key, overlay_id, sm_key, system_map_id, self.analyzer_version)
        # Progressive results (docs/api.md): the IDs are exposed once the static documents are stored; status stays
        # "running" until Claude is done and the final documents replace them.
        ctx.on_static = lambda: self.repo.set_status(analysis_id, "running", overlay_id=overlay_id, system_map_id=system_map_id)
        self.repo.set_status(analysis_id, "running")

        def report(stage: str, status: str, message: str) -> None:
            self.repo.set_stage(analysis_id, stage, status, message)

        try:
            path = self.pipeline.run(ctx, report)
        except Exception as exc:  # recorded, surfaced through GET /api/analyses/{id}
            self.repo.set_status(analysis_id, "failed", error=f"{type(exc).__name__}: {exc}"[:2000])
            return None
        finally:
            if ctx.workspace is not None:  # nothing reads a worktree after its analysis; the bare cache stays
                try:
                    self.source.release(pr)
                except Exception:
                    pass
        self.repo.index_overlay(overlay_id, key, str(path), analysis_id, pr, system_map_id)
        self.repo.set_status(analysis_id, "done", overlay_id=overlay_id, system_map_id=system_map_id)
        return path

    def analyze_sync(self, req: AnalysisRequest) -> tuple[dict, dict]:
        """CLI helper: returns ({"overlay": path, "system_map": path}, analysis record)."""
        aid, status, cached = self.start(req)
        if not cached:
            self.run(aid)
        rec = self.repo.get(aid) or {}
        if rec.get("status") != "done":
            raise RuntimeError(rec.get("error") or "analysis failed")
        ov = self.repo.get_overlay_index(rec["overlay_id"])
        sm = self.repo.get_system_map(rec["system_map_id"])
        return {"overlay": ov["path"], "system_map": sm["path"]}, rec
