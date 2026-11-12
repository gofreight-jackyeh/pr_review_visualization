"""Isolated bare-clone cache + per-SHA detached worktrees under $PR_EXPLORER_HOME.

The user's own checkout is only ever *read* (rev-parse / clone source); all checkouts happen
in our cache. Repository hooks are disabled for every git invocation.

A released worktree is kept idle for a few minutes (at most MAX_IDLE per repository): the next analysis resets it to
its commit (the same one, or another after moving it), rewriting only the files that differ — a full checkout plus
removal of a large repository (34k files) costs ~5.5 s per worktree, a reset ~1 s.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from pathlib import Path

from ...application.ports import SourceError
from ..process.runner import CommandRunner

SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]+")
IDLE_TTL_S = 600.0  # an unheld worktree is removed after this long (or at the next server start)
MAX_IDLE = 2  # unheld worktrees kept per repository: one PR's base + head


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


class GitCache:
    def __init__(self, home: Path, runner: CommandRunner, timeout: float = 600, idle_ttl: float = IDLE_TTL_S):
        self.home = home
        self.runner = runner
        self.timeout = timeout
        self.idle_ttl = idle_ttl  # 0: remove a worktree as soon as its last holder releases it
        self._lock = threading.RLock()  # serializes clone/fetch/worktree mutations
        # Worktrees are shared by concurrent analyses (two PRs on one base); when its last holder releases one it
        # becomes idle (reusable, see the module doc). Nothing reads a worktree after its analysis (no source
        # endpoint; freshness and fingerprints use the bare cache), so they are regenerable scratch space.
        self._holds: dict[Path, int] = {}
        self._idle: dict[Path, tuple[str, float]] = {}  # path -> (repo key, monotonic time it became idle)

    @staticmethod
    def key_for(prefix: str, ident: str, name: str) -> str:
        return f"{prefix}-{SAFE_KEY.sub('_', name)[:40]}-{hashlib.sha256(ident.encode()).hexdigest()[:10]}"

    def repo_dir(self, key: str) -> Path:
        return self.home / "repos" / f"{key}.git"

    def git(self, args: list[str], key: str | None = None, cwd: str | None = None, check: bool = True) -> str:
        base = ["git", "-c", "core.hooksPath=/dev/null", "-c", "advice.detachedHead=false"]
        if key is not None:
            base += ["--git-dir", str(self.repo_dir(key))]
        res = self.runner.run(base + args, cwd=cwd, timeout=self.timeout, env=git_env())
        if check and res.returncode != 0:
            raise SourceError(f"git {' '.join(args[:3])} failed: {res.stderr.strip()[:400]}")
        return res.stdout if res.returncode == 0 else ""

    def ensure_clone(self, key: str, source: str) -> None:
        d = self.repo_dir(key)
        with self._lock:
            if not (d / "HEAD").exists():
                d.parent.mkdir(parents=True, exist_ok=True)
                self.git(["clone", "--bare", "--quiet", source, str(d)])

    def fetch(self, key: str, source: str, refspecs: list[str]) -> None:
        with self._lock:
            self.git(["fetch", "--quiet", "--force", "--update-head-ok", "--prune", source, *refspecs], key=key)

    def has_commit(self, key: str, sha: str) -> bool:
        return bool(self.git(["cat-file", "-t", sha], key=key, check=False).strip() == "commit")

    def rev_parse(self, key: str, ref: str) -> str:
        out = self.git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], key=key, check=False).strip()
        if not SHA_RE.match(out):
            raise SourceError(f"cannot resolve ref {ref!r}")
        return out

    def merge_base(self, key: str, a: str, b: str) -> str:
        out = self.git(["merge-base", a, b], key=key).strip()
        if not SHA_RE.match(out):
            raise SourceError(f"no merge base between {a[:10]} and {b[:10]}")
        return out

    def diff(self, key: str, a: str, b: str) -> str:
        return self.git(["diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M", "-U3", a, b], key=key)

    def ls_tree_blobs(self, key: str, sha: str) -> dict[str, str]:
        """path -> blob SHA at a commit (cheap: no file content is read)."""
        out = self.git(["ls-tree", "-r", "-z", sha], key=key)
        blobs: dict[str, str] = {}
        for entry in out.split("\0"):
            meta, _, path = entry.partition("\t")
            parts = meta.split()
            if path and len(parts) == 3 and parts[1] == "blob":
                blobs[path] = parts[2]
        return blobs

    def is_ancestor(self, key: str, a: str, b: str) -> bool:
        return self.runner.run(["git", "--git-dir", str(self.repo_dir(key)), "merge-base", "--is-ancestor", a, b],
                               timeout=self.timeout, env=git_env()).returncode == 0

    def ls_tree(self, key: str, sha: str) -> set[str]:
        out = self.git(["ls-tree", "-r", "--name-only", "-z", sha], key=key)
        return {p for p in out.split("\0") if p}

    def log_subject(self, key: str, sha: str, fmt: str = "%s") -> str:
        return self.git(["log", "-1", f"--format={fmt}", sha], key=key, check=False).strip()

    def log_range(self, key: str, a: str, b: str, limit: int = 30) -> str:
        return self.git(["log", f"--max-count={limit}", "--format=- %s", f"{a}..{b}"], key=key, check=False).strip()

    def worktree(self, key: str, sha: str) -> Path:
        if not SHA_RE.match(sha):
            raise SourceError(f"refusing worktree for non-SHA {sha!r}")
        path = self.home / "worktrees" / key / sha
        with self._lock:
            idle = self._idle.pop(path, None) is not None  # held worktrees are in use: never reset under a reader
            if not ((path / ".git").exists() and (not idle or self._reset(key, path, sha))):
                spare = next((p for p, (k, _) in self._idle.items() if k == key), None)
                if spare is None or not self._recycle(key, spare, path, sha):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    self.git(["worktree", "prune"], key=key, check=False)
                    self.git(["worktree", "add", "--detach", "--force", "--quiet", str(path), sha], key=key)
            self._holds[path] = self._holds.get(path, 0) + 1
        return path

    def _reset(self, key: str, path: Path, sha: str) -> bool:
        """Make an idle worktree hold exactly the files of a fresh checkout of `sha`: forced checkout (only files that
        differ are rewritten), then every untracked / ignored file removed. Hooks stay disabled. False: removed."""
        try:
            self.git(["checkout", "--detach", "--force", "--quiet", sha], cwd=str(path))
            self.git(["clean", "-ffdxq"], cwd=str(path))
            return True
        except SourceError:
            self._remove(key, path)
            return False

    def _recycle(self, key: str, spare: Path, path: Path, sha: str) -> bool:
        """Move an idle worktree of another commit to `path` and reset it to `sha`. False: removed instead."""
        del self._idle[spare]
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.git(["worktree", "move", str(spare), str(path)], key=key)
        except SourceError:
            self._remove(key, spare)
            return False
        return self._reset(key, path, sha)

    def release_worktree(self, key: str, sha: str) -> None:
        """Drop one hold; after the last one the checkout stays idle for reuse (removed after `idle_ttl`, or at once
        beyond MAX_IDLE per repository; the bare cache keeps every commit)."""
        path = self.home / "worktrees" / key / sha
        with self._lock:
            left = self._holds.get(path, 0) - 1
            if left > 0:
                self._holds[path] = left
                return
            self._holds.pop(path, None)
            if self.idle_ttl <= 0 or not (path / ".git").exists():
                self._remove(key, path)
                return
            self._idle[path] = (key, time.monotonic())
            for old in [p for p, (k, _) in self._idle.items() if k == key][:-MAX_IDLE]:  # oldest first
                del self._idle[old]
                self._remove(key, old)
        timer = threading.Timer(self.idle_ttl + 1, self.remove_expired)
        timer.daemon = True
        timer.start()

    def remove_expired(self, now: float | None = None) -> list[Path]:
        """Remove the worktrees idle for longer than `idle_ttl`."""
        now = time.monotonic() if now is None else now
        with self._lock:
            gone = [(p, k) for p, (k, since) in self._idle.items() if now - since >= self.idle_ttl]
            for p, k in gone:
                del self._idle[p]
                self._remove(k, p)
        return [p for p, _ in gone]

    def _remove(self, key: str, path: Path) -> None:
        self.git(["worktree", "remove", "--force", str(path)], key=key, check=False)
        self.git(["worktree", "prune"], key=key, check=False)
        try:
            path.parent.rmdir()  # worktrees/<key>/ once empty
        except OSError:
            pass

    def prune_stale_worktrees(self) -> list[Path]:
        """At server start: remove worktrees left by earlier runs. Only paths under $HOME/worktrees that are
        registered worktrees of one of our bare caches are touched; anything else is left alone."""
        root = (self.home / "worktrees").resolve()
        removed: list[Path] = []
        for repo in sorted((self.home / "repos").glob("*.git")):
            key = repo.name.removesuffix(".git")
            out = self.git(["worktree", "list", "--porcelain"], key=key, check=False)
            for line in out.splitlines():
                if not line.startswith("worktree "):
                    continue
                p = Path(line.removeprefix("worktree ")).resolve()
                with self._lock:
                    held = {h.resolve() for h in self._holds}
                    if p.is_relative_to(root) and p != root and p not in held:
                        self._remove(key, p)
                        removed.append(p)
        return removed

