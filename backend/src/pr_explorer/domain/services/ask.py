"""Follow-up questions about an analyzed PR (pure). Two halves, both over the stored documents (JSON dicts):

- `ask_digest`: what Claude sees for one question — a compact digest of the overlay, the bounded PR diff, the item the
  reviewer has selected and the last turns of this overlay's Q&A. All of it ends up inside the prompt's untrusted fence.
- `gate_answer`: the evidence gate for the reply. Cites must be IDs of that overlay / its system map (or a line of a
  changed file); unknown ones are dropped. An answer with no surviving cite is kept but marked `unsupported`.
"""

from __future__ import annotations

import json
from typing import Any

from ..model.ids import parse_file_cite
from .semantic_digest import code_first

QUESTION_MAX = 2000
HISTORY_TURNS = 6
HISTORY_ANSWER_CHARS = 1500
DIGEST_BUDGET = 40_000  # characters of digest JSON (without the diff); lists are trimmed from the end to fit
DIFF_BUDGET = 30_000  # characters of unified diff, code before tests (prompt size drives reply time)
ANSWER_MAX = 8000
MAX_CITES = 12
MAX_REFS = 4
LIST_CAPS = {"behavior_changes": 20, "findings": 30, "architecture_checks": 30, "flows_touched": 12, "changed_files": 120,
             "capabilities": 60}
STEPS_PER_FLOW = 25


def _t(v: Any) -> str | None:
    """Text of a stored Text object ({text, source, evidence_refs}) or None."""
    return v.get("text") if isinstance(v, dict) and isinstance(v.get("text"), str) else None


def _name(el: dict) -> str:
    return _t(el.get("business_name")) or el.get("technical_name") or el.get("id", "")


def _clip(s: str | None, n: int) -> str | None:
    return s if s is None or len(s) <= n else s[: n - 1] + "…"


def _elements(overlay: dict, smap: dict) -> dict[str, tuple[str, dict]]:
    """id -> (kind, element) for every map element plus the overlay's head-only additions."""
    out: dict[str, tuple[str, dict]] = {}
    ha = overlay.get("head_additions") or {}
    for key, kind in (("capabilities", "capability"), ("entities", "entity"), ("entry_points", "entry_point"),
                      ("components", "component"), ("flows", "flow"), ("interfaces", "interface")):
        for el in [*smap.get(key, []), *ha.get(key, [])]:
            out.setdefault(el["id"], (kind, el))
    return out


def _diff_evidence(overlay: dict) -> dict[str, str]:
    """changed file path -> the id of its diff evidence."""
    out: dict[str, str] = {}
    for e in overlay.get("evidence", []):
        if e.get("kind") == "diff":
            for r in e.get("refs", []):
                out.setdefault(r["path"], e["id"])
    return out


def _steps(overlay: dict) -> dict[str, tuple[str, dict]]:
    return {s["id"]: (fc["flow_id"], s) for fc in overlay.get("flow_changes", []) for s in fc.get("steps", [])}


def selected_item(overlay: dict, smap: dict, selected_id: str | None) -> dict | None:
    """The reviewer's current selection as a compact record, or None when the id is unknown."""
    if not selected_id:
        return None
    for f in overlay.get("findings", []):
        if f["id"] == selected_id:
            return {"id": f["id"], "kind": "finding", "title": f.get("title"), "description": _clip(f.get("description"), 600),
                    "status": f.get("status"), "evidence_refs": f.get("evidence_refs", [])[:4]}
    for a in overlay.get("architecture_checks", []):
        if a["id"] == selected_id:
            return {"id": a["id"], "kind": "architecture_check", "title": a.get("title"), "check": a.get("check"),
                    "verdict": a.get("verdict"), "status": a.get("status"), "description": _clip(a.get("description"), 600),
                    "evidence_refs": a.get("evidence_refs", [])[:4]}
    for b in overlay.get("behavior_changes", []):
        if b["id"] == selected_id:
            return {"id": b["id"], "kind": "behavior_change", "before": _t(b.get("before")), "after": _t(b.get("after")),
                    "flow_ids": b.get("flow_ids", [])}
    for f in overlay.get("changes", {}).get("files", []):
        if f["id"] == selected_id:
            return {"id": f["id"], "kind": "changed_file", "path": f["path"], "status": f.get("status"),
                    "capability_id": f.get("capability_id"), "evidence_id": _diff_evidence(overlay).get(f["path"])}
    st = _steps(overlay).get(selected_id)
    if st:
        flow, s = st
        return {"id": s["id"], "kind": "step", "flow_id": flow, "label": _t(s.get("business_label")) or s.get("technical_label"),
                "step_change_status": s.get("step_change_status"), "evidence_refs": s.get("evidence_refs", [])[:4]}
    el = _elements(overlay, smap).get(selected_id)
    if el:
        kind, e = el
        return {"id": e["id"], "kind": kind, "name": _name(e), "technical_name": e.get("technical_name"),
                "description": _clip(_t(e.get("description")), 600), "evidence_refs": e.get("evidence_refs", [])[:4]}
    return None


