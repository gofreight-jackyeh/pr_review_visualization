"""Domain dataclasses → schema v2 JSON. Drops None optionals and internal analysis fields."""

from __future__ import annotations

import dataclasses
from typing import Any

# Internal fields never written: analyzer facts (meta), diff internals (hunks, is_binary), graph node ids behind
# findings (node_ids, mapped to capability_ids/flow_ids instead), CodeRef.symbol.
INTERNAL_FIELDS = {"meta", "hunks", "is_binary", "node_ids", "symbol"}
KEEP_NULL = {"capability_id"}  # ChangedFile.capability_id is null when a file belongs to no capability


def to_json(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out = {}
        for f in dataclasses.fields(value):
            if f.name in INTERNAL_FIELDS:
                continue
            v = getattr(value, f.name)
            if v is None and f.name not in KEEP_NULL:
                continue
            out[f.name] = to_json(v)
        return out
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, set):
        return sorted(to_json(v) for v in value)
    if isinstance(value, dict):
        return {k: to_json(v) for k, v in value.items()}
    return value
