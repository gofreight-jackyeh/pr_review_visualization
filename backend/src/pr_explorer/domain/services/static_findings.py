"""Verifiable technical findings (spec §8). Only emits what the graph proves; status=verified."""

from __future__ import annotations

from ..model import ids
from ..model.changes import ChangedFile
from ..model.finding import Finding
from ..model.graph import CallShape, CodeRef, Evidence, Graph, Node, Producer

PRODUCER = Producer("static_findings", "1")


def _ref(node: Node, side: str) -> CodeRef | None:
    return next((r for r in node.code_refs if r.side == side), None)


def _path(node: Node) -> str:
    return node.code_refs[0].path if node.code_refs else ""


def _zh_problem(p: str) -> str:
    """check_call's English problem phrases in Traditional Chinese (identifiers kept as-is)."""
    for en, zh in (("passes ", "傳入 "), (" positional args, accepts ", " 個位置參數，但只接受 "),
                   ("missing required argument(s): ", "缺少必要參數："), ("unexpected keyword(s): ", "不認得的關鍵字參數：")):
        p = p.replace(en, zh)
    return p + (" 個" if p.startswith("傳入 ") else "")


def check_call(sig: dict, shape: CallShape) -> list[str]:
    """Problems binding `shape` to `sig`; empty when it binds (or cannot be decided)."""
    if shape.star_args or shape.star_kwargs:
        return []
    positional: list[str] = sig["positional"]
    problems: list[str] = []
    if shape.positional > len(positional) and not sig.get("vararg"):
        problems.append(f"passes {shape.positional} positional args, accepts {len(positional)}")
    bound = set(positional[: shape.positional]) | set(shape.keywords)
    missing = [p for p in positional[: sig["required_positional"]] if p not in bound]
    missing += [p for p in sig.get("required_kwonly", []) if p not in shape.keywords]
    if missing:
        problems.append(f"missing required argument(s): {', '.join(missing)}")
    known = set(positional) | set(sig.get("kwonly", []))
    unexpected = [k for k in shape.keywords if k not in known]
    if unexpected and not sig.get("kwarg"):
        problems.append(f"unexpected keyword(s): {', '.join(unexpected)}")
    return problems


class _Ctx:
    def __init__(self, graph: Graph, files: list[ChangedFile], lang: str = "en"):
        self.zh = lang == "zh-Hant"  # finding texts follow the detected output language (spec: business language)
        self.graph = graph
        self.changed_paths = {f.path: f for f in files} | {f.old_path: f for f in files if f.old_path}
        self.findings: list[Finding] = []
        self.evidence: list[Evidence] = []

    def file_ids(self, *paths: str) -> list[str]:
        return list(dict.fromkeys(self.changed_paths[p].id for p in paths if p in self.changed_paths))

    def ev(self, side: str, path: str, line: int, desc: str, *disc: object) -> str:
        e = Evidence(ids.evidence_id("ast", side, path, line, "finding", *disc), "ast", desc, [CodeRef(side, path, line)], PRODUCER)
        if all(x.id != e.id for x in self.evidence):
            self.evidence.append(e)
        return e.id

    def add(self, rule: str, key: str, **kw) -> None:
        if not kw["changed_file_ids"]:
            return  # must be tied to a PR change
        fid = f"finding:{rule}:{key}"
        if any(f.id == fid for f in self.findings):
            return
        self.findings.append(Finding(id=fid, source="static", rule=rule, status="verified", **kw))


def _removed_still_referenced(c: _Ctx) -> None:
    g = c.graph
    removed = {n.meta.get("dotted"): n for n in g.nodes.values() if n.change_status == "removed" and n.meta.get("dotted")}
    for ref in g.head.unresolved:
        node = removed.get(ref.target)
        if node is None:
            continue
        base_ref = _ref(node, "base")
        ev = c.ev("head", ref.code_ref.path, ref.code_ref.start_line, f"{ref.target} referenced but not defined at head", ref.target)
        c.add(
            "removed_symbol_still_referenced", f"{node.id}@{ref.code_ref.path}:{ref.code_ref.start_line}",
            title=f"已移除的 {node.label} 仍被引用" if c.zh else f"Removed {node.label} is still referenced",
            description=(f"這個 PR 移除了 {node.label}，但 {ref.code_ref.path}:{ref.code_ref.start_line} 仍然引用 {ref.target}。" if c.zh else
                         f"{node.label} was removed in this PR but {ref.code_ref.path}:{ref.code_ref.start_line} still refers to {ref.target}."),
            severity="warning",
            changed_file_ids=c.file_ids(_path(node), ref.code_ref.path),
            node_ids=[n for n in (node.id, ref.from_node) if n in g.nodes],
            refs=[ref.code_ref] + ([base_ref] if base_ref else []),
            evidence_refs=[ev] + node.evidence_refs[:1],
        )