def ask_digest(overlay: dict, smap: dict, *, question: str, diff: str, selected_id: str | None = None,
               history: list[dict] | None = None, budget: int = DIGEST_BUDGET, diff_budget: int = DIFF_BUDGET) -> dict[str, Any]:
    """`history`: earlier questions of this overlay, oldest first ({question, answer: {text}} — only answered ones count)."""
    els = _elements(overlay, smap)
    ev_of = _diff_evidence(overlay)
    pr = overlay.get("pr", {})
    pr_ev = next((e["id"] for e in overlay.get("evidence", []) if e.get("kind") == "pr_metadata"), "pr")
    caps_used: set[str] = set()
    flows = []
    for fc in overlay.get("flow_changes", []):
        changed = [s for s in fc.get("steps", []) if s.get("step_change_status") != "unchanged"]
        caps_used |= {s["capability_id"] for s in fc.get("steps", []) if s.get("capability_id")}
        flows.append({"flow_id": fc["flow_id"], "name": _name(els[fc["flow_id"]][1]) if fc["flow_id"] in els else fc["flow_id"],
                      "change_status": fc.get("change_status"), "step_count": len(fc.get("steps", [])),
                      "changed_steps": [{"id": s["id"], "label": _t(s.get("business_label")) or s.get("technical_label"),
                                         "step_change_status": s.get("step_change_status"),
                                         "evidence_status": s.get("evidence_status")} for s in changed[:STEPS_PER_FLOW]]})
    files = overlay.get("changes", {}).get("files", [])
    caps_used |= {f["capability_id"] for f in files if f.get("capability_id")}
    data: dict[str, Any] = {
        "question": {"text": question, "role": "the reviewer's question: answer this one"},
        "pr": {"title": pr.get("title"), "body": _clip(pr.get("body") or "", 3000), "evidence_id": pr_ev,
               "base_ref": pr.get("base_ref"), "head_ref": pr.get("head_ref")},
        "system": {"name": smap.get("system", {}).get("technical_name"), "summary": _clip(_t(smap.get("system", {}).get("summary")), 800)},
        "purpose": _t(overlay.get("purpose")),
        "design_rationale": _t(overlay.get("design_rationale")),
        "behavior_changes": [{"id": b["id"], "before": _t(b.get("before")), "after": _t(b.get("after")),
                              "flow_ids": b.get("flow_ids", []), "capability_ids": b.get("capability_ids", [])}
                             for b in overlay.get("behavior_changes", [])],
        "findings": [{"id": f["id"], "source": f.get("source"), "title": f.get("title"), "severity": f.get("severity"),
                      "status": f.get("status"), "description": _clip(f.get("description"), 300),
                      "evidence_refs": f.get("evidence_refs", [])[:3]} for f in overlay.get("findings", [])],
        "architecture_checks": [{"id": a["id"], "check": a.get("check"), "ring": a.get("ring"), "verdict": a.get("verdict"),
                                 "status": a.get("status"), "title": a.get("title"),
                                 "description": _clip(a.get("description"), 300), "evidence_refs": a.get("evidence_refs", [])[:3]}
                                for a in overlay.get("architecture_checks", [])],
        "flows_touched": flows,
        "changed_files": [{"id": f["id"], "path": f["path"], "status": f.get("status"), "capability_id": f.get("capability_id"),
                           "evidence_id": ev_of.get(f["path"])} for f in files],
        "capabilities": [{"id": c, "name": _name(els[c][1])} for c in sorted(caps_used) if c in els],
    }
    sel = selected_item(overlay, smap, selected_id)
    if sel:
        data["selected"] = sel
    turns = [h for h in (history or []) if _t(h.get("answer"))][-HISTORY_TURNS:]
    if turns:
        data["history"] = [{"question": h["question"], "answer": _clip(_t(h["answer"]), HISTORY_ANSWER_CHARS)} for h in turns]
    for key, cap in LIST_CAPS.items():
        data[key] = data[key][:cap]
    _fit(data, budget)
    data["diff_excerpt"] = code_first(diff)[:diff_budget]
    return data


