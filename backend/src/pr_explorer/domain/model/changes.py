from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int


@dataclass
class ChangedFile:
    id: str
    path: str
    status: str  # added | modified | removed | renamed
    language: str
    additions: int
    deletions: int
    hunks: list[Hunk] = field(default_factory=list)  # internal
    old_path: str | None = None
    is_binary: bool = False  # internal
    capability_id: str | None = None  # null when the file belongs to no capability (serialized as null)

    @property
    def side_path(self) -> tuple[str, str]:
        """(side, path) where this file can be read."""
        return ("base", self.old_path or self.path) if self.status == "removed" else ("head", self.path)


@dataclass
class Coverage:
    language: str
    level: str  # deep | generic | unsupported
    file_count: int
    notes: list[str] = field(default_factory=list)
