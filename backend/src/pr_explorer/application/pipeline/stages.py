"""Analysis pipeline v2, one stage per step of docs/api.md:

acquire → system_map → pr_static → flows → impact → semantic → assemble

system_map builds (or reuses, per repo + merge-base commit) the PR-independent map, including Claude call (a);
the remaining stages lay the PR over that map; semantic is Claude call (b). Claude output is untrusted: any failure
or malformed reply degrades to the static result, which is always stored (spec v2 §13).

Call (a) runs in up to three steps: capability texts (batched), then — in parallel — the domain assignment and the
flow phases. Each step is skipped when an earlier map of the repository already holds a valid result for it.
"""

from __future__ import annotations

import copy
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ...domain.model.changes import ChangedFile, Coverage
from ...domain.model import ids
from ...domain.model.convention import DocExcerpt, instruction_scope
from ...domain.model.document import (
    ArchCheck, ChangesPayload, FlowChange, Impact, Phase, PrOverlay, PullRequest, Repo, SemanticStatus, SystemMap, Text,
)
from ...domain.model.finding import Finding
from ...domain.model.graph import CodeRef, Evidence, Graph, Producer, SideGraph
from ...domain.services.complexity_score import decide, score_change
from ...domain.services.architecture import check_order, check_pr, infer_rules
from ...domain.services.rings import detect_interfaces, diff_interfaces
from ...domain.services.domains import MIN_BUSINESS_FOR_CLAUDE, apply_domains, business, domain_digest, gate_domains
from ...domain.services.graph_diff import diff_graphs
from ...domain.services.impact_propagation import propagate_impact
from ...domain.services.map_reuse import (
    BaseChange, ReuseLimits, ReusePlan, capability_fingerprints, capability_lines, capability_surfaces, donor_language,
    has_surfaces, reuse_texts, skeleton,
)
from ...domain.services.overlay_builder import MapIndex, OverlayBuilder, static_behavior_changes
from ...domain.services.semantic_digest import (
    capability_batches, detect_language, digest_targets, phase_batches, phase_digest, pr_digest, relevant_rules,
    restrict_reply, system_map_digest,
)
from ...domain.services.semantic_gate import PrKnown, PrSemantic, apply_phases, apply_system_semantics, gate_pr
from ...domain.model.document import TextBasis
from ...domain.services.static_findings import find_static_issues
from ...domain.services.system_map_builder import assemble_system_map, build_flows, referenced_evidence
from ...domain.services.system_structure import Structure, build_structure
from ...domain.services.walkthrough_builder import build_walkthrough
from ..ports import (
    AnalysisRepository, AnalysisRequest, AnalyzerUnavailable, DiffParser, DocumentStore, GenericAnalyzer,
    KnowledgeResult, KnowledgeSource, LanguageAnalyzer, ResolvedPr, SemanticAnalyzer, SemanticRequest, SourceProvider,
    Workspace,
)

STAGES = ["acquire", "system_map", "pr_static", "flows", "impact", "semantic", "assemble"]
DIFF_PRODUCER = Producer("git_diff", "1")
DOC_PRODUCER = Producer("repo_docs", "1")
PR_PRODUCER = Producer("pr_metadata", "1")
PR_EVIDENCE = "ev:pr:metadata"
DEEP_LEVEL = {"python": "python", "typescript": "typescript", "javascript": "typescript"}
NON_CODE = {"markdown", "text", "restructuredtext", "json", "yaml", "toml", "html", "css", "dotenv"}
REUSABLE_MAP_SEMANTIC = {"done", "skipped"}  # a map whose Claude call failed is rebuilt next time, not reused
DONOR_SEMANTIC = {"done", "partial"}  # maps whose Claude texts may be carried over to a map on another commit
MAX_DONORS = 20


class StageSkipped(Exception):
    """Raised by a stage that intentionally did nothing; message is reported."""


