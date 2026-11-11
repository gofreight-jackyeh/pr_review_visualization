"""JSON Schema v2 validation + referential integrity, within one document and across documents
(an overlay may cite anything its system map lists, plus its own head_additions and evidence)."""

from __future__ import annotations

import json
import os
from collections import Counter
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_REL = Path("schemas/artifact/v2/artifact.schema.json")
ELEMENT_KEYS = ("capabilities", "entities", "entry_points", "components", "relations", "flows", "interfaces")


class DocumentValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__(f"document invalid ({len(errors)} problem(s)): " + "; ".join(errors[:10]))
        self.errors = errors


def find_schema_path() -> Path:
    env = os.environ.get("PR_EXPLORER_SCHEMA")
    if env:
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        cand = parent / SCHEMA_REL
        if cand.is_file():
            return cand
    raise FileNotFoundError(f"cannot locate {SCHEMA_REL}; set PR_EXPLORER_SCHEMA")


@lru_cache(maxsize=4)
def _validator(path: str) -> Draft202012Validator:
    schema = json.loads(Path(path).read_text("utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def schema_errors(doc: dict, schema_path: Path | None = None) -> list[str]:
    v = _validator(str(schema_path or find_schema_path()))
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:200]}" for e in v.iter_errors(doc)]


class _Pools:
    def __init__(self) -> None:
        self.ids: dict[str, set[str]] = {k: set() for k in ELEMENT_KEYS}
        self.evidence: set[str] = set()
        self.errs: list[str] = []

    def add(self, part: dict, where: str) -> None:
        for k in ELEMENT_KEYS:
            ids = [x["id"] for x in part.get(k, [])]
            dup = (set(ids) & self.ids[k]) | {i for i, n in Counter(ids).items() if n > 1}
            if dup:
                self.errs.append(f"{where}.{k}: duplicate id(s) {', '.join(sorted(dup)[:3])}")
            self.ids[k] |= set(ids)

    def need(self, kind: str, ident: str | None, where: str) -> None:
        if ident is not None and ident not in self.ids[kind]:
            self.errs.append(f"{where}: unknown {kind[:-1] if kind.endswith('s') else kind} {ident}")

    def needs(self, kind: str, idents, where: str) -> None:
        for i in idents or []:
            self.need(kind, i, where)


def _check_steps(p: _Pools, steps: list[dict], where: str) -> None:
    ids = [s["id"] for s in steps]
    if len(ids) != len(set(ids)):
        p.errs.append(f"{where}: duplicate step ids")
    for s in steps:
        w = f"{where} step {s['id']}"
        p.need("capabilities", s["capability_id"], w)
        p.need("entities", s.get("entity_id"), w)
        p.needs("components", s["component_ids"], w)


def _check_phases(p: _Pools, phases: list[dict] | None, steps: list[dict], where: str) -> None:
    """Phases cut the step order: concatenated, they are exactly the steps (spec v2 §5 Layer 7)."""
    if phases and [x for ph in phases for x in ph["step_ids"]] != [s["id"] for s in steps]:
        p.errs.append(f"{where}: phases do not cover the steps contiguously, in order, once each")


def _check_elements(p: _Pools, part: dict, where: str, domains: set[str] | None = None) -> None:
    for c in part.get("capabilities", []):
        p.needs("entities", c["entity_ids"], f"{where} capability {c['id']}")
        p.needs("entry_points", c["entry_point_ids"], f"{where} capability {c['id']}")
        p.need("capabilities", c.get("parent_capability_id"), f"{where} capability {c['id']} parent")
        if domains is not None and c.get("domain_id") is not None and c["domain_id"] not in domains:
            p.errs.append(f"{where} capability {c['id']}: unknown domain {c['domain_id']}")
    for e in part.get("entities", []):
        p.need("capabilities", e["capability_id"], f"{where} entity {e['id']}")
    for ep in part.get("entry_points", []):
        p.need("capabilities", ep["capability_id"], f"{where} entry point {ep['id']}")
        p.need("components", ep.get("handler_component_id"), f"{where} entry point {ep['id']}")
    for c in part.get("components", []):
        p.need("capabilities", c["capability_id"], f"{where} component {c['id']}")
    for r in part.get("relations", []):
        p.need("capabilities", r["source"], f"{where} relation {r['id']}")
        p.need("entities" if r["kind"] == "uses_entity" else "capabilities", r["target"], f"{where} relation {r['id']}")
    for i in part.get("interfaces", []):
        p.need("capabilities", i["capability_id"], f"{where} interface {i['id']}")
        p.need("entry_points", i.get("entry_point_id"), f"{where} interface {i['id']}")
    for f in part.get("flows", []):
        w = f"{where} flow {f['id']}"
        p.need("entry_points", f["entry_point_id"], w)
        p.needs("capabilities", f["capability_ids"], w)
        p.needs("entities", f["entity_ids"], w)
        _check_steps(p, f["timeline"]["steps"], w)
        _check_phases(p, f.get("phases"), f["timeline"]["steps"], w)


