"""Follow-up questions about an analyzed PR (docs/api.md「Questions」).

One question = one Claude call (kind `ask`) over a digest of the stored overlay plus the PR diff. The PR's worktrees are
materialized from the git cache for the duration of that one answer (Claude may Read the head, read-only) and released
afterwards, like an analysis. At most one unfinished question per overlay (bounds cost)."""

from __future__ import annotations

import threading
import uuid
from typing import Callable

from ...domain.model.semantic import ENGINES, AiChoice
from ...domain.services.ask import QUESTION_MAX, ask_digest, cite_index, gate_answer
from ..ports import (AnalysisRepository, DocumentStore, QuestionRepository, ResolvedPr, SemanticAnalyzer, SemanticRequest,
                     SourceError, SourceProvider)
from .errors import Conflict, InvalidInput, NotFound

ASK_TIER = "standard"  # --max-turns for the call
UNFINISHED = ("queued", "running")


def pr_from_index(idx: dict, overlay: dict) -> ResolvedPr:
    """The stored overlay's PR bound to its analyzed commits (enough to materialize its worktrees again)."""
    pr = overlay.get("pr", {})
    repo = pr.get("repo", {})
    return ResolvedPr(
        source=idx["descriptor"].get("kind", "local"), repo_key=idx["repo_key"], repo_name=repo.get("name", ""),
        repo_owner=repo.get("owner"), base_ref=pr.get("base_ref", ""), head_ref=pr.get("head_ref", ""),
        base_sha=idx["base_sha"], head_sha=idx["head_sha"], merge_base_sha=idx["merge_base_sha"],
        title=pr.get("title", ""), body=pr.get("body") or "", url=pr.get("url"), number=pr.get("number"),
        descriptor=idx["descriptor"])


def public(q: dict) -> dict:
    return {"question_id": q["id"], "overlay_id": q["overlay_id"], "created_at": q["created_at"], "question": q["question"],
            "selected_id": q["selected_id"], "status": q["status"], "answer": q["answer"], "error": q["error"]}


def overlay_ai(overlay: dict) -> AiChoice | None:
    """The AI that wrote the overlay answers its questions too (None: overlays from before 2.6.0 → server default)."""
    ai = (overlay.get("semantic") or {}).get("ai")
    if not isinstance(ai, dict) or ai.get("engine") not in ENGINES:
        return None
    return AiChoice(ai["engine"], ai.get("model"), ai.get("effort"))


class AskQuestion:
    def __init__(self, repo: AnalysisRepository, questions: QuestionRepository, store: DocumentStore, source: SourceProvider,
                 semantic: SemanticAnalyzer, language: Callable[..., str], scrub: Callable[[str], str]):
        self.repo = repo
        self.questions = questions
        self.store = store
        self.source = source
        self.semantic = semantic
        self.language = language  # PipelineConfig.language: the same output-language setting as analyses
        self.scrub = scrub  # answers and questions are stored (and shared) like documents: secrets redacted
        self._lock = threading.Lock()

    def _docs(self, overlay_id: str) -> tuple[dict, dict, dict]:
        idx = self.repo.get_overlay_index(overlay_id)
        overlay = self.store.load_overlay(overlay_id) if idx else None
        smap = self.store.load_system_map(overlay["system_map_id"]) if overlay else None
        if idx is None or overlay is None or smap is None:
            raise NotFound(f"overlay {overlay_id} not found")
        return idx, overlay, smap

    def start(self, overlay_id: str, question: str, selected_id: str | None = None) -> dict:
        question = (question or "").strip()
        if not 1 <= len(question) <= QUESTION_MAX:
            raise InvalidInput(f"question must be 1–{QUESTION_MAX} characters")
        self._docs(overlay_id)
        with self._lock:  # check + insert atomically: one unfinished question per overlay
            if any(q["status"] in UNFINISHED for q in self.questions.list_questions(overlay_id)):
                raise Conflict("another question about this PR is still being answered; wait for it to finish")
            q = self.questions.create_question(f"q_{uuid.uuid4().hex[:16]}", overlay_id, self.scrub(question),
                                               (selected_id or "").strip()[:300] or None)
        return {"question_id": q["id"], "status": q["status"]}

    def list(self, overlay_id: str) -> list[dict]:
        if self.repo.get_overlay_index(overlay_id) is None:
            raise NotFound(f"overlay {overlay_id} not found")
        return [public(q) for q in self.questions.list_questions(overlay_id)]

    def run(self, question_id: str) -> None:
        q = self.questions.get_question(question_id)
        if q is None:
            return
        self.questions.update_question(question_id, "running")
        held: ResolvedPr | None = None  # set only once materialize succeeded (it releases itself when it fails)
        answer: dict | None = None
        error: str | None = None
        try:
            idx, overlay, smap = self._docs(q["overlay_id"])
            pr = pr_from_index(idx, overlay)
            try:
                ws = self.source.materialize(pr)
                held = pr
            except SourceError as exc:
                raise RuntimeError("the PR's commits are not in the local git cache (e.g. a shared result); analyze "
                                   f"this PR on this machine first ({exc})") from exc
            history = [h for h in self.questions.list_questions(q["overlay_id"])
                       if h["status"] == "done" and h["created_at"] <= q["created_at"] and h["id"] != q["id"]]
            data = ask_digest(overlay, smap, question=q["question"], diff=ws.diff_text, selected_id=q["selected_id"],
                              history=history)
            pr_meta = overlay.get("pr", {})
            lang = self.language(pr_meta.get("title", ""), pr_meta.get("body") or "", q["question"])
            draft = self.semantic.analyze(SemanticRequest("ask", ASK_TIER, lang, data, ws.head_root, overlay_ai(overlay)))
            if draft.status != "done":
                raise RuntimeError(f"Claude {draft.status}: {draft.error or ''}".strip())
            answer = gate_answer(draft.payload, cite_index(overlay, smap))
            if not answer["text"]:
                raise RuntimeError("Claude returned an empty answer")
            answer["text"] = self.scrub(answer["text"])
        except Exception as exc:  # recorded, surfaced through GET .../questions
            answer, error = None, f"{exc}"[:1000] or type(exc).__name__
        finally:
            if held is not None:  # the worktrees exist only for this answer; the bare cache keeps the commits
                try:
                    self.source.release(held)
                except Exception:
                    pass
        # written after the release: once a client sees the result, nothing of this answer is held any more
        self.questions.update_question(question_id, "done" if answer else "failed", answer=answer, error=error)
