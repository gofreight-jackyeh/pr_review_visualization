"""SQLite: analyses, stages, system-map index (cache key → map) and overlay index (cache key → overlay).

Schema v2 lives in its own DB file (pr_explorer_v2.db): v1 artifacts are invalid under schema 2 and are simply
re-analyzed; nothing is migrated."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from ....application.ports import ResolvedPr

DB_NAME = "pr_explorer_v2.db"
DOC_DIRS = ("system-maps", "overlays")  # FsDocumentStore directories under the data home
SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
  id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  request_json TEXT NOT NULL, cache_key TEXT, title TEXT, repo TEXT, head_sha TEXT,
  overlay_id TEXT, system_map_id TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS stages (
  analysis_id TEXT NOT NULL, idx INTEGER NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (analysis_id, name)
);
CREATE TABLE IF NOT EXISTS system_maps (
  id TEXT PRIMARY KEY, cache_key TEXT NOT NULL UNIQUE, path TEXT NOT NULL, repo_key TEXT NOT NULL,
  commit_sha TEXT NOT NULL, semantic_status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS overlays (
  id TEXT PRIMARY KEY, cache_key TEXT NOT NULL UNIQUE, path TEXT NOT NULL, analysis_id TEXT NOT NULL,
  repo_key TEXT NOT NULL, descriptor_json TEXT NOT NULL, base_sha TEXT NOT NULL, head_sha TEXT NOT NULL,
  merge_base_sha TEXT NOT NULL, system_map_id TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS analyses_created ON analyses(created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SqliteAnalysisRepository:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._home = db_path.parent.resolve()  # document paths are stored relative to it (see _rel / _abs)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")

    def _rel(self, path) -> str:
        """Store document paths relative to the data home, so a copied database works under another home."""
        p = Path(path)
        try:
            return p.resolve().relative_to(self._home).as_posix()
        except ValueError:
            return str(p)

    def _abs(self, row) -> dict:
        r = dict(row)
        p = Path(r["path"])
        if not p.is_absolute():
            r["path"] = str(self._home / p)
        elif not p.exists() and p.parent.name in DOC_DIRS:  # absolute path written by an older version / other machine
            r["path"] = str(self._home / p.parent.name / p.name)
        return r

    def _x(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def create(self, analysis_id, request, stage_names, status="queued", cache_key=None, title="", repo="", head_sha="",
               overlay_id=None, system_map_id=None) -> None:
        now = _now()
        stage_status = "done" if status == "done" else "pending"
        with self._lock:
            self._conn.execute("BEGIN")
            self._conn.execute(
                "INSERT INTO analyses VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (analysis_id, status, now, now, json.dumps(request), cache_key, title, repo, head_sha, overlay_id, system_map_id),
            )
            for i, n in enumerate(stage_names):
                msg = "served from cache" if status == "done" else ""
                self._conn.execute("INSERT INTO stages VALUES (?,?,?,?,?)", (analysis_id, i, n, stage_status, msg))
            self._conn.execute("COMMIT")

    def set_status(self, analysis_id, status, error=None, overlay_id=None, system_map_id=None) -> None:
        self._x("UPDATE analyses SET status=?, error=?, overlay_id=COALESCE(?, overlay_id), "
                "system_map_id=COALESCE(?, system_map_id), updated_at=? WHERE id=?",
                (status, error, overlay_id, system_map_id, _now(), analysis_id))

    def set_stage(self, analysis_id, name, status, message="") -> None:
        self._x("UPDATE stages SET status=?, message=? WHERE analysis_id=? AND name=?", (status, message[:2000], analysis_id, name))  # call stats make semantic long

    def get(self, analysis_id) -> dict | None:
        rows = self._x("SELECT * FROM analyses WHERE id=?", (analysis_id,))
        if not rows:
            return None
        r = dict(rows[0])
        r["stages"] = [dict(s) for s in self._x(
            "SELECT name, status, message FROM stages WHERE analysis_id=? ORDER BY idx", (analysis_id,))]
        return r

    def list(self, limit=50) -> list[dict]:
        return [dict(r) for r in self._x(
            "SELECT id, status, title, repo, head_sha, created_at, overlay_id, system_map_id FROM analyses "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))]

    # ---- system maps
    def index_system_map(self, system_map_id, cache_key, path, repo_key, commit_sha, semantic_status) -> None:
        self._x("INSERT OR REPLACE INTO system_maps VALUES (?,?,?,?,?,?,?)",
                (system_map_id, cache_key, self._rel(path), repo_key, commit_sha, semantic_status, _now()))

    def find_system_map(self, cache_key) -> dict | None:
        rows = self._x("SELECT * FROM system_maps WHERE cache_key=?", (cache_key,))
        return self._abs(rows[0]) if rows else None

    def list_system_maps(self, repo_key, limit=50) -> list[dict]:
        return [self._abs(r) for r in self._x("SELECT * FROM system_maps WHERE repo_key=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                         (repo_key, limit))]

    def get_system_map(self, system_map_id) -> dict | None:
        rows = self._x("SELECT * FROM system_maps WHERE id=?", (system_map_id,))
        return self._abs(rows[0]) if rows else None

    # ---- overlays
    def index_overlay(self, overlay_id, cache_key, path, analysis_id, pr: ResolvedPr, system_map_id) -> None:
        self._x(
            "INSERT OR REPLACE INTO overlays VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (overlay_id, cache_key, self._rel(path), analysis_id, pr.repo_key, json.dumps(pr.descriptor), pr.base_sha, pr.head_sha,
             pr.merge_base_sha, system_map_id, _now()),
        )

    def find_overlay(self, cache_key) -> dict | None:
        return self._overlay_row(self._x("SELECT * FROM overlays WHERE cache_key=?", (cache_key,)))

    def get_overlay_index(self, overlay_id) -> dict | None:
        return self._overlay_row(self._x("SELECT * FROM overlays WHERE id=?", (overlay_id,)))

    def _overlay_row(self, rows) -> dict | None:
        if not rows:
            return None
        r = self._abs(rows[0])
        r["descriptor"] = json.loads(r.pop("descriptor_json"))
        return r
