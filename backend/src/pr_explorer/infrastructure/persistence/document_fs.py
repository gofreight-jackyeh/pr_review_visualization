"""Versioned document JSON files: $PR_EXPLORER_HOME/system-maps/<id>.v<schema>.json and overlays/<id>.v<schema>.json.

Every document is scrubbed for secrets, then validated (schema + referential integrity; an overlay also against
its system map) before it is written atomically. Invalid documents are rejected loudly (ValueError)."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from ..security.secret_scrubber import scrub_tree
from .document_serializer import to_json
from .schema_validator import validate_overlay, validate_system_map

ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
NO_SCRUB = frozenset({"base_sha", "head_sha", "merge_base_sha", "commit_sha"})


class FsDocumentStore:
    def __init__(self, home: Path):
        self.dirs = {"system_map": home / "system-maps", "pr_overlay": home / "overlays"}

    def _path(self, kind: str, doc_id: str, schema_version: str) -> Path:
        if not ID_RE.match(doc_id):
            raise ValueError(f"bad document id {doc_id!r}")
        return self.dirs[kind] / f"{doc_id}.v{schema_version}.json"

    def _write(self, kind: str, doc_id: str, doc: dict) -> Path:
        path = self._path(kind, doc_id, doc["schema_version"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")  # unique: concurrent runs may write one id
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, path)
        return path

    def save_system_map(self, smap, files: set[str]) -> Path:
        doc = scrub_tree(to_json(smap), NO_SCRUB)
        validate_system_map(doc, files)
        return self._write("system_map", smap.system_map_id, doc)

    def save_overlay(self, overlay, system_map: dict, files: dict[str, set[str]]) -> Path:
        doc = scrub_tree(to_json(overlay), NO_SCRUB)
        validate_overlay(doc, system_map, files)
        return self._write("pr_overlay", overlay.overlay_id, doc)

    def _load(self, kind: str, doc_id: str) -> dict | None:
        if not ID_RE.match(doc_id):
            return None
        matches = sorted(self.dirs[kind].glob(f"{doc_id}.v*.json"))
        return json.loads(matches[-1].read_text("utf-8")) if matches else None

    def load_system_map(self, system_map_id: str) -> dict | None:
        return self._load("system_map", system_map_id)

    def load_overlay(self, overlay_id: str) -> dict | None:
        return self._load("pr_overlay", overlay_id)