def _walk_refs(p: _Pools, doc: dict, files: dict[str, set[str]] | None, allowed_sides: set[str]) -> None:
    """Every evidence_refs entry resolves; every CodeRef points at a file that exists on its side."""
    def walk(v, where: str) -> None:
        if isinstance(v, dict):
            if {"side", "path", "start_line"} <= v.keys() and isinstance(v.get("path"), str):
                if v["side"] not in allowed_sides:
                    p.errs.append(f"{where}: CodeRef side {v['side']} not allowed in this document")
                elif files is not None and v["path"] not in files.get(v["side"], set()):
                    p.errs.append(f"{where}: CodeRef {v['side']}:{v['path']} does not exist on that side")
                return
            for k, x in v.items():
                if k == "evidence_refs" and isinstance(x, list):
                    for e in x:
                        if e not in p.evidence:
                            p.errs.append(f"{where}: unknown evidence {e}")
                else:
                    walk(x, f"{where}.{k}")
        elif isinstance(v, list):
            for x in v:
                walk(x, where)
    walk(doc, "$")


def interface_change_convention(ic: dict) -> list[str]:
    """Spec §8 (enforced in code, not in the schema): added ⇒ compatible; removed ⇒ breaking or unknown."""
    if ic["change"] == "added" and ic["compatibility"] != "compatible":
        return [f"interface change 'added' must be 'compatible', got '{ic['compatibility']}'"]
    if ic["change"] == "removed" and ic["compatibility"] not in ("breaking", "unknown"):
        return [f"interface change 'removed' must be 'breaking' or 'unknown', got '{ic['compatibility']}'"]
    return []


def system_map_integrity(doc: dict, files: set[str] | None = None) -> list[str]:
    p = _Pools()
    p.add(doc, "$")
    ev = [e["id"] for e in doc["evidence"]]
    if len(ev) != len(set(ev)):
        p.errs.append("duplicate evidence ids")
    p.evidence = set(ev)
    domains = [d["id"] for d in doc["domains"]]
    if len(domains) != len(set(domains)):
        p.errs.append("duplicate domain ids")
    _check_elements(p, doc, "$", set(domains))
    rules = [r["id"] for r in doc["architecture_rules"]]
    if len(rules) != len(set(rules)):
        p.errs.append("duplicate architecture rule ids")
    for r in doc["architecture_rules"]:
        p.needs("capabilities", r["scope_capability_ids"], f"architecture rule {r['id']}")
    b = doc["flow_budget"]
    if b["entry_points_total"] != len(doc["entry_points"]) or b["flows_built"] > len(doc["flows"]):
        p.errs.append("flow_budget does not match entry_points / flows")
    _walk_refs(p, doc, {"base": files} if files is not None else None, {"base"})
    return p.errs


