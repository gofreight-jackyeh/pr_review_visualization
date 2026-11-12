"""Both documents produced from the fixture satisfy the shared v2 schema AND referential integrity — inside each
document and across them (overlay → system map) — and follow the frontend's shape conventions."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from pr_explorer.domain.model.document import SCHEMA_VERSION
from pr_explorer.infrastructure.persistence.schema_validator import (
    DocumentValidationError, find_schema_path, overlay_integrity, schema_errors, system_map_integrity, validate_overlay,
    validate_system_map,
)

FRONTEND_FIXTURES = Path(__file__).resolve().parents[3] / "frontend" / "src" / "fixtures"


def _files(repo: Path, ov: dict) -> dict[str, set[str]]:
    def ls(sha):
        out = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", sha], capture_output=True, text=True, check=True)
        return set(out.stdout.split())

    return {"base": ls(ov["pr"]["merge_base_sha"]), "head": ls(ov["pr"]["head_sha"])}


def test_schema_is_the_shared_v2_file():
    assert find_schema_path().parts[-4:] == ("schemas", "artifact", "v2", "artifact.schema.json")


def test_fixture_documents_are_valid(shop_docs, shop_repo):
    smap, ov, paths, _ = shop_docs
    files = _files(shop_repo, ov)
    assert schema_errors(smap) == [] and schema_errors(ov) == []
    assert system_map_integrity(smap, files["base"]) == []
    assert overlay_integrity(ov, smap, files) == []
    suffix = f".v{SCHEMA_VERSION}.json"
    assert Path(paths["system_map"]).name.endswith(suffix) and Path(paths["overlay"]).name.endswith(suffix)


def test_every_statement_and_element_is_traceable(shop_docs):
    smap, ov, _, _ = shop_docs
    evidence = {e["id"] for e in smap["evidence"]} | {e["id"] for e in ov["evidence"]}

    def walk(v):
        if isinstance(v, dict):
            if {"text", "source", "evidence_refs"} <= v.keys():
                assert v["evidence_refs"] and set(v["evidence_refs"]) <= evidence, v
            for k, x in v.items():
                if k == "evidence_refs":
                    assert set(x) <= evidence
                else:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(smap)
    walk(ov)
    for f in ov["findings"]:
        assert f["changed_file_ids"] and f["refs"] and f["evidence_refs"]


def _ov(d):
    return d[1]


@pytest.mark.parametrize("target,mutate", [
    ("map", lambda m, o: m["relations"][0].__setitem__("target", "cap:does-not-exist")),
    ("map", lambda m, o: m["flows"][0]["timeline"]["steps"][0].__setitem__("capability_id", "cap:nope")),
    ("map", lambda m, o: m["entities"][0]["evidence_refs"].append("ev:missing")),
    ("map", lambda m, o: m["capabilities"][0].__setitem__("surprise", 1)),  # schema: no extra fields
    ("overlay", lambda m, o: o["impacts"][0].__setitem__("target_id", "cap:not-in-the-map")),
    ("overlay", lambda m, o: o["flow_changes"][0]["steps"][0].__setitem__("entity_id", "entity:not-in-the-map")),
    ("overlay", lambda m, o: o["flow_changes"][0].__setitem__("flow_id", "flow:not-in-the-map")),
    ("overlay", lambda m, o: o.__setitem__("system_map_id", "sm_other")),
    ("overlay", lambda m, o: o["findings"][0]["capability_ids"].append("cap:ghost")),
    ("overlay", lambda m, o: o["purpose"].__setitem__("evidence_refs", [])),  # schema: Text must cite evidence
    ("overlay", lambda m, o: o["flow_changes"][0]["steps"][0]["evidence_refs"].__setitem__(0, "ev:only-in-another-map")),
    ("overlay", lambda m, o: o["findings"][0]["refs"][0].__setitem__("path", "nope/missing.py")),
])
def test_invalid_documents_are_rejected_loudly(shop_docs, shop_repo, target, mutate):
    smap, ov, _, _ = shop_docs
    m, o = copy.deepcopy(smap), copy.deepcopy(ov)
    mutate(m, o)
    files = _files(shop_repo, ov)
    with pytest.raises(DocumentValidationError):
        if target == "map":
            validate_system_map(m, files["base"])
        else:
            validate_overlay(o, smap, files)


def test_overlay_is_only_valid_against_its_own_map(shop_docs, shop_repo):
    smap, ov, _, _ = shop_docs
    other = copy.deepcopy(smap)
    other["capabilities"] = [c for c in other["capabilities"] if c["id"] != "cap:orders"]
    errs = overlay_integrity(ov, other, _files(shop_repo, ov))
    assert any("unknown capabilit" in e and "cap:orders" in e for e in errs)


@pytest.mark.parametrize("kind", ["system-map", "overlay"])
def test_shapes_follow_the_frontend_fixtures(shop_docs, kind):
    """Same top-level keys and element field names as the hand-made samples the finished frontend was built on."""
    smap, ov, _, _ = shop_docs
    ours = smap if kind == "system-map" else ov
    sample = json.loads((FRONTEND_FIXTURES / f"sample-{kind}.json").read_text())
    optional = {"design_rationale"}
    assert set(sample) - optional <= set(ours) <= set(sample) | optional
    for key, val in sample.items():
        if isinstance(val, list) and val and isinstance(val[0], dict) and ours.get(key):
            sample_keys = set().union(*(x.keys() for x in val))
            our_keys = set().union(*(x.keys() for x in ours[key]))
            assert our_keys <= sample_keys | {"language", "component_count", "mode_reason", "old_path", "business_impact",
                                              "rule", "flow_ids", "doc_ref", "impact_depth", "end_line", "before", "notes",
                                              "description"}, (key, our_keys - sample_keys)


def test_frontend_model_invariants(shop_docs):
    """What frontend/src/entities/artifact/artifact.test.ts checks with buildModel(map, overlay)."""
    smap, ov, _, _ = shop_docs
    ha = ov["head_additions"]
    by_id = {x["id"] for k in ("capabilities", "entities", "entry_points", "components", "flows") for x in smap[k] + ha[k]}
    evidence = {e["id"] for e in smap["evidence"] + ov["evidence"]}
    flows = {f["id"] for f in smap["flows"] + ha["flows"]}
    for i in ov["impacts"]:
        assert i["target_id"] in by_id and set(i["evidence_refs"]) <= evidence
    for fc in ov["flow_changes"]:
        assert fc["flow_id"] in flows
        for s in fc["steps"]:
            assert s["capability_id"] in by_id and set(s["evidence_refs"]) <= evidence
    for r in smap["relations"] + ha["relations"]:
        assert r["source"] in by_id and r["target"] in by_id


def _mutations(doc: dict, limit: int = 50):
    """Deterministic broken copies: each drops a key, swaps a value's type or adds an unknown key somewhere."""
    spots: list[tuple[dict, str]] = []

    def walk(v):
        if isinstance(v, dict):
            spots.extend((v, k) for k in v)
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(doc)
    step = max(1, len(spots) // limit)
    for i, (holder, key) in enumerate(spots[::step][:limit]):
        kind = i % 3
        orig = holder[key]
        if kind == 0:
            del holder[key]
        elif kind == 1:
            holder[key] = 7 if isinstance(orig, str) else "x"
        else:
            holder["zz_unknown"] = 1
        yield doc
        if kind == 2:
            del holder["zz_unknown"]
        holder[key] = orig  # dict order may change; schema errors do not depend on it


def test_inlined_schema_reports_exactly_the_original_errors(shop_docs):
    """The validator runs on a $ref-inlined copy of the schema (speed); valid and broken documents must give the same
    error list as the schema as written."""
    from jsonschema import Draft202012Validator

    from pr_explorer.infrastructure.persistence.schema_validator import _validator

    original = Draft202012Validator(json.loads(find_schema_path().read_text("utf-8")))
    fast = _validator(str(find_schema_path()))
    assert fast.schema != original.schema

    def errs(v, doc):
        return [(list(e.absolute_path), e.message) for e in v.iter_errors(doc)]

    smap, ov, _, _ = shop_docs
    broken = 0
    for doc in (copy.deepcopy(smap), copy.deepcopy(ov)):
        assert errs(fast, doc) == errs(original, doc) == []
        for bad in _mutations(doc):
            want = errs(original, bad)
            assert errs(fast, bad) == want
            broken += bool(want)
    assert broken > 60
