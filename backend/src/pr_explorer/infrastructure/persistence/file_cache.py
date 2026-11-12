"""Per-file analysis results on disk (`$PR_EXPLORER_HOME/analysis-cache.db`), shared by every repository, side and
commit: the analyzers key each entry by what its result depends on (analyzer version, language, path, file content and
analyzer environment), so an entry never goes stale; it only stops being looked up.

Values are opaque bytes (the analyzers encode them); stored zlib-compressed. Size is bounded: past `max_bytes` the
least recently used entries are deleted. Any damage reads as a miss: an entry that does not decompress is deleted
(the caller recomputes it), and a database file that cannot be opened is moved aside and recreated. A cache that
still fails turns itself off for the process: analysis never depends on it."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
import zlib
from pathlib import Path

CACHE_DB = "analysis-cache.db"
DEFAULT_MAX_BYTES = 1 << 30  # 1 GiB; a large repository's full TS + Python results take ~25 MB per distinct version set
TOUCH_AFTER_S = 3600  # used_at is refreshed at most hourly per entry (LRU granularity), not on every read
_CHUNK = 500  # keys per IN (...) query (SQLite's default variable limit is 999 on old builds)
log = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, data BLOB NOT NULL, size INTEGER NOT NULL,
                                    used_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS entries_used ON entries(used_at);
"""


def cache_key(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8", "surrogatepass")).hexdigest()


class FileResultCache:
    def __init__(self, path: Path, max_bytes: int = DEFAULT_MAX_BYTES):
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._disabled = False

    # ------------------------------------------------------------------ connection
    def _db(self) -> sqlite3.Connection | None:
        if self._disabled:
            return None
        if self._conn is None:
            try:
                self._conn = self._open()
            except sqlite3.DatabaseError as exc:  # not a database / corrupt header: start over, once
                log.warning("analysis cache %s unreadable (%s); recreating it", self.path, exc)
                try:
                    self.path.replace(self.path.with_suffix(f".corrupt-{int(time.time())}"))
                    for side in ("-wal", "-shm"):
                        Path(f"{self.path}{side}").unlink(missing_ok=True)
                    self._conn = self._open()
                except (OSError, sqlite3.Error) as exc2:
                    return self._off(exc2)
            except (OSError, sqlite3.Error) as exc:
                return self._off(exc)
        return self._conn

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.execute("SELECT count(*) FROM entries").fetchone()  # surfaces a corrupt file now, not mid-analysis
        return conn

    def _off(self, exc: BaseException) -> None:
        log.warning("analysis cache %s disabled for this process: %s", self.path, exc)
        self._disabled = True
        self._conn = None
        return None

    # ------------------------------------------------------------------ API
    def get_many(self, keys: list[str]) -> dict[str, bytes]:
        """key -> stored bytes for the keys present (damaged entries are dropped and missing from the result)."""
        out: dict[str, bytes] = {}
        bad: list[str] = []
        stale: list[str] = []
        now = int(time.time())
        with self._lock:
            db = self._db()
            if db is None:
                return out
            try:
                uniq = list(dict.fromkeys(keys))
                for i in range(0, len(uniq), _CHUNK):
                    chunk = uniq[i:i + _CHUNK]
                    q = f"SELECT key, data, used_at FROM entries WHERE key IN ({','.join('?' * len(chunk))})"
                    for k, data, used in db.execute(q, chunk):
                        try:
                            out[k] = zlib.decompress(data)
                        except (zlib.error, TypeError):
                            bad.append(k)
                            continue
                        if now - used > TOUCH_AFTER_S:
                            stale.append(k)
                if bad:
                    db.executemany("DELETE FROM entries WHERE key = ?", [(k,) for k in bad])
                if stale:
                    db.executemany("UPDATE entries SET used_at = ? WHERE key = ?", [(now, k) for k in stale])
            except sqlite3.Error as exc:
                self._off(exc)
                return {}
        return out

    def put_many(self, items: dict[str, bytes]) -> None:
        if not items:
            return
        now = int(time.time())
        rows = [(k, d, len(d), now) for k, d in ((k, zlib.compress(v, 6)) for k, v in items.items())]
        with self._lock:
            db = self._db()
            if db is None:
                return
            try:
                db.execute("BEGIN IMMEDIATE")
                db.executemany("INSERT OR REPLACE INTO entries (key, data, size, used_at) VALUES (?, ?, ?, ?)", rows)
                db.execute("COMMIT")
                self._prune(db)
            except sqlite3.Error as exc:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                self._off(exc)

    def discard(self, keys: list[str]) -> None:
        with self._lock:
            db = self._db()
            if db is not None and keys:
                try:
                    db.executemany("DELETE FROM entries WHERE key = ?", [(k,) for k in keys])
                except sqlite3.Error as exc:
                    self._off(exc)

    def size(self) -> tuple[int, int]:
        """(entries, stored bytes)."""
        with self._lock:
            db = self._db()
            if db is None:
                return 0, 0
            n, total = db.execute("SELECT count(*), coalesce(sum(size), 0) FROM entries").fetchone()
            return int(n), int(total)

    def _prune(self, db: sqlite3.Connection) -> None:
        """LRU: past max_bytes, delete the least recently used entries down to 80% of it."""
        (total,) = db.execute("SELECT coalesce(sum(size), 0) FROM entries").fetchone()
        if total <= self.max_bytes:
            return
        excess = total - int(self.max_bytes * 0.8)
        doomed: list[str] = []
        for k, size in db.execute("SELECT key, size FROM entries ORDER BY used_at, key"):
            doomed.append(k)
            excess -= size
            if excess <= 0:
                break
        db.executemany("DELETE FROM entries WHERE key = ?", [(k,) for k in doomed])
