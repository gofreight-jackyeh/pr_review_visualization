"""Read-side use cases: system map, overlay, freshness, analysis status. There is no source-code reader (spec v2 §4)."""

from __future__ import annotations

from ..ports import AnalysisRepository, DocumentStore, SourceError, SourceProvider
from .errors import AcquireFailed, NotFound


class GetSystemMap:
    def __init__(self, store: DocumentStore):
        self.store = store

    def __call__(self, system_map_id: str) -> dict:
        doc = self.store.load_system_map(system_map_id)
        if doc is None:
            raise NotFound(f"system map {system_map_id} not found")
        return doc


class GetOverlay:
    def __init__(self, store: DocumentStore):
        self.store = store

    def __call__(self, overlay_id: str) -> dict:
        doc = self.store.load_overlay(overlay_id)
        if doc is None:
            raise NotFound(f"overlay {overlay_id} not found")
        return doc


class CheckFreshness:
    def __init__(self, repo: AnalysisRepository, source: SourceProvider):
        self.repo = repo
        self.source = source

    def __call__(self, overlay_id: str) -> dict:
        idx = self.repo.get_overlay_index(overlay_id)
        if idx is None:
            raise NotFound(f"overlay {overlay_id} not found")
        try:
            current = self.source.current_head_sha(idx["descriptor"])
        except SourceError as exc:
            raise AcquireFailed(str(exc)) from exc
        return {"stale": current != idx["head_sha"], "analyzed_head_sha": idx["head_sha"], "current_head_sha": current}


class AnalysisStatus:
    def __init__(self, repo: AnalysisRepository):
        self.repo = repo

    def get(self, analysis_id: str) -> dict:
        r = self.repo.get(analysis_id)
        if r is None:
            raise NotFound(f"analysis {analysis_id} not found")
        return {"analysis_id": r["id"], "status": r["status"], "stages": r["stages"], "error": r["error"],
                "overlay_id": r["overlay_id"], "system_map_id": r["system_map_id"]}

    def list(self, limit: int = 50) -> list[dict]:
        rows = self.repo.list(limit)
        # queue position (1 = next to start) = rank among queued analyses by creation order; derived, not stored
        waiting = [r["id"] for r in reversed(rows) if r["status"] == "queued"]
        return [{"analysis_id": r["id"], "status": r["status"], "title": r["title"], "repo": r["repo"],
                 "head_sha": r["head_sha"], "created_at": r["created_at"], "overlay_id": r["overlay_id"],
                 "system_map_id": r["system_map_id"],
                 "queue_position": waiting.index(r["id"]) + 1 if r["status"] == "queued" else None}
                for r in rows]