def _k(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def call_stats(label: str, drafts: list) -> str:
    """'capabilities 3 calls 12k–31k chars, ≤ 95 s' / 'PR call 41k chars, 312 s' (+ failures): prompt size and wall
    time of every Claude call, in the stage message (the per-attempt debug log is <home>/logs/semantic-calls.jsonl)."""
    if not drafts:
        return ""
    chars = [d.prompt_chars for d in drafts]
    secs = [d.seconds for d in drafts]
    failed = [d for d in drafts if d.status != "done"]
    if len(drafts) == 1:
        out = f"{label} call {_k(chars[0])} chars, {secs[0]:.0f} s"
    else:
        span = _k(min(chars)) if min(chars) == max(chars) else f"{_k(min(chars))}–{_k(max(chars))}"
        out = f"{label} {len(drafts)} calls {span} chars, ≤ {max(secs):.0f} s"
    if failed:
        out += f" ({len(failed)} failed: {(failed[0].error or failed[0].status)[:100]})"
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class PipelineConfig:
    impact_max_depth: int = 3
    impact_max_nodes: int = 200
    flow_budget: int = 20  # flows with full business steps in the system map (plus every flow a PR touches)
    capability_batch: int = 15  # capabilities per Claude system-map call ...
    capability_chars: int = 60_000  # ... and at most this many characters of their own data (reply time follows it)
    phase_batch: int = 10  # flows per Claude phase call ...
    phase_chars: int = 24_000  # ... and at most this many characters of flow data
    semantic_parallel: int = 4  # concurrent Claude system-map calls
    # language of every generated text: "zh-Hant" | "en" | "auto" (zh-Hant when the repo docs / PR are Chinese, else en)
    output_language: str = "zh-Hant"
    # The PR call starts as soon as the map texts it shows (names of the elements it lists, step labels of its flows,
    # the system summary) are written, instead of after the whole map (other capabilities, domains, phases). Its prompt
    # is the same; should the stored map end up different, the call is repeated with the final names.
    pr_early_start: bool = True
    # Staleness guard of incremental map reuse (domain/services/map_reuse.py, ReuseLimits): a capability's Claude texts
    # are re-explained as a whole once more than this share of its code lines changed since they were written ...
    reuse_max_changed_share: float = 0.4
    reuse_max_base_changes: int = 40  # ... or they were carried across more base commits than this ...
    reuse_max_age_days: float = 14.0  # ... or they are older than this many days

    def language(self, *texts: str) -> str:
        return detect_language(*texts) if self.output_language == "auto" else self.output_language


@dataclass
class PipelineDeps:
    source: SourceProvider
    diff_parser: DiffParser
    generic: GenericAnalyzer
    analyzers: list[LanguageAnalyzer]
    semantic: SemanticAnalyzer
    knowledge: KnowledgeSource
    store: DocumentStore
    repo: AnalysisRepository
    language_of: Callable[[str], str]
    config: PipelineConfig = field(default_factory=PipelineConfig)


@dataclass
class Context:
    analysis_id: str
    request: AnalysisRequest
    pr: ResolvedPr
    overlay_key: str
    overlay_id: str
    system_map_key: str
    system_map_id: str
    analyzer_version: str
    workspace: Workspace | None = None
    # system map
    map_doc: dict | None = None
    map_reused: bool = False
    map_path: Path | None = None
    sides: dict[str, SideGraph] = field(default_factory=dict)
    deep_ok: dict[str, set[str]] = field(default_factory=dict)
    notes: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    deep_jobs: dict[tuple[int, str], Future] = field(default_factory=dict)  # (analyzer index, side) -> its SideGraph
    # PR
    files: list[ChangedFile] = field(default_factory=list)
    diff_evidence: dict[str, Evidence] = field(default_factory=dict)  # path -> evidence
    graph: Graph | None = None
    base_st: Structure | None = None
    head_st: Structure | None = None
    map_index: MapIndex | None = None
    builder: OverlayBuilder | None = None
    coverage: list[Coverage] = field(default_factory=list)
    flow_changes: list[FlowChange] = field(default_factory=list)
    flow_nodes: dict[str, set[str]] = field(default_factory=dict)
    impacts: list[Impact] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    arch_checks: list[ArchCheck] = field(default_factory=list)
    extra_evidence: dict[str, Evidence] = field(default_factory=dict)
    knowledge: KnowledgeResult | None = None
    lang: str = "en"
    semantic: SemanticStatus | None = None
    pr_semantic: PrSemantic | None = None
    overlay_path: Path | None = None
    map_flight: "MapFlight | None" = None  # the in-process build of this map (None: served from cache)
    on_static: Callable[[], None] | None = None  # called once the static overlay + map are stored (progressive)

    @property
    def semantic_on(self) -> bool:
        return self.request.semantic_mode != "off"


@dataclass
class MapFlight:
    """One in-flight map build: `static` → (doc, path) once the static map is stored; `final` → (doc, path, message)
    once Claude's texts are in (or failed)."""

    static: Future = field(default_factory=Future)
    final: Future = field(default_factory=Future)
    # what lets a PR call start before `final`: one future per Claude capability / summary call (its SemanticDraft,
    # set as each call returns) and the map those calls write into
    calls: list[Future] = field(default_factory=list)
    early: "EarlyNames | None" = None


@dataclass
class EarlyNames:
    """The map as stored before Claude's texts (`pending`) and, per capability / summary call, the IDs it writes
    (`targets`). A call's texts depend on its own reply only, so the texts a PR shows are final once the calls
    writing them returned."""

    pending: SystemMap
    targets: list[set[str]]
    known: set[str]  # every element / step ID of the map (replies are restricted to their call's targets)
    summary_call: int | None  # the call writing the system summary (None: reused)


def _light_copy(smap: SystemMap) -> SystemMap:
    """A copy whose texts can be rewritten (named elements, flows and steps copied; everything else shared)."""
    m = copy.copy(smap)
    m.system = copy.copy(smap.system)
    m.capabilities = [copy.copy(c) for c in smap.capabilities]
    m.entities = [copy.copy(e) for e in smap.entities]
    m.entry_points = [copy.copy(e) for e in smap.entry_points]
    m.flows = []
    for f in smap.flows:
        f2 = copy.copy(f)
        f2.timeline = copy.copy(f.timeline)
        f2.timeline.steps = [copy.copy(s) for s in f.timeline.steps]
        m.flows.append(f2)
    return m


class Pipeline:
    def __init__(self, deps: PipelineDeps):
        self.d = deps
        self.system_map_builds = 0  # observable: a cache hit or a joined in-flight build must not rebuild
        self._flights: dict[str, MapFlight] = {}
        self._flight_lock = threading.Lock()
        self._map_jobs = ThreadPoolExecutor(max_workers=2, thread_name_prefix="map-semantic")

    def run(self, ctx: Context, report: Callable[[str, str, str], None]) -> Path:
        try:
            for name in STAGES:
                report(name, "running", "")
                try:
                    msg = getattr(self, f"stage_{name}")(ctx) or ""
                except StageSkipped as s:
                    report(name, "skipped", str(s))
                    continue
                except Exception as exc:
                    report(name, "failed", f"{type(exc).__name__}: {exc}"[:500])
                    raise
                report(name, "done", msg)
        finally:
            self._join_deep(ctx)  # the caller releases the worktrees next: no analyzer may still be reading them
        assert ctx.overlay_path is not None
        return ctx.overlay_path

    # ------------------------------------------------------------------ L1
    def stage_acquire(self, ctx: Context) -> str:
        ctx.workspace = self.d.source.materialize(ctx.pr)
        self._start_deep(ctx)
        return f"worktrees at merge-base {ctx.pr.merge_base_sha[:10]} and head {ctx.pr.head_sha[:10]}"

    # ------------------------------------------------------------------ L2/L3 helpers
    def _start_deep(self, ctx: Context) -> None:
        """Start both sides' deep analysis right after checkout: one thread per analyzer, base then head. The TS
        analyzer is a separate Node process, so its two runs overlap the Python parse instead of following it; the
        Python analyzer still parses head after base (reusing its summaries). One side at a time per analyzer keeps a
        single Node process per analysis (peak memory as before). `_deep` merges the results in analyzer order."""
        ws = ctx.workspace
        assert ws is not None
        for i, an in enumerate(self.d.analyzers):
            futs = [ctx.deep_jobs.setdefault((i, side), Future()) for side in ("base", "head")]

            def chain(an=an, futs=futs) -> None:
                for fut, (root, files, side) in zip(futs, ((ws.base_root, ws.base_files, "base"),
                                                           (ws.head_root, ws.head_files, "head"))):
                    if not fut.set_running_or_notify_cancel():
                        continue
                    try:
                        fut.set_result(an.analyze(root, files, side))
                    except BaseException as exc:
                        fut.set_exception(exc)

            threading.Thread(target=chain, name=f"deep-{an.name}", daemon=True).start()

    @staticmethod
    def _join_deep(ctx: Context) -> None:
        for fut in ctx.deep_jobs.values():
            fut.cancel()  # not started: never starts
        wait(list(ctx.deep_jobs.values()))

    def _deep(self, ctx: Context, side: str) -> SideGraph:
        """Deep analyzers over the whole side (parsed once per commit; the Python analyzer reuses unchanged files)."""
        ws = ctx.workspace
        assert ws is not None
        root, files = (ws.base_root, ws.base_files) if side == "base" else (ws.head_root, ws.head_files)
        sg = SideGraph(side=side, files=files)
        notes: dict[str, list[str]] = {}
        ok: set[str] = set()
        for i, an in enumerate(self.d.analyzers):
            if not an.available():
                notes.setdefault(an.language, []).append(f"{an.name} analyzer unavailable; generic analysis only")
                continue
            if not any(an.claims(p) for p in files):
                continue
            try:
                job = ctx.deep_jobs.get((i, side))
                sg.merge(job.result() if job is not None else an.analyze(root, files, side))
                ok.add(an.language)
            except AnalyzerUnavailable as exc:
                notes.setdefault(an.language, []).append(f"{exc}; generic analysis only")
        for lang, ns in sg.notes.items():
            notes.setdefault(lang, []).extend(n for n in ns if n not in notes.get(lang, []))
        ctx.sides[side], ctx.deep_ok[side], ctx.notes[side] = sg, ok, notes
        return sg

    def _coverage(self, ctx: Context, side: str, paths: list[tuple[str, bool]]) -> list[Coverage]:
        counts: dict[str, int] = {}
        binary: set[str] = set()
        for p, is_bin in paths:
            lang = self.d.language_of(p)
            counts[lang] = counts.get(lang, 0) + 1
            if is_bin:
                binary.add(lang)
        out = []
        for lang in sorted(counts):
            deep = DEEP_LEVEL.get(lang)
            if lang == "unknown" or lang in binary:
                level = "unsupported"
            elif deep and deep in ctx.deep_ok.get(side, set()):
                level = "deep"
            else:
                level = "generic"
            notes = list(ctx.notes.get(side, {}).get(deep or lang, []))
            if level == "generic" and lang not in NON_CODE and not notes:
                notes.append("no deep analyzer for this language: module + regex import analysis only")
            if level == "unsupported":
                notes.append("binary or unrecognized files: listed only")
            out.append(Coverage(lang, level, counts[lang], notes))
        return out

    # ------------------------------------------------------------------ L4 system map (+ Claude call a)
    def stage_system_map(self, ctx: Context) -> str:
        """Exact cache hit → reuse. Otherwise one build per map key in this process (single-flight): the first
        analysis builds and stores the static map (Claude texts carried over from an earlier map where the code is
        unchanged), then Claude explains the rest in the background; concurrent analyses join that build."""
        with self._flight_lock:
            hit = self.d.repo.find_system_map(ctx.system_map_key)
            if hit and Path(hit["path"]).is_file() and hit["semantic_status"] in REUSABLE_MAP_SEMANTIC:
                doc = self.d.store.load_system_map(hit["id"])
                if doc is not None:
                    ctx.map_doc, ctx.map_reused, ctx.map_path = doc, True, Path(hit["path"])
                    raise StageSkipped(f"reused {hit['id']}")
            flight = self._flights.get(ctx.system_map_key)
            leader = flight is None
            if flight is None:
                flight = self._flights[ctx.system_map_key] = MapFlight()
                self.system_map_builds += 1
        ctx.map_flight = flight
        if not leader:
            doc, path = flight.static.result()  # waits for the leader's static map
            ctx.map_doc, ctx.map_reused, ctx.map_path = doc, True, path
            raise StageSkipped(f"reused {ctx.system_map_id} (built concurrently by another analysis)")
        try:
            return self._build_map(ctx, flight)
        except BaseException as exc:
            self._end_flight(ctx.system_map_key, flight, exc=exc)
            raise

    def _end_flight(self, key: str, flight: "MapFlight", result: tuple | None = None, exc: BaseException | None = None) -> None:
        """Publish the final map (or the failure) to every waiting analysis; the map is indexed before this."""
        with self._flight_lock:
            if self._flights.get(key) is flight:
                del self._flights[key]
        for fut, val in ((flight.static, result[:2] if result else None), (flight.final, result)):
            if not fut.done():
                fut.set_exception(exc) if exc is not None else fut.set_result(val)
        for fut in flight.calls:  # an early PR call waiting on one of them falls back to `final`
            if not fut.done():
                fut.set_exception(RuntimeError("map semantic ended before this call returned"))

    def _save_map(self, ctx: Context, smap: SystemMap, fallback: SystemMap, files: set[str]) -> tuple[SystemMap, Path, str]:
        """Store `smap`; if Claude text broke validation, store `fallback` (static) instead (spec §13)."""
        try:
            return smap, self.d.store.save_system_map(smap, files), ""
        except ValueError as exc:
            if smap is fallback:
                raise
            fallback.semantic = SemanticStatus("failed", tier=smap.semantic.tier, dropped_claims=smap.semantic.dropped_claims,
                                               knowledge_sources=smap.semantic.knowledge_sources,
                                               error=f"semantic output discarded, system map failed validation: {exc}"[:2000])
            return fallback, self.d.store.save_system_map(fallback, files), "semantic discarded (validation)"

    def _index_map(self, ctx: Context, smap: SystemMap, path: Path) -> dict:
        # a partially explained map (some capability batches failed) is stored but rebuilt next time, not reused
        partial = smap.semantic.status == "done" and (smap.semantic.error or "").startswith("partial:")
        self.d.repo.index_system_map(ctx.system_map_id, ctx.system_map_key, str(path), ctx.pr.repo_key,
                                     ctx.pr.merge_base_sha, "partial" if partial else smap.semantic.status)
        doc = self.d.store.load_system_map(ctx.system_map_id)
        assert doc is not None
        return doc

    def _build_map(self, ctx: Context, flight: "MapFlight") -> str:
        ws = ctx.workspace
        assert ws is not None
        sg = self._deep(ctx, "base")
        knowledge = self.d.knowledge.collect(ws.base_root, ws.base_files, [])
        docs = [(f"ev:doc:base:{d.path}", d) for d in knowledge.excerpts]
        doc_ev = {eid: Evidence(eid, "doc", f"Repository document {d.path}", [CodeRef("base", d.path, 1, d.end_line)], DOC_PRODUCER)
                  for eid, d in docs}
        lang = self.d.config.language(*(d.text for _, d in docs))
        st = build_structure(sg, self.d.language_of, lang=lang)
        flows, built = build_flows(sg, st, self.d.config.flow_budget)
        contracts, contract_notes = self.d.knowledge.contracts(ws.base_root, ws.base_files)
        rules = infer_rules(sg, st, contracts, lang)
        surface = self.d.knowledge.surface(ws.base_root, ws.base_files)
        ifs = detect_interfaces(sg, st, surface, surface.config, contracts, lang)
        coverage = self._coverage(ctx, "base", [(p, False) for p in sorted(ws.base_files)])
        static = assemble_system_map(
            sg, st, flows, built, repo=Repo(ctx.pr.repo_name, ctx.pr.repo_owner), commit_sha=ctx.pr.merge_base_sha,
            analyzer_version=ctx.analyzer_version, system_map_id=ctx.system_map_id, generated_at=now_iso(),
            coverage=coverage, semantic=SemanticStatus("skipped", tier="none"),
            extra_evidence={**doc_ev, **{e.id: e for e in rules.evidence}, **ifs.evidence}, rules=rules.rules,
            interfaces=list(ifs.interfaces.values()),
        )
        static.evidence = sorted({**{e.id: e for e in static.evidence}, **doc_ev}.values(), key=lambda e: e.id)
        b = static.flow_budget
        cats = {k: sum(1 for c in static.capabilities if c.category == k) for k in ("business", "shared", "engineering")}
        split = sum(1 for c in static.capabilities if c.parent_capability_id)
        shape = (f"{ctx.system_map_id}: {len(static.capabilities)} capabilities ({cats['business']} business, "
                 f"{cats['shared']} shared, {cats['engineering']} engineering; {split} from splits), "
                 f"{len(static.entry_points)} entry points, {len(static.entities)} entities; flows built "
                 f"{b.flows_built}/{b.entry_points_total}; {len(static.architecture_rules)} architecture rule(s)"
                 + (f" ({'; '.join(contract_notes)})" if contract_notes else "")
                 + f"; interfaces {sum(i.ring == 'outer' for i in static.interfaces)} outer / "
                   f"{sum(i.ring == 'middle' for i in static.interfaces)} middle"
                 + (f" ({surface.config.path}" + (f": {'; '.join(surface.config.notes[:3])}" if surface.config.notes else "") + ")"
                    if surface.config.path else "")
                 + (f"; {len(surface.api_specs)} OpenAPI document(s)" if surface.api_specs else "")
                 + (f" ({'; '.join(surface.notes[:3])})" if surface.notes else ""))
        if not ctx.semantic_on:
            smap, path, _ = self._save_map(ctx, static, static, ws.base_files)
            doc = self._index_map(ctx, smap, path)
            ctx.map_doc, ctx.map_path = doc, path
            self._end_flight(ctx.system_map_key, flight, result=(doc, path, "semantic off"))
            return f"{shape}; semantic off"

        tier = "standard"  # fixed: the cached map must not depend on the semantic_mode of the PR that built it
        sources = list(dict.fromkeys(["repo:code"] + knowledge.sources))
        smap = copy.deepcopy(static)
        try:
            plan = self._reuse(ctx, smap, lang, sg)
        except Exception as exc:  # reuse is an optimisation: on any surprise explain everything
            smap = copy.deepcopy(static)
            plan = self._fresh_plan(smap, [f"reuse skipped: {type(exc).__name__}: {exc}"[:200]], sg)
        need_domains = not plan.domains_reused and len(business(smap.capabilities)) >= MIN_BUSINESS_FOR_CLAUDE
        reuse_msg = self._reuse_message(plan)
        if not plan.ask and not plan.partial and plan.summary_reused and not need_domains and not plan.phases_ask:  # nothing left for Claude
            err = "; ".join(plan.reasons[:5])
            smap.semantic = SemanticStatus("done", tier=tier, dropped_claims=plan.dropped, knowledge_sources=sources,
                                           error=f"dropped {plan.dropped} reused claim(s): {err}" if plan.dropped else None)
            smap, path, note = self._save_map(ctx, smap, static, ws.base_files)
            doc = self._index_map(ctx, smap, path)
            ctx.map_doc, ctx.map_path = doc, path
            self._end_flight(ctx.system_map_key, flight, result=(doc, path, f"{reuse_msg}; no Claude call {note}".strip()))
            return f"{shape}; {reuse_msg}; no Claude call needed"

        # progressive: store the static map (with reused texts) now; Claude fills in the rest in the background
        pending = copy.deepcopy(smap)
        pending.semantic = SemanticStatus("pending", tier=tier, knowledge_sources=sources)
        try:
            path = self.d.store.save_system_map(pending, ws.base_files)
        except ValueError:  # a reused text broke validation: start from the plain static map
            smap = copy.deepcopy(static)
            plan = self._fresh_plan(smap, ["reused texts broke validation"], sg)
            need_domains = len(business(smap.capabilities)) >= MIN_BUSINESS_FOR_CLAUDE
            pending = copy.deepcopy(smap)
            pending.semantic = SemanticStatus("pending", tier=tier, knowledge_sources=sources)
            path = self.d.store.save_system_map(pending, ws.base_files)
        doc = self.d.store.load_system_map(ctx.system_map_id)
        assert doc is not None
        ctx.map_doc, ctx.map_path = doc, path
        cfg = self.d.config
        units = plan.ask + [c for c in plan.partial if c not in plan.ask]
        batches, priority = self._map_batches(ctx, smap, sg, units, plan.partial)
        need_summary = not plan.summary_reused
        # The summary rides along only when the map needs a single (small) batch; otherwise it is its own small call, so
        # a slow or failed capability batch cannot take it down (seen in practice: the first of two batches, which also
        # carried the summary, timed out and the map ended without a system summary).
        solo = need_summary and len(batches) != 1
        groups = ([[]] if solo else []) + batches
        digests = [system_map_digest(smap, sg, batch, docs, first=(solo or bool(batch)) and i == 0,
                                     write_summary=need_summary and i == 0, partial=plan.partial)
                   for i, batch in enumerate(groups)]  # built now: the PR stages go on to mutate the base graph
        known = ({el.id for coll in (smap.capabilities, smap.entities, smap.entry_points, smap.flows) for el in coll}
                 | {s.id for f in smap.flows for s in f.timeline.steps})
        targets = [digest_targets(smap, batch, plan.partial) for batch in groups]
        flight.calls = [Future() for _ in digests]
        flight.early = EarlyNames(pending, targets, known, 0 if need_summary and digests else None)
        flight.static.set_result((doc, path))
        self.d.source.retain(ctx.pr)  # the background Claude calls read the base worktree
        try:
            self._map_jobs.submit(self._finish_map, ctx, flight, smap, pending, digests, plan, sources, lang,
                                  need_domains, docs, targets, known)
        except BaseException:
            self.d.source.release(ctx.pr)
            raise
        parts = sum(len(v) for c, v in plan.partial.items() if c not in plan.ask)
        return (f"{shape}; {reuse_msg}; static map stored, Claude explaining {len(plan.ask)} capabilit"
                f"{'y' if len(plan.ask) == 1 else 'ies'}" + (f" + {parts} element(s) of {len(units) - len(plan.ask)} more" if parts else "")
                + f" in {len(batches)} call(s)" + (f" (first: the {len(priority)} this PR touches)" if priority else "")
                + (" + summary call" if solo else "")
                + ("; domains" if need_domains else "") + (f"; phases of {len(plan.phases_ask)} flow(s)" if plan.phases_ask else ""))

    def _map_batches(self, ctx: Context, smap: SystemMap, sg: SideGraph, units: list[str],
                     partial: dict[str, list[str]]) -> tuple[list[list[str]], list[str]]:
        """Capability batches; when there are several, the capabilities this PR's files belong to (and the owners of
        the built flows passing through them) go first, in batches of their own, so the PR call can start sooner.
        Returns (batches, the prioritised capabilities)."""
        cfg = self.d.config
        if not units:
            return [], []
        allb = capability_batches(smap, cfg.capability_batch, only=units, sg=sg, char_budget=cfg.capability_chars, partial=partial)
        if len(allb) <= 1:
            return allb, []
        ws = ctx.workspace
        assert ws is not None
        changed = {p for f in self.d.diff_parser.parse(ws.diff_text) for p in (f.path, f.old_path) if p}
        mods = {c.refs[0].path: c.capability_id for c in smap.components if c.kind == "module" and c.refs and c.capability_id}
        touched = {mods[p] for p in changed if p in mods}
        ep_cap = {ep.id: ep.capability_id for ep in smap.entry_points}
        touched |= {ep_cap[f.entry_point_id] for f in smap.flows
                    if f.entry_point_id in ep_cap and any(s.capability_id in touched for s in f.timeline.steps)}
        first = [c for c in units if c in touched]
        if not first or len(first) == len(units):
            return allb, []
        rest = [c for c in units if c not in touched]
        return (capability_batches(smap, cfg.capability_batch, only=first, sg=sg, char_budget=cfg.capability_chars, partial=partial)
                + capability_batches(smap, cfg.capability_batch, only=rest, sg=sg, char_budget=cfg.capability_chars, partial=partial)), first

    @staticmethod
    def _reuse_message(plan: ReusePlan) -> str:
        if not plan.donor_id:
            return "no earlier map to reuse"
        why: dict[str, int] = {}
        for w in plan.why.values():
            why[w.split(":")[0]] = why.get(w.split(":")[0], 0) + 1
        parts = sum(len(v) for v in plan.partial.values())
        return (f"reused texts of {len(plan.reused)}/{len(plan.reused) + len(plan.ask)} capabilities from {plan.donor_id}"
                + (" + summary" if plan.summary_reused else "") + (" + domains" if plan.domains_reused else "")
                + f" + phases of {len(plan.phases_reused)} flow(s)"
                + (f"; re-explained: {', '.join(f'{n} {k}' for k, n in sorted(why.items()))}" if why else "")
                + (f"; {parts} element(s) of {len(plan.partial)} reused capabilities re-asked (cited code changed)" if parts else "")
                + (f"; evidence of reused texts: {plan.evidence_kept} unchanged, {plan.evidence_moved} remapped, "
                   f"{plan.evidence_failed} not remappable" if plan.evidence_moved or plan.evidence_failed else ""))

    @staticmethod
    def _fresh_plan(smap: SystemMap, reasons: list[str], sg: SideGraph | None = None) -> ReusePlan:
        """Explain everything (no donor): every non-engineering capability and every eligible flow."""
        Pipeline._set_surfaces(smap, sg)
        plan = reuse_texts(smap, None, {}, {})
        plan.reasons = reasons
        return plan

    @staticmethod
    def _set_surfaces(smap: SystemMap, sg: SideGraph | None) -> dict[str, str]:
        """Every explained capability records its surface fingerprint (what the next map compares)."""
        surfaces = capability_surfaces(smap, sg)
        for c in smap.capabilities:
            if c.category != "engineering":
                c.text_basis = TextBasis(surfaces[c.id])
        return surfaces

    def _reuse(self, ctx: Context, smap: SystemMap, lang: str = "", sg: SideGraph | None = None) -> ReusePlan:
        """Texts from the most recent earlier map of this repo whose commit is an ancestor of the new base (else the
        most recent one). Any stored 2.x map donates, whatever its analyzer version: every text is re-validated, its
        line-based evidence remapped across the diff between the donor's commit and the new base."""
        surfaces = self._set_surfaces(smap, sg)
        rows = [r for r in self.d.repo.list_system_maps(ctx.pr.repo_key)
                if r["cache_key"].endswith("|semantic") and r["semantic_status"] in DONOR_SEMANTIC][:MAX_DONORS]
        ancestors = [r for r in rows if r["commit_sha"] == ctx.pr.merge_base_sha
                     or self.d.source.is_ancestor(ctx.pr, r["commit_sha"], ctx.pr.merge_base_sha)]
        cfg = self.d.config
        base = ctx.pr.merge_base_sha
        new_fp: dict[str, str] | None = None
        for r in ancestors + [r for r in rows if r not in ancestors]:
            try:
                donor = self.d.store.load_system_map(r["id"])
                if not donor or donor.get("kind") != "system_map" or not str(donor.get("schema_version", "")).startswith("2."):
                    continue
                if lang and donor_language(donor) != lang:  # reused texts must already be in the output language
                    continue
                legacy = not has_surfaces(donor)  # stored before 2.5.0: the blob-fingerprint rule decides
                donor_fp = (capability_fingerprints(donor, self.d.source.blobs(ctx.pr, donor["commit_sha"]), self.d.language_of)
                            if legacy else None)
            except Exception:  # unreadable / foreign document or commit gone from the cache: try the next one
                continue
            if legacy and new_fp is None:
                new_fp = capability_fingerprints(skeleton(smap), self.d.source.blobs(ctx.pr, base), self.d.language_of)
            change: BaseChange | None
            if donor["commit_sha"] == base:
                change = BaseChange(same_commit=True)
            else:
                try:
                    change = BaseChange.from_files(self.d.diff_parser.parse(self.d.source.diff(ctx.pr, donor["commit_sha"], base)))
                except Exception:  # no diff (e.g. commit gone): evidence must then exist unchanged
                    change = None
            ws = ctx.workspace
            base_lines = {n.code_refs[0].path: max(n.code_refs[0].end_line or 1, 1) for n in (sg.nodes.values() if sg else [])
                          if n.level == "module" and n.code_refs}
            lines, changed = capability_lines(skeleton(smap), ws.base_files if ws else set(base_lines), base_lines, change,
                                              self.d.language_of)
            return reuse_texts(smap, donor, new_fp, donor_fp, surfaces=surfaces, change=change,
                               cap_lines=lines, cap_changed=changed, now=now_iso(),
                               limits=ReuseLimits(cfg.reuse_max_changed_share, cfg.reuse_max_base_changes, cfg.reuse_max_age_days))
        return reuse_texts(smap, None, {}, {})

    def _finish_map(self, ctx: Context, flight: "MapFlight", smap: SystemMap, pending: SystemMap, digests: list[dict],
                    plan: ReusePlan, sources: list[str], lang: str, need_domains: bool = False, docs: list | None = None,
                    targets: list[set[str]] | None = None, known: set[str] | None = None) -> None:
        """Background: Claude call (a) for the capabilities that still need texts, then the final map replaces the
        pending one. Always resolves the flight, so waiting analyses never hang."""
        ws = ctx.workspace
        assert ws is not None
        try:
            fallback = copy.deepcopy(pending)
            if fallback.system.summary is None and plan.summary_fallback is not None:
                fallback.system.summary = plan.summary_fallback  # the earlier map's summary beats none
            fallback.semantic = (SemanticStatus("done", tier="standard", dropped_claims=plan.dropped, knowledge_sources=sources)
                                 if plan.reused else SemanticStatus("failed", tier="standard", knowledge_sources=sources))
            try:
                msg = self._explain_system(ctx, smap, digests, plan, sources, lang, need_domains, docs or [],
                                           targets, known, flight.calls)
            except Exception as exc:  # untrusted reply: any malformed shape → the map without Claude's new texts
                err = f"semantic reply rejected: {type(exc).__name__}: {exc}"
                fallback.semantic.error = (f"partial: {err}" if plan.reused else err)[:2000]
                smap, msg = fallback, "semantic failed on a malformed reply"
            smap, path, note = self._save_map(ctx, smap, fallback, ws.base_files)
            doc = self._index_map(ctx, smap, path)
            self._end_flight(ctx.system_map_key, flight, result=(doc, path, f"{msg} {note}".strip()))
        except BaseException as exc:
            self._end_flight(ctx.system_map_key, flight, exc=exc)
        finally:
            self.d.source.release(ctx.pr)

    def _explain_system(self, ctx: Context, smap: SystemMap, digests: list[dict], plan: ReusePlan, sources: list[str],
                        lang: str, need_domains: bool = False, docs: list | None = None,
                        targets: list[set[str]] | None = None, known: set[str] | None = None,
                        calls: list[Future] | None = None) -> str:
        """Step 1: capability texts (parallel batches); each reply only writes what its call asked about (`targets`),
        and each call's draft is published on `calls` as it returns (an early PR call waits on those). Step 2, in
        parallel: domain assignment (validated; retried once with the problems, then static domains) and flow phases
        (validated per flow)."""
        ws = ctx.workspace
        assert ws is not None
        tier = "standard"
        cfg = self.d.config
        reqs = [SemanticRequest("system_map", tier, lang, d, ws.base_root) for d in digests]
        drafts = []

        def call(i: int, req: SemanticRequest):
            try:
                d = self.d.semantic.analyze(req)
            except BaseException as exc:
                if calls and not calls[i].done():
                    calls[i].set_exception(exc)
                raise
            if calls and not calls[i].done():
                calls[i].set_result(d)
            return d

        if reqs:
            with ThreadPoolExecutor(max_workers=max(1, min(cfg.semantic_parallel, len(reqs))), thread_name_prefix="claude-map") as pool:
                drafts = list(pool.map(call, range(len(reqs)), reqs))  # bounded concurrency; replies keep batch order
        replies: list[dict] = []
        errors: list[str] = []
        status = "done"
        stats = [call_stats("capabilities", [d for d, g in zip(drafts, digests) if g["capabilities"]]),
                 call_stats("summary", [d for d, g in zip(drafts, digests) if not g["capabilities"]])]
        for i, draft in enumerate(drafts):
            if draft.status != "done":
                status = draft.status if status == "done" else status
                errors.append(draft.error or draft.status)
                continue
            replies.append(restrict_reply(draft.payload, targets[i], known) if targets is not None and known is not None
                           else draft.payload)
        if reqs and not replies and not plan.reused:
            smap.semantic = SemanticStatus(status, tier=tier, knowledge_sources=sources, error="; ".join(errors)[:2000])
            return f"semantic {status}; Claude {'; '.join(x for x in stats if x)}"
        r = apply_system_semantics(smap, replies)  # raises on a malformed shape; the caller keeps the static map
        written, asked = now_iso(), set(plan.ask)
        for c in smap.capabilities:  # texts written now: their age and change count start over
            if c.id in asked and c.text_basis is not None and c.business_name is not None and c.business_name.source == "semantic":
                c.text_basis = TextBasis(c.text_basis.surface, written, ctx.pr.merge_base_sha)
        dropped, reasons, notes = r.dropped + plan.dropped, plan.reasons[:3] + r.reasons[:5], []
        if smap.system.summary is None and plan.summary_fallback is not None:
            smap.system.summary = plan.summary_fallback
            notes.append("system summary: kept the earlier map's (no new one was written)")

        phase_ids = plan.phases_ask
        phase_reqs = [SemanticRequest("phases", tier, lang, phase_digest(smap, ids_), ws.base_root)
                      for ids_ in phase_batches(smap, phase_ids, cfg.phase_batch, cfg.phase_chars)]
        dom_drafts: list = []
        with ThreadPoolExecutor(max_workers=max(1, min(cfg.semantic_parallel, len(phase_reqs) + 1)), thread_name_prefix="claude-map2") as pool:
            dom_job = pool.submit(self._domains, smap, lang, ws.base_root, docs or [], dom_drafts) if need_domains else None
            phase_drafts = list(pool.map(self.d.semantic.analyze, phase_reqs))
            dom = dom_job.result() if dom_job else None
        stats += [call_stats("domains", dom_drafts), call_stats("phases", phase_drafts)]
        if dom is not None:
            dropped += dom[1]
            reasons += dom[2][:3]
            notes.append(dom[0])
            if dom[3]:
                errors.append(dom[3])
        phase_replies = []
        for d in phase_drafts:
            if d.status == "done":
                phase_replies.append(d.payload)
            else:
                errors.append(f"phases: {d.error or d.status}")
        if phase_replies:
            pr_ = apply_phases(smap, phase_replies, only=set(phase_ids))
            dropped += pr_.dropped
            reasons += pr_.reasons[:5]
            notes.append(f"phases for {sum(1 for f in smap.flows if f.phases)} flow(s)")
        err = "; ".join(errors + reasons) or None
        smap.semantic = SemanticStatus("done", tier=tier, dropped_claims=dropped, knowledge_sources=sources,
                                       error=(f"partial: {err}" if errors else (f"dropped {dropped} claim(s): {err}" if dropped else None)))
        failed = sum(1 for d in drafts if d.status != "done")
        return (f"map semantic done in {len(reqs)} parallel call(s) for {len(plan.ask)} capabilities"
                + (f" + {sum(len(v) for v in plan.partial.values())} element(s)" if plan.partial else "")
                + f" ({len(plan.reused)} reused" + (f"; {failed} call(s) failed" if failed else "") + f"); {'; '.join(notes)}; "
                f"dropped {dropped} claim(s); Claude {'; '.join(x for x in stats if x)}")

    def _domains(self, smap: SystemMap, lang: str, root: Path, docs: list,
                 drafts: list | None = None) -> tuple[str, int, list[str], str | None]:
        """Domain step: (message, dropped, reasons, call error). Retried once with the validator's problems; static
        domains (already in the map) stay when both replies are rejected. Never raises. `drafts` collects the calls."""
        data = domain_digest(smap, docs)
        problems: list[str] = []
        for attempt in (1, 2):
            req = SemanticRequest("domains", "standard", lang, {**data, **({"previous_reply_problems": problems} if problems else {})}, root)
            draft = self.d.semantic.analyze(req)
            if drafts is not None:
                drafts.append(draft)
            if draft.status != "done":
                return "domains: static (Claude call failed)", 0, [], f"domains: {draft.error or draft.status}"
            gate = gate_domains(smap, draft.payload)
            if not gate.problems:
                apply_domains(smap, gate)
                retry = " after one retry" if attempt == 2 else ""
                return f"{len(gate.domains)} domains by Claude{retry}", gate.dropped, gate.reasons, None
            problems = gate.problems
        return ("domains: static fallback, Claude's assignment rejected twice (" + "; ".join(problems[:3]) + ")")[:300], \
            1, [f"domains rejected: {problems[0]}"[:200]], None

    # ------------------------------------------------------------------ L2/L3 for the PR
    def stage_pr_static(self, ctx: Context) -> str:
        ws = ctx.workspace
        assert ws is not None and ctx.map_doc is not None
        ctx.files = self.d.diff_parser.parse(ws.diff_text)
        base = ctx.sides.get("base") or self._deep(ctx, "base")
        head = self._deep(ctx, "head")
        changed = {f.path for f in ctx.files} | {f.old_path for f in ctx.files if f.old_path}
        for side, sg, root in (("base", base, ws.base_root), ("head", head, ws.head_root)):
            ok = ctx.deep_ok[side]
            todo = sorted(p for p in changed if p in sg.files and not any(
                an.language in ok and an.claims(p) for an in self.d.analyzers))
            if todo:
                sg.merge(self.d.generic.analyze(root, todo, side, sg.files))
        for f in ctx.files:
            side, path = f.side_path
            refs = [
                CodeRef(side, path, max(h.new_start if side == "head" else h.old_start, 1),
                        max((h.new_start + h.new_lines - 1) if side == "head" else (h.old_start + h.old_lines - 1), 1))
                for h in f.hunks if (h.new_lines if side == "head" else h.old_lines) > 0
            ] or [CodeRef(side, path, 1)]
            ctx.diff_evidence[f.path] = Evidence(
                f"ev:diff:{f.path}", "diff", f"{f.path}: {f.status}, +{f.additions}/-{f.deletions} in {len(f.hunks)} hunk(s)",
                refs[:20], DIFF_PRODUCER)
        graph = diff_graphs(base, head)
        for ev in ctx.diff_evidence.values():
            graph.evidence[ev.id] = ev
        by_path: dict[str, list] = {}
        for n in graph.nodes.values():
            if n.level == "module" and n.code_refs:
                by_path.setdefault(n.code_refs[0].path, []).append(n)
        for f in ctx.files:
            for n in by_path.get(f.path, []) + (by_path.get(f.old_path, []) if f.old_path else []):
                if f"ev:diff:{f.path}" not in n.evidence_refs:
                    n.evidence_refs.append(f"ev:diff:{f.path}")
        ctx.graph = graph
        # Structures are cheap (O(nodes + edges)) and content-derived, so they reproduce the cached map's IDs. Both
        # sides use the map's capability splits, so a PR crossing the split threshold does not regroup the map.
        paths = {c["id"]: c["path"] for c in ctx.map_doc["capabilities"]}
        splits = {c["path"]: paths.get(c["parent_capability_id"], c["parent_capability_id"].removeprefix("cap:"))
                  for c in ctx.map_doc["capabilities"] if c.get("parent_capability_id")}
        ctx.base_st = build_structure(base, self.d.language_of, splits=splits)
        ctx.head_st = build_structure(head, self.d.language_of, splits=splits)
        ctx.map_index = MapIndex.from_doc(ctx.map_doc)
        ctx.builder = OverlayBuilder(graph, ctx.base_st, ctx.head_st, ctx.map_index)
        ctx.builder.assign_capabilities(ctx.files)
        ctx.coverage = self._coverage(ctx, "head", [(f.side_path[1], f.is_binary) for f in ctx.files])
        unknown = sum(1 for e in graph.edges.values() if e.confidence == "unknown")
        mapped = sum(1 for f in ctx.files if f.capability_id)
        return (f"{len(ctx.files)} changed file(s), {mapped} mapped to a capability; graph {len(graph.nodes)} nodes, "
                f"{len(graph.edges)} edges ({unknown} unknown), {len(graph.changed_node_ids())} changed")

    # ------------------------------------------------------------------ L5 flows
    def stage_flows(self, ctx: Context) -> str:
        assert ctx.builder is not None
        fcs, nodes, flow_notes = ctx.builder.flow_changes(ctx.sides["base"], ctx.sides["head"])
        ctx.flow_changes, ctx.flow_nodes = fcs, nodes
        by = {s: sum(1 for f in fcs if f.change_status == s) for s in ("added", "modified", "removed")}
        budget = ctx.map_doc["flow_budget"] if ctx.map_doc else {}
        steps = {}
        for fc in fcs:
            for s in fc.steps:
                steps[s.step_change_status] = steps.get(s.step_change_status, 0) + 1
        return (f"{len(fcs)} flow change(s) {by}; step changes {steps}; map flow budget "
                f"{budget.get('flows_built')}/{budget.get('entry_points_total')} built"
                + "".join(f"; {n}" for n in flow_notes))

    # ------------------------------------------------------------------ L6 impact + findings + architecture
    def stage_impact(self, ctx: Context) -> str:
        g, ws, b = ctx.graph, ctx.workspace, ctx.builder
        assert g is not None and ws is not None and b is not None
        res = propagate_impact(g.nodes, g.edges, self.d.config.impact_max_depth, self.d.config.impact_max_nodes)
        # documented rules come from the merge-base: a PR cannot rewrite the rules that judge it (its edits to them
        # are a static finding, documented_rules_changed)
        # instruction rules are chosen by the PR's changed paths and the capabilities it touches (budget, relevance)
        touched = {f.capability_id for f in ctx.files if f.capability_id}
        topics = [t for c in (ctx.map_doc or {}).get("capabilities", []) if c["id"] in touched
                  for t in (c.get("technical_name") or "", (c.get("business_name") or {}).get("text") or "", c.get("path") or "")]
        ctx.knowledge = self.d.knowledge.collect(ws.base_root, ws.base_files, ctx.request.context_docs,
                                                 changed={f.path for f in ctx.files} | {f.old_path for f in ctx.files if f.old_path},
                                                 topics=topics)
        ctx.lang = self.d.config.language(ctx.pr.title, ctx.pr.body, *(d.text for d in ctx.knowledge.excerpts[:2]))
        findings, fev = find_static_issues(g, ctx.files, ctx.lang)
        b.map_findings(findings, ctx.flow_nodes, ctx.lang)
        ctx.findings = findings
        # rules recomputed on the base graph (same IDs as the map's), only those the map lists are applied
        in_map = {r["id"] for r in (ctx.map_doc or {}).get("architecture_rules", [])}
        base_contracts, _ = self.d.knowledge.contracts(ws.base_root, ws.base_files)
        head_contracts, _ = self.d.knowledge.contracts(ws.head_root, ws.head_files)
        rules = [r for r in infer_rules(ctx.sides["base"], ctx.base_st, base_contracts, ctx.lang).rules if r.id in in_map]
        checks = check_pr(g, ctx.base_st, ctx.head_st, rules, head_contracts, cap_ref=b.cap_ref, name=b.name, lang=ctx.lang)
        # interface rings (spec §8): both sides use the base's .pr-explorer.yml and import-linter contracts
        base_surface = self.d.knowledge.surface(ws.base_root, ws.base_files)
        head_surface = self.d.knowledge.surface(ws.head_root, ws.head_files)
        b_if = detect_interfaces(ctx.sides["base"], ctx.base_st, base_surface, base_surface.config, base_contracts, ctx.lang)
        h_if = detect_interfaces(ctx.sides["head"], ctx.head_st, head_surface, base_surface.config, base_contracts, ctx.lang)
        changed_paths = {f.path for f in ctx.files} | {f.old_path for f in ctx.files if f.old_path}
        iface_checks = diff_interfaces(
            b_if.interfaces, h_if.interfaces, head_sg=ctx.sides["head"], known=ctx.map_index.interfaces,
            changed_nodes=g.changed_node_ids(), changed_paths=changed_paths, iface_ref=b.iface_ref, cap_ref=b.cap_ref,
            diff_ev={p: e.id for p, e in ctx.diff_evidence.items()}, lang=ctx.lang)
        ctx.arch_checks = sorted(iface_checks + checks.checks, key=check_order)
        doc_ev = [Evidence(ids.evidence_id("doc", "base", r.path, r.line, r.text), "doc", f"Documented in {r.path}:{r.line}",
                           [CodeRef("base", r.path, r.line)], DOC_PRODUCER) for r in ctx.knowledge.rules]
        ctx.extra_evidence = {**b_if.evidence, **h_if.evidence, **{e.id: e for e in fev + checks.evidence + doc_ev}}
        ctx.impacts = b.impacts(ctx.flow_changes, {p: e.id for p, e in ctx.diff_evidence.items()})
        counts: dict[str, int] = {}
        for i in ctx.impacts:
            counts[f"{i.target_kind}:{i.impact_status}"] = counts.get(f"{i.target_kind}:{i.impact_status}", 0) + 1
        trunc = " (node budget reached)" if res.truncated else ""
        verdicts = {v: sum(1 for c in checks.checks if c.verdict == v) for v in ("violates", "new_debt", "follows")}
        compat = {k: sum(1 for c in iface_checks if c.interface_change.compatibility == k) for k in ("breaking", "unknown", "compatible")}
        return (f"{len(ctx.impacts)} impact(s){trunc}; {len(findings)} static finding(s); {len(iface_checks)} interface "
                f"change(s) {compat}; {len(rules)} architecture rule(s) applied: {verdicts}")

    # ------------------------------------------------------------------ L7 Claude call b
    def _pr_evidence(self, ctx: Context) -> Evidence:
        return Evidence(PR_EVIDENCE, "pr_metadata", f"PR title and description ({ctx.pr.source})", [], PR_PRODUCER)

    def _known(self, ctx: Context) -> PrKnown:
        doc = ctx.map_doc
        assert doc is not None and ctx.builder is not None
        elements: dict[str, list[str]] = {}
        for key in ("capabilities", "entities", "entry_points", "components", "flows"):
            for el in doc[key]:
                elements[el["id"]] = list(el["evidence_refs"])
        for el in doc.get("interfaces", []):
            elements[el["id"]] = list(el["evidence_refs"])
        ha = ctx.builder.ha
        for coll in (ha.capabilities, ha.entities, ha.entry_points, ha.components, ha.flows, ha.interfaces):
            for el in coll:
                elements[el.id] = list(el.evidence_refs)
        steps: set[str] = set()
        for fc in ctx.flow_changes:
            elements.setdefault(fc.flow_id, list(fc.evidence_refs))
            for s in fc.steps:
                elements[s.id] = list(s.evidence_refs)
                steps.add(s.id)
        for f in ctx.files:
            elements[f.id] = [ctx.diff_evidence[f.path].id]
        for f in ctx.findings:
            elements[f.id] = list(f.evidence_refs)
        for c in ctx.arch_checks:  # Claude may cite the static architecture checks it explains
            elements[c.id] = list(c.evidence_refs)
        elements["pr"] = [PR_EVIDENCE]
        evidence = {e.id: e for e in self._pool(ctx).values()}
        for e in doc["evidence"]:
            evidence.setdefault(e["id"], Evidence(e["id"], e["kind"], e["description"],
                                                  [CodeRef(r["side"], r["path"], r["start_line"], r.get("end_line")) for r in e["refs"]],
                                                  Producer(e["producer"]["name"], e["producer"]["version"])))
        caps = {c["id"] for c in doc["capabilities"]} | {c.id for c in ha.capabilities}
        flows = {f["id"] for f in doc["flows"]} | {f.id for f in ha.flows} | {fc.flow_id for fc in ctx.flow_changes}
        additions = {el.id for coll in (ha.capabilities, ha.entities, ha.entry_points, ha.flows) for el in coll}
        interfaces = {i["id"]: i["ring"] for i in doc.get("interfaces", [])} | {i.id: i.ring for i in ha.interfaces}
        return PrKnown(elements, evidence, caps, flows, steps, ctx.files, additions,
                       flow_steps={fc.flow_id: [s.id for s in fc.steps] for fc in ctx.flow_changes}, interfaces=interfaces,
                       interface_checks={c.id for c in ctx.arch_checks if c.check == "interface_change"})

    def _pool(self, ctx: Context) -> dict[str, Evidence]:
        assert ctx.graph is not None
        structure = {**(ctx.base_st.evidence if ctx.base_st else {}), **(ctx.head_st.evidence if ctx.head_st else {})}
        return {**ctx.graph.evidence, **structure, **ctx.extra_evidence, **{e.id: e for e in ctx.diff_evidence.values()},
                PR_EVIDENCE: self._pr_evidence(ctx)}

    def _publish_static(self, ctx: Context, tier: str, decision, sources: list[str]) -> str:
        """Progressive results: store the static overlay (semantic pending) so the explorer opens while Claude runs."""
        ctx.semantic = SemanticStatus("pending", tier=tier, decision=decision, knowledge_sources=sources)
        try:
            self._assemble(ctx, False)
        except ValueError as exc:  # should not happen (the same overlay is stored without Claude at the end)
            return f"static overlay not published early: {exc}"[:200]
        if ctx.on_static is not None:
            ctx.on_static()
        return "static overlay published"

    def _use_map(self, ctx: Context, doc: dict, path: Path) -> None:
        """Lay the PR over the final (explained) map: same IDs as the pending one, new names and labels."""
        b = ctx.builder
        assert b is not None
        ctx.map_doc, ctx.map_path = doc, path
        ctx.map_index = b.mi = MapIndex.from_doc(doc)
        for fc in ctx.flow_changes:
            for s in fc.steps:
                lab = ctx.map_index.step_labels.get(s.id) if s.step_change_status == "unchanged" else None
                if lab:
                    s.business_label = Text(lab["text"], lab["source"], list(lab["evidence_refs"]))
        b.map_findings(ctx.findings, ctx.flow_nodes, ctx.lang)  # business impact in the map's business names

    def _pr_data(self, ctx: Context, tier: str) -> dict:
        """Claude call (b)'s data, from the map names in `ctx.map_index`."""
        ws, k = ctx.workspace, ctx.knowledge
        assert ws is not None and k is not None and ctx.map_index is not None and ctx.builder is not None
        mi = ctx.map_index
        ha = ctx.builder.ha
        names = {**mi.names, **{el.id: el.technical_name for coll in (ha.capabilities, ha.flows) for el in coll}}
        additions = [{"id": el.id, "kind": kind, "technical_name": el.technical_name,
                      **({"capability_id": el.capability_id} if getattr(el, "capability_id", None) else {})}
                     for kind, coll in (("capability", ha.capabilities), ("entity", ha.entities),
                                        ("entry_point", ha.entry_points), ("flow", ha.flows)) for el in coll]
        # only what this PR touches: the capabilities of its files and checks (a large repo can have hundreds of rules and thousands of interfaces)
        touched = {f.capability_id for f in ctx.files if f.capability_id} | {c for a in ctx.arch_checks for c in a.capability_ids}
        data = pr_digest(
            title=ctx.pr.title, body=ctx.pr.body, pr_evidence=PR_EVIDENCE, system_name=mi.system_name,
            system_summary=(mi.summary or {}).get("text"), names=names, files=ctx.files, impacts=ctx.impacts,
            flow_changes=ctx.flow_changes, findings=ctx.findings, diff_evidence={p: e.id for p, e in ctx.diff_evidence.items()},
            diff_excerpt=ws.diff_text, docs=k.excerpts, context_docs=ctx.request.context_docs, tier=tier,
            head_additions=additions,
            architecture_rules=[{"id": r["id"], "origin": r["origin"], "kind": r["kind"], "title": r["title"],
                                 **({"statistic": r["statistic"]} if r.get("statistic") else {}),
                                 "evidence_id": r["evidence_refs"][0]}
                                for r in relevant_rules(ctx.map_doc.get("architecture_rules", []), touched, ctx.arch_checks)],
            architecture_checks=[{"id": c.id, "title": c.title, "verdict": c.verdict, "rule_id": c.rule_id, "ring": c.ring}
                                 for c in ctx.arch_checks if c.check != "interface_change"],
            interface_changes=[{"check_id": c.id, "interface_id": c.interface_change.interface_id, "ring": c.ring,
                                "name": c.title, "change": c.interface_change.change,
                                "compatibility": c.interface_change.compatibility, "detail": c.interface_change.detail,
                                "capability_ids": c.capability_ids, "evidence_ids": c.evidence_refs[:4]}
                               for c in ctx.arch_checks if c.check == "interface_change"],
            architecture_docs=[{"evidence_id": ids.evidence_id("doc", "base", r.path, r.line, r.text), "path": r.path,
                                "line": r.line, "text": r.text,
                                **({"source": "instructions", "applies_to": scope or "(whole repository)"}
                                   if (scope := instruction_scope(r.path)) is not None else {})} for r in k.rules])
        return data

    def _early_names(self, ctx: Context, flight: "MapFlight") -> MapIndex | None:
        """The map's names as the final map will hold them for everything the PR call shows (the names of the elements
        it lists, the step labels of its flows, the system summary), as soon as the calls writing those returned —
        without waiting for the other capability calls, domains or phases. None (wait for the whole map, as before)
        when one of them failed or did not write what it should have."""
        e = flight.early
        if e is None or len(flight.calls) != len(e.targets):
            return None
        need = ({i.target_id for i in ctx.impacts} | {fc.flow_id for fc in ctx.flow_changes}
                | {s.id for fc in ctx.flow_changes for s in fc.steps})
        wanted = sorted({i for i, t in enumerate(e.targets) if t & need} | ({e.summary_call} if e.summary_call is not None else set()))
        replies = []
        for i in wanted:
            try:
                d = flight.calls[i].result()
            except Exception:
                return None
            if d.status != "done":
                return None
            replies.append(restrict_reply(d.payload, e.targets[i], e.known))
        m = _light_copy(e.pending)
        try:
            apply_system_semantics(m, replies)  # same replies, same order, same map as the final map's step 1
        except Exception:
            return None
        if e.summary_call is not None and m.system.summary is None:
            return None  # the summary call wrote none: the final map's summary depends on the other calls
        written = set().union(*(e.targets[i] for i in wanted)) if wanted else set()
        mi = copy.copy(ctx.map_index)
        assert mi is not None
        mi.names, mi.step_labels = dict(mi.names), dict(mi.step_labels)
        for coll in (m.capabilities, m.entities, m.entry_points, m.flows):
            for el in coll:
                if el.id in written:
                    mi.names[el.id] = el.business_name.text if el.business_name else el.technical_name
        for f in m.flows:
            for st in f.timeline.steps:
                if st.id in written and st.business_label:
                    t = st.business_label
                    mi.step_labels[st.id] = {"text": t.text, "source": t.source, "evidence_refs": list(t.evidence_refs)}
        if m.system.summary is not None:
            t = m.system.summary
            mi.summary = {"text": t.text, "source": t.source, "evidence_refs": list(t.evidence_refs)}
        return mi

    def _use_index(self, ctx: Context, mi: MapIndex) -> None:
        """Like `_use_map` with names that are not stored yet (the early PR call); the map document stays the pending one."""
        b = ctx.builder
        assert b is not None
        ctx.map_index = b.mi = mi
        for fc in ctx.flow_changes:
            for s in fc.steps:
                lab = mi.step_labels.get(s.id) if s.step_change_status == "unchanged" else None
                if lab:
                    s.business_label = Text(lab["text"], lab["source"], list(lab["evidence_refs"]))
        b.map_findings(ctx.findings, ctx.flow_nodes, ctx.lang)

    def stage_semantic(self, ctx: Context) -> str:
        g, ws, k = ctx.graph, ctx.workspace, ctx.knowledge
        assert g is not None and ws is not None and k is not None and ctx.map_index is not None
        result = score_change(ctx.files, g)
        decision, tier = decide(ctx.request.semantic_mode, result)
        sources = list(dict.fromkeys(["pr:metadata", "pr:diff"] + k.sources))
        flight = ctx.map_flight
        notes: list[str] = list(k.notes)
        if tier != "none" or (flight is not None and not flight.final.done()):
            notes.append(self._publish_static(ctx, tier, decision, sources))
        early = None
        if flight is not None and tier != "none" and self.d.config.pr_early_start and not flight.final.done():
            early = self._early_names(ctx, flight)  # waits only for the map calls writing what the PR call shows
        if flight is not None and early is None:
            doc, path, map_msg = flight.final.result()  # Claude call (a) for the map: the PR call uses its names
            if map_msg:
                notes.append(map_msg)
            self._use_map(ctx, doc, path)
        if tier == "none":
            ctx.semantic = SemanticStatus("skipped", tier="none", decision=decision, knowledge_sources=sources)
            raise StageSkipped("; ".join([f"score {result.score:g}; mode {decision.mode}"] + notes))
        if early is None:
            draft = self.d.semantic.analyze(SemanticRequest("pr", tier, ctx.lang, self._pr_data(ctx, tier), ws.head_root))
        else:
            assert flight is not None
            labels = [(s, s.business_label) for fc in ctx.flow_changes for s in fc.steps]
            self._use_index(ctx, early)
            data = self._pr_data(ctx, tier)
            draft = self.d.semantic.analyze(SemanticRequest("pr", tier, ctx.lang, data, ws.head_root))
            doc, path, map_msg = flight.final.result()  # the overlay is still laid over the final map, as before
            if map_msg:
                notes.append(map_msg)
            for st, lab in labels:
                st.business_label = lab
            self._use_map(ctx, doc, path)
            final = self._pr_data(ctx, tier)
            if final == data:
                notes.append("PR call started before the map's remaining calls finished")
            else:  # the stored map differs from what the early call was shown (e.g. it failed validation): ask again
                notes.append(call_stats("PR (early, superseded)", [draft]))
                draft = self.d.semantic.analyze(SemanticRequest("pr", tier, ctx.lang, final, ws.head_root))
        notes.append(call_stats("PR", [draft]))
        if draft.status != "done":
            ctx.semantic = SemanticStatus(draft.status, tier=tier, decision=decision, knowledge_sources=sources,
                                          error=draft.error or draft.status)
            return "; ".join([f"semantic {draft.status}: {draft.error or ''}; static overlay still complete"] + notes)
        try:
            gate = gate_pr(draft.payload, self._known(ctx))
        except Exception as exc:  # Claude's reply is untrusted input: any malformed shape degrades to static (spec §13)
            ctx.semantic = SemanticStatus("failed", tier=tier, decision=decision, knowledge_sources=sources,
                                          error=f"semantic reply rejected: {type(exc).__name__}: {exc}"[:2000])
            return "semantic failed on a malformed reply; static overlay still complete"
        ctx.pr_semantic = gate
        err = "; ".join(gate.reasons[:5])
        ctx.semantic = SemanticStatus("done", tier=tier, decision=decision, dropped_claims=gate.dropped, knowledge_sources=sources,
                                      error=(f"dropped {gate.dropped} ungrounded claim(s): {err}" if gate.dropped else draft.error))
        return "; ".join([f"tier {tier}; {len(gate.behavior_changes)} behavior change(s), {len(gate.findings)} semantic "
                          f"finding(s), {len(gate.step_labels)} step label(s), {len(gate.names)} new element name(s); "
                          f"dropped {gate.dropped} claim(s)"] + notes)

    # ------------------------------------------------------------------ assemble
    def stage_assemble(self, ctx: Context) -> str:
        """The static result must survive anything Claude says: if the overlay WITH semantic content fails validation,
        re-assemble without it and record why in semantic.error."""
        assert ctx.semantic is not None
        with_semantic = ctx.semantic.status == "done" and ctx.pr_semantic is not None
        try:
            self._assemble(ctx, with_semantic)
        except ValueError as exc:  # DocumentValidationError
            if not with_semantic:
                raise
            s = ctx.semantic
            ctx.semantic = SemanticStatus("failed", tier=s.tier, decision=s.decision, dropped_claims=s.dropped_claims,
                                          knowledge_sources=s.knowledge_sources,
                                          error=f"semantic output discarded, overlay failed validation: {exc}"[:2000])
            self._assemble(ctx, False)
            return f"overlay {ctx.overlay_id} validated and stored without semantic content ({exc})"[:500]
        return f"overlay {ctx.overlay_id} validated against {ctx.system_map_id} and stored"

    def _assemble(self, ctx: Context, with_semantic: bool) -> None:
        ws, b, mi, doc = ctx.workspace, ctx.builder, ctx.map_index, ctx.map_doc
        assert ws is not None and b is not None and mi is not None and doc is not None and ctx.semantic is not None
        sem = ctx.pr_semantic if with_semantic else None
        flow_changes = copy.deepcopy(ctx.flow_changes)
        flows_ha = copy.deepcopy(b.ha.flows)
        if sem is not None:
            for fc in flow_changes:
                for s in fc.steps:
                    if s.id in sem.step_labels:
                        s.business_label = sem.step_labels[s.id]
            for f in flows_ha:
                for s in f.timeline.steps:
                    if s.id in sem.step_labels:
                        s.business_label = sem.step_labels[s.id]
        for fc in flow_changes:  # Claude's phases over the merged steps, else the map flow's when the steps line up
            sids = [s.id for s in fc.steps]
            got = (sem.phases.get(fc.flow_id) if sem else None) or mi.phases.get(fc.flow_id)
            fc.phases = got if got and [x for p in got for x in p.step_ids] == sids else None
        for f in flows_ha:
            fc = next((x for x in flow_changes if x.flow_id == f.id and x.phases), None)
            if fc is not None and [x for p in fc.phases for x in p.step_ids] == [s.id for s in f.timeline.steps]:
                f.phases = fc.phases
        pr = self._pr(ctx)
        purpose = (sem.purpose if sem and sem.purpose else
                   Text((ctx.pr.title + ("\n\n" + ctx.pr.body.strip() if ctx.pr.body.strip() else "")).strip() or "(no description)",
                        "pr_description", [PR_EVIDENCE]))
        new_names = {i: t["business_name"].text for i, t in (sem.names if sem else {}).items() if "business_name" in t}
        names = lambda i: new_names.get(i) or mi.names.get(i) or b.name(i)  # noqa: E731
        behavior = sem.behavior_changes if sem and sem.behavior_changes else static_behavior_changes(flow_changes, names, ctx.lang)
        severity = {"warning": 0, "attention": 1, "info": 2}
        findings = sorted(ctx.findings + (sem.findings if sem else []), key=lambda f: (f.source != "static", severity[f.severity], f.id))
        known = self._known(ctx)
        caps_order = ([c["id"] for c in doc["capabilities"] if c.get("category", "business") == "business"]
                      + [c.id for c in b.ha.capabilities if c.category == "business"])
        summary = doc["system"].get("summary")
        walkthrough = build_walkthrough(
            lang=ctx.lang, system_name=mi.system_name,
            system_summary=Text(summary["text"], summary["source"], list(summary["evidence_refs"])) if summary else None,
            capability_ids=caps_order, purpose=purpose, behavior_changes=behavior, impacts=ctx.impacts,
            flow_changes=flow_changes, findings=findings, name=names, evidence_of=lambda i: known.elements.get(i, []),
            fallback_ev=[PR_EVIDENCE], semantic=sem.walkthrough if sem else None, semantic_questions=sem.questions if sem else None,
        )
        head_additions = copy.copy(b.ha)
        head_additions.flows = flows_ha
        if sem is not None and sem.names:  # Claude's names for elements that exist only on the PR head
            for attr in ("capabilities", "entities", "entry_points", "flows"):
                items = [copy.copy(el) for el in getattr(head_additions, attr)]
                for el in items:
                    t = sem.names.get(el.id, {})
                    el.business_name = t.get("business_name") or el.business_name
                    el.description = t.get("description") or el.description
                setattr(head_additions, attr, items)
        overlay = PrOverlay(
            analyzer_version=ctx.analyzer_version, overlay_id=ctx.overlay_id, system_map_id=ctx.system_map_id,
            generated_at=now_iso(), pr=pr, coverage=ctx.coverage, changes=ChangesPayload(ctx.files), purpose=purpose,
            behavior_changes=behavior, impacts=ctx.impacts, flow_changes=flow_changes, head_additions=head_additions,
            findings=findings, architecture_checks=sorted(self._with_notes(ctx.arch_checks, sem) + (sem.hypotheses if sem else []),
                                                          key=check_order), evidence=[],
            semantic=ctx.semantic, walkthrough=walkthrough,
            design_rationale=sem.design_rationale if sem else None,
        )
        in_map = {e["id"] for e in doc["evidence"]}
        pool = self._pool(ctx)
        overlay.evidence = [e for e in referenced_evidence([overlay], pool) if e.id not in in_map]
        ctx.overlay_path = self.d.store.save_overlay(overlay, doc, {"base": ws.base_files, "head": ws.head_files})

    @staticmethod
    def _with_notes(checks: list[ArchCheck], sem: PrSemantic | None) -> list[ArchCheck]:
        """Claude's grounded note on who an interface change affects replaces the static one (copies: the static
        overlay must stay as computed)."""
        if sem is None or not sem.interface_notes:
            return list(checks)
        out = []
        for c in checks:
            note = sem.interface_notes.get(c.id)
            if note is not None:
                c = copy.copy(c)
                c.business_impact = note.text
            out.append(c)
        return out

    @staticmethod
    def _pr(ctx: Context) -> PullRequest:
        p = ctx.pr
        return PullRequest(
            source=p.source, title=p.title, body=p.body, repo=Repo(p.repo_name, p.repo_owner), base_ref=p.base_ref,
            head_ref=p.head_ref, base_sha=p.base_sha, head_sha=p.head_sha, merge_base_sha=p.merge_base_sha,
            url=p.url, number=p.number, author=p.author,
        )


__all__ = ["STAGES", "Context", "MapFlight", "Pipeline", "PipelineConfig", "PipelineDeps", "StageSkipped"]