def _size(d: dict) -> int:
    return len(json.dumps(d, ensure_ascii=False, separators=(",", ":")))


def _fit(data: dict, budget: int) -> None:
    """Halve the longest lists until the digest fits (a huge PR must not blow up the prompt)."""
    while _size(data) > budget:
        key = max(LIST_CAPS, key=lambda k: len(data[k]))
        if len(data[key]) <= 1:
            break
        data[key] = data[key][: len(data[key]) // 2]


# ---------------------------------------------------------------- reply gate

def _refs(r: list[dict]) -> list[dict]:
    return [{k: x[k] for k in ("side", "path", "start_line", "end_line") if k in x} for x in r][:MAX_REFS]


def cite_index(overlay: dict, smap: dict) -> dict[str, dict]:
    """Everything an answer may cite: id -> {id, kind, label, refs}. refs are code locations the UI can link."""
    evidence = {e["id"]: e for e in [*smap.get("evidence", []), *overlay.get("evidence", [])]}
    ev_refs = lambda ids: _refs([r for i in ids for r in evidence.get(i, {}).get("refs", [])])  # noqa: E731
    out: dict[str, dict] = {}
    for kind, el in _elements(overlay, smap).values():
        out[el["id"]] = {"id": el["id"], "kind": kind, "label": _name(el), "refs": ev_refs(el.get("evidence_refs", []))}
    for e in evidence.values():
        out[e["id"]] = {"id": e["id"], "kind": "evidence", "label": e.get("description", e["id"]), "refs": _refs(e.get("refs", []))}
    for sid, (_, s) in _steps(overlay).items():
        out[sid] = {"id": sid, "kind": "step", "label": _t(s.get("business_label")) or s.get("technical_label", sid),
                    "refs": ev_refs(s.get("evidence_refs", []))}
    for fc in overlay.get("flow_changes", []):
        out.setdefault(fc["flow_id"], {"id": fc["flow_id"], "kind": "flow", "label": fc["flow_id"], "refs": []})
    for b in overlay.get("behavior_changes", []):
        out[b["id"]] = {"id": b["id"], "kind": "behavior", "label": _t(b.get("after")) or b["id"], "refs": []}
    for f in overlay.get("findings", []):
        out[f["id"]] = {"id": f["id"], "kind": "finding", "label": f.get("title", f["id"]),
                        "refs": _refs(f.get("refs") or []) or ev_refs(f.get("evidence_refs", []))}
    for a in overlay.get("architecture_checks", []):
        out[a["id"]] = {"id": a["id"], "kind": "arch", "label": a.get("title", a["id"]),
                        "refs": _refs(a.get("pr_refs") or []) or ev_refs(a.get("evidence_refs", []))}
    for f in overlay.get("changes", {}).get("files", []):
        out[f["id"]] = {"id": f["id"], "kind": "file", "label": f["path"],
                        "refs": [{"side": "base" if f.get("status") == "removed" else "head", "path": f["path"]}]}
    pr_ev = next((e["id"] for e in overlay.get("evidence", []) if e.get("kind") == "pr_metadata"), None)
    out["pr"] = {"id": "pr", "kind": "pr", "label": "PR 描述", "refs": []}
    if pr_ev:
        out[pr_ev]["kind"] = "pr"
    return out


def resolve_cite(cite: str, index: dict[str, dict]) -> dict | None:
    if cite in index:
        return index[cite]
    got = parse_file_cite(cite)  # file:<path>:<line>[-<end>] — a line (range) of a changed file
    if got and index.get(got[0], {}).get("kind") == "file":
        base = index[got[0]]
        line = {"start_line": got[1], **({"end_line": got[2]} if got[2] else {})}
        return {**base, "id": cite, "refs": [{**base["refs"][0], **line}]}
    return None


def gate_answer(payload: dict, index: dict[str, dict]) -> dict[str, Any]:
    """{text, cites: [resolved], unsupported, dropped: [unknown cites]}. `text` is '' when the reply had no answer."""
    text = payload.get("answer")
    text = text.strip()[:ANSWER_MAX] if isinstance(text, str) else ""
    raw = payload.get("cites")
    cites = [c.strip() for c in raw if isinstance(c, str) and c.strip()] if isinstance(raw, list) else []
    kept: list[dict] = []
    dropped: list[str] = []
    for c in dict.fromkeys(cites):
        r = resolve_cite(c, index)
        if r is None:
            dropped.append(c[:200])
        elif len(kept) < MAX_CITES:
            kept.append(r)
    return {"text": text, "cites": kept, "unsupported": not kept, "dropped": dropped[:20]}
