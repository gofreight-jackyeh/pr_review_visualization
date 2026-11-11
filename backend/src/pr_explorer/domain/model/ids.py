"""Deterministic, content-derived IDs. Identical across base/head and across PRs so layouts stay stable."""

from __future__ import annotations

import hashlib


def short_hash(*parts: object, length: int = 10) -> str:
    raw = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def module_id(lang: str, path: str) -> str:
    return f"node:{lang}:{path}"


def symbol_id(lang: str, path: str, qualname: str) -> str:
    return f"node:{lang}:{path}::{qualname}"


def route_id(path: str, pattern: str, index: int = 0) -> str:
    suffix = f"#{index}" if index else ""
    return f"node:route:{path}::{pattern}{suffix}"


def external_id(key: str) -> str:
    return f"node:ext:{key}"


def edge_id(kind: str, source: str, target: str) -> str:
    return f"edge:{kind}:{source}->{target}"


def file_id(path: str) -> str:
    return f"file:{path}"


def evidence_id(kind: str, side: str, path: str, line: int, *discriminator: object) -> str:
    return f"ev:{kind}:{side}:{path}:{line}:{short_hash(*discriminator, length=8)}"


# ---- system map element IDs (schema v2) ----

def capability_id(path: str) -> str:
    return f"cap:{path}"


def entity_id(node_id: str) -> str:
    return "entity:" + node_id.removeprefix("node:")


def entry_point_id(fact_id: str) -> str:
    """Independent of the entry-point kind, so a route that becomes a DRF API keeps its identity."""
    return "ep:" + fact_id.removeprefix("node:")


def flow_id(entry_point: str) -> str:
    return "flow:" + entry_point.removeprefix("ep:")


def relation_id(kind: str, source: str, target: str) -> str:
    return f"rel:{kind}:{source}->{target}"


def step_id(flow: str, key: object, occurrence: int) -> str:
    return f"step:{short_hash(flow, key, occurrence, length=14)}"