def overlay_integrity(doc: dict, system_map: dict, files: dict[str, set[str]] | None = None) -> list[str]:
    p = _Pools()
    if doc["system_map_id"] != system_map["system_map_id"]:
        p.errs.append(f"system_map_id {doc['system_map_id']} does not name the given map {system_map['system_map_id']}")
    if doc["pr"]["merge_base_sha"] != system_map["commit_sha"]:
        p.errs.append("pr.merge_base_sha differs from the system map's commit_sha")
    p.add(system_map, "map")
    p.add(doc["head_additions"], "head_additions")
    own = [e["id"] for e in doc["evidence"]]
    if len(own) != len(set(own)):
        p.errs.append("duplicate evidence ids")
    p.evidence = {e["id"] for e in system_map["evidence"]} | set(own)
    _check_elements(p, doc["head_additions"], "head_additions", {d["id"] for d in system_map.get("domains", [])})
    files_ids = {f["id"] for f in doc["changes"]["files"]}
    for f in doc["changes"]["files"]:
        p.need("capabilities", f["capability_id"], f"changed file {f['id']}")
    kinds = {"capability": "capabilities", "entity": "entities", "entry_point": "entry_points",
             "component": "components", "flow": "flows"}
    seen: set[str] = set()
    for i in doc["impacts"]:
        p.need(kinds[i["target_kind"]], i["target_id"], "impact")
        if i["target_id"] in seen:
            p.errs.append(f"impact: {i['target_id']} listed twice")
        seen.add(i["target_id"])
    for fc in doc["flow_changes"]:
        p.need("flows", fc["flow_id"], "flow_change")
        _check_steps(p, fc["steps"], f"flow_change {fc['flow_id']}")
        _check_phases(p, fc.get("phases"), fc["steps"], f"flow_change {fc['flow_id']}")
    rules = {r["id"] for r in system_map.get("architecture_rules", [])}
    checks = [c["id"] for c in doc["architecture_checks"]]
    if len(checks) != len(set(checks)):
        p.errs.append("duplicate architecture check ids")
    for c in doc["architecture_checks"]:
        w = f"architecture check {c['id']}"
        if c.get("rule_id") is not None and c["rule_id"] not in rules:
            p.errs.append(f"{w}: unknown rule {c['rule_id']}")
        p.needs("capabilities", c["capability_ids"], w)
        if c.get("violating_relation"):
            p.needs("capabilities", [c["violating_relation"]["source"], c["violating_relation"]["target"]], w)
        ic = c.get("interface_change")
        if ic:
            p.need("interfaces", ic["interface_id"], w)
            p.errs += [f"{w}: {e}" for e in interface_change_convention(ic)]
    for bc in doc["behavior_changes"]:
        p.needs("flows", bc["flow_ids"], f"behavior_change {bc['id']}")
        p.needs("capabilities", bc["capability_ids"], f"behavior_change {bc['id']}")
    finding_ids = set()
    for f in doc["findings"]:
        w = f"finding {f['id']}"
        finding_ids.add(f["id"])
        for x in f["changed_file_ids"]:
            if x not in files_ids:
                p.errs.append(f"{w}: unknown changed file {x}")
        p.needs("capabilities", f["capability_ids"], w)
        p.needs("flows", f.get("flow_ids"), w)
    for item in doc["walkthrough"]["steps"] + doc["walkthrough"]["questions"]:
        t, w = item["target"], f"walkthrough {item['id']}"
        p.needs("capabilities", t.get("capability_ids"), w)
        p.need("flows", t.get("flow_id"), w)
        if t.get("finding_id") is not None and t["finding_id"] not in finding_ids:
            p.errs.append(f"{w}: unknown finding {t['finding_id']}")
    _walk_refs(p, doc, files, {"base", "head"})
    return p.errs


def validate_system_map(doc: dict, files: set[str] | None = None) -> None:
    errs = schema_errors(doc)
    if not errs and doc.get("kind") != "system_map":
        errs = ["not a system_map document"]
    if not errs:
        errs = system_map_integrity(doc, files)
    if errs:
        raise DocumentValidationError(errs)


def validate_overlay(doc: dict, system_map: dict, files: dict[str, set[str]] | None = None) -> None:
    errs = schema_errors(doc)
    if not errs and doc.get("kind") != "pr_overlay":
        errs = ["not a pr_overlay document"]
    if not errs:
        errs = overlay_integrity(doc, system_map, files)
    if errs:
        raise DocumentValidationError(errs)