def _signature_mismatch(c: _Ctx) -> None:
    g = c.graph
    for caller_id, steps in sorted(g.head.flows.items()):
        for st in steps:
            if st.kind != "call" or st.confidence != "confirmed" or len(st.target_ids) != 1 or st.call_shape is None:
                continue
            target = g.nodes.get(st.target_ids[0])
            if target is None or target.kind not in {"function", "method", "view"}:
                continue
            sig = target.meta.get("signature")
            base_sig = target.meta.get("base_meta", {}).get("signature")
            if not sig:
                continue
            sig_changed = base_sig is not None and base_sig != sig
            if not (sig_changed or st.path in c.changed_paths):
                continue
            problems = check_call(sig, st.call_shape)
            if not problems:
                continue
            tref = _ref(target, "head")
            ev = c.ev("head", st.path, st.line, f"call to {target.label}: {'; '.join(problems)}", target.id)
            signature = f"{target.label}({', '.join(sig['positional'])})"
            if c.zh:
                title = f"呼叫 {target.label} 的方式與它{'新的' if sig_changed else '的'}參數定義不符"
                description = (f"{st.path}:{st.line} 呼叫了 {target.label}，它的參數定義是 {signature}。問題："
                               + "；".join(_zh_problem(p) for p in problems) + "。"
                               + ("這個 PR 改了參數定義，但這個呼叫端沒有跟著更新。" if sig_changed else ""))
            else:
                title = f"Call to {target.label} does not match its {'new ' if sig_changed else ''}signature"
                description = (f"{st.path}:{st.line} calls {target.label}, whose signature is {signature}. Problem: "
                               + "; ".join(problems) + "."
                               + (" The signature changed in this PR and this caller was not updated." if sig_changed else ""))
            c.add(
                "call_signature_mismatch", f"{target.id}@{st.path}:{st.line}",
                title=title,
                description=description,
                severity="warning",
                changed_file_ids=c.file_ids(_path(target), st.path),
                node_ids=[n for n in (target.id, caller_id) if n in g.nodes],
                refs=[CodeRef("head", st.path, st.line, st.end_line)] + ([tref] if tref else []),
                evidence_refs=[ev] + st.evidence_refs[:1],
            )


def _model_field_without_migration(c: _Ctx) -> None:
    g = c.graph
    head_files = g.head.files
    for n in sorted(g.nodes.values(), key=lambda n: n.id):
        if n.kind != "model" or n.change_status not in {"added", "modified"} or n.meta.get("abstract"):
            continue
        fields = n.meta.get("fields", {})
        base_fields = n.meta.get("base_meta", {}).get("fields", {}) if n.change_status == "modified" else {}
        new = [f for f in fields if f not in base_fields]
        if not new:
            continue
        app_dir = _path(n).rsplit("/", 1)[0] if "/" in _path(n) else ""
        mig_prefix = f"{app_dir}/migrations/" if app_dir else "migrations/"
        if not any(f.startswith(mig_prefix) for f in head_files):
            continue  # app does not use migrations; nothing to prove
        if any(p.startswith(mig_prefix) and f.status in {"added", "modified"} for p, f in c.changed_paths.items()):
            continue
        line = fields[new[0]]
        ev = c.ev("head", _path(n), line, f"new field(s) {', '.join(new)} on {n.label}; no change under {mig_prefix}", n.id)
        c.add(
            "model_field_without_migration", n.id,
            title=f"{n.label} 新增欄位但沒有 migration" if c.zh else f"New field(s) on {n.label} without a migration",
            description=(
                f"{n.label} 新增了欄位 {'、'.join(new)}，但這個 PR 沒有新增或修改 {mig_prefix} 底下的檔案，"
                "而這個 app 本來就使用 migration。" if c.zh else
                f"{n.label} gains field(s) {', '.join(new)} but no file under {mig_prefix} was added or changed "
                "in this PR, although the app already uses migrations."
            ),
            severity="warning",
            changed_file_ids=c.file_ids(_path(n)),
            node_ids=[n.id],
            refs=[CodeRef("head", _path(n), fields[f]) for f in new],
            evidence_refs=[ev],
        )


def view_groups(g: Graph) -> dict[str, list[Node]]:
    """Function views grouped by module path at head."""
    groups: dict[str, list[Node]] = {}
    for n in g.nodes.values():
        if n.kind == "view" and n.change_status != "removed" and not n.meta.get("is_class"):
            groups.setdefault(_path(n), []).append(n)
    return {k: sorted(v, key=lambda n: n.id) for k, v in groups.items()}


def _view_missing_decorator(c: _Ctx) -> None:
    for path, views in sorted(view_groups(c.graph).items()):
        if len(views) < 3:
            continue
        decorators = sorted({d for v in views for d in v.meta.get("decorators", [])})
        for d in decorators:
            holders = [v for v in views if d in v.meta.get("decorators", [])]
            if len(holders) < 2 or len(holders) / len(views) < 0.5:
                continue
            for v in views:
                if v.change_status not in {"added", "modified"} or d in v.meta.get("decorators", []):
                    continue
                vref = _ref(v, "head")
                if vref is None:
                    continue
                ev = c.ev("head", vref.path, vref.start_line, f"{v.label} has no @{d}; {len(holders)}/{len(views)} sibling views do", d)
                c.add(
                    "view_missing_sibling_decorator", f"{v.id}:{d}",
                    title=(f"{v.label} 缺少同檔其他 view 都有的 @{d}" if c.zh else f"{v.label} lacks @{d} used by its sibling views"),
                    description=(
                        f"{path} 的 {len(views)} 個 view 中有 {len(holders)} 個加了 @{d}"
                        f"（{'、'.join(h.label for h in holders[:4])}）；這個 PR 修改的 {v.label} 沒有加。" if c.zh else
                        f"{len(holders)} of {len(views)} views in {path} are decorated with @{d} "
                        f"({', '.join(h.label for h in holders[:4])}); {v.label}, changed in this PR, is not."
                    ),
                    severity="attention",
                    changed_file_ids=c.file_ids(path),
                    node_ids=[v.id] + [h.id for h in holders[:3]],
                    refs=[vref] + [r for h in holders[:2] if (r := _ref(h, "head"))],
                    evidence_refs=[ev] + v.evidence_refs[:1],
                )


def find_static_issues(graph: Graph, files: list[ChangedFile], lang: str = "en") -> tuple[list[Finding], list[Evidence]]:
    c = _Ctx(graph, files, lang)
    _removed_still_referenced(c)
    _signature_mismatch(c)
    _model_field_without_migration(c)
    _view_missing_decorator(c)
    return c.findings, c.evidence
