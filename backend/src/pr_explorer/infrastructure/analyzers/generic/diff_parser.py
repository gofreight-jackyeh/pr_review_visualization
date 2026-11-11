"""Unified diff (git diff output) → ChangedFile list."""

from __future__ import annotations

import re

from ....domain.model.changes import ChangedFile, Hunk
from ....domain.model.ids import file_id
from .languages import detect_language

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _unquote(p: str) -> str:
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
    return p


def _strip_prefix(p: str) -> str | None:
    p = _unquote(p.strip())
    if p == "/dev/null":
        return None
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_unified_diff(text: str) -> list[ChangedFile]:
    files: list[ChangedFile] = []
    cur: dict | None = None

    def flush() -> None:
        if cur is None:
            return
        path = cur["new"] or cur["old"]
        if not path:
            return
        status = cur["status"]
        old = cur["old"] if status == "renamed" else None
        files.append(
            ChangedFile(
                id=file_id(path), path=path, status=status, language=detect_language(path),
                additions=cur["add"], deletions=cur["del"], hunks=cur["hunks"], old_path=old, is_binary=cur["binary"],
            )
        )

    for line in text.splitlines():
        if line.startswith("diff --git "):
            flush()
            m = re.match(r'^diff --git (".*?"|\S+) (".*?"|\S+)$', line)
            a = _strip_prefix(m.group(1)) if m else None
            b = _strip_prefix(m.group(2)) if m else None
            rest = line[len("diff --git "):]
            half = (len(rest) - 1) // 2
            if m is None and rest.startswith("a/") and rest[half + 1:].startswith("b/") and rest[2:half] == rest[half + 3:]:
                a, b = rest[2:half], rest[half + 3:]  # unquoted path with spaces and no ---/+++ lines (binary, mode-only)
            cur = {"old": a, "new": b, "status": "modified", "add": 0, "del": 0, "hunks": [], "binary": False, "in_hunk": False}
            continue
        if cur is None:
            continue
        if not cur["in_hunk"] or not line[:1] in ("+", "-", " ", "\\"):
            if line.startswith("new file mode"):
                cur["status"] = "added"
            elif line.startswith("deleted file mode"):
                cur["status"] = "removed"
            elif line.startswith("rename from "):
                cur["status"], cur["old"] = "renamed", _unquote(line[len("rename from "):])
            elif line.startswith("rename to "):
                cur["new"] = _unquote(line[len("rename to "):])
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur["binary"] = True
            elif line.startswith("--- "):
                cur["old"] = _strip_prefix(line[4:]) or cur["old"]
                if _strip_prefix(line[4:]) is None:
                    cur["status"] = "added"
                cur["in_hunk"] = False
                continue
            elif line.startswith("+++ "):
                new = _strip_prefix(line[4:])
                if new is None:
                    cur["status"] = "removed"
                else:
                    cur["new"] = new
                continue
        m = HUNK.match(line)
        if m:
            cur["in_hunk"] = True
            cur["hunks"].append(
                Hunk(int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1))
            )
            continue
        if cur["in_hunk"]:
            if line.startswith("+"):
                cur["add"] += 1
            elif line.startswith("-"):
                cur["del"] += 1
    flush()
    for f in files:
        if f.status == "removed":
            f.path = f.old_path or f.path
            f.id = file_id(f.path)
    return files
