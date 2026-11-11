"""The one way to read an analyzed repository's files from disk. The PR under review is untrusted input.

A PR can commit a symlink (`x.py -> /etc/...` or `-> /dev/zero`) or a path under a symlinked directory; reading it
would pull files from outside the repository into the analysis (and the Claude prompt) or hang on an endless device.
So: no symlink as the final component, the resolved path must stay inside the root, only regular files are read
(checked on the opened descriptor, opened non-blocking so a FIFO cannot stall), and at most `max_bytes` are read.
Anything refused reads as missing (None) — callers already treat missing files as unknown structure.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def read_repo_bytes(root: Path, rel: str, max_bytes: int) -> bytes | None:
    fp = root / rel
    try:
        if fp.is_symlink() or not fp.resolve().is_relative_to(root.resolve()):
            return None
        fd = os.open(fp, _FLAGS)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return None
            return f.read(max_bytes)
    except (OSError, ValueError):
        return None


def read_repo_text(root: Path, rel: str, max_bytes: int) -> str | None:
    raw = read_repo_bytes(root, rel, max_bytes)
    return None if raw is None else raw.decode("utf-8", "replace")
