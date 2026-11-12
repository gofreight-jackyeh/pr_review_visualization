"""Review-focus quality: instruction rules chosen by relevance with their own budget, precise per-finding locations,
rule-backed findings, the own-data permission false positive, structured output (--json-schema)."""

import json

from fakes import RecordingRunner

from pr_explorer.application.ports import SemanticRequest
from pr_explorer.domain.model.changes import ChangedFile, Hunk
from pr_explorer.domain.model.graph import CodeRef, Evidence, Producer
from pr_explorer.domain.services.semantic_gate import MAX_CITE_LINES, PrKnown, gate_pr
from pr_explorer.infrastructure.knowledge.local_docs import LocalDocsKnowledge
from pr_explorer.infrastructure.process.runner import RunResult
from pr_explorer.infrastructure.semantic.claude_cli import ClaudeCliSemanticAnalyzer, parse_reply, reply_schema


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# ---------------------------------------------------------------- rule selection

DOMAINS = ["billing", "invoice", "quote", "catalog", "cart", "checkout", "warehouse", "suppliers", "rates", "payments",
           "accounting", "reports", "auth", "users", "notifications", "documents", "inventory", "pricing", "vendors",
           "contacts", "schedules", "stores", "returns", "reviews", "coupons", "loyalty", "webhooks", "search", "exports",
           "imports", "audits", "settings", "tasks", "crm", "sales", "leads", "claims", "subscriptions", "dashboard"]


def _big_repo(tmp_path):
    repo = tmp_path / "repo"
    files = set()
    # a large root CLAUDE.md: many rules, none about analytics
    root_rules = "\n".join(f"- Always keep the {d} module free of cross-domain imports, rule {i}" for i, d in
                           enumerate(DOMAINS * 3))
    _write(repo, "CLAUDE.md", "# Project\n\n## Conventions\n" + root_rules + "\n")
    files.add("CLAUDE.md")
    for d in DOMAINS:  # 39 domain rule files + analytics = 40
        rel = f".claude/rules/{d}/{d}.md"
        _write(repo, rel, f"# {d.title()} rules\n\n" + "\n".join(f"- {d} rule number {i} must be followed" for i in range(4)) + "\n")
        files.add(rel)
    _write(repo, ".claude/rules/analytics/analytics.md",
           "# Analytics\n\n" + "\n".join(f"- filler statement {i} about events" for i in range(30))
           + "\n- Never send analytics events from the client without the consent flag\n")
    files.add(".claude/rules/analytics/analytics.md")
    _write(repo, ".claude/rules/api.md", "---\npaths:\n  - api/**/*.py\n---\n# API\n\n- Public endpoints need a changelog entry\n")
    files.add(".claude/rules/api.md")
    _write(repo, "frontend/AGENTS.md", "# Frontend\n\n- Every component must have a story\n")
    _write(repo, "services/analytics/CLAUDE.md", "# Analytics service\n\n- Batch events before sending them upstream\n")
    files |= {"frontend/AGENTS.md", "services/analytics/CLAUDE.md"}
    # README / docs with many statements: they must not starve the instruction rules
    _write(repo, "README.md", "# Shop\n\n## Guidelines\n" + "\n".join(f"- You should read guide {i}" for i in range(60)) + "\n")
    files.add("README.md")
    return repo, files


def test_relevant_rules_are_kept_from_forty_rule_files(tmp_path):
    repo, files = _big_repo(tmp_path)
    changed = {"services/analytics/client.py", "services/analytics/tests/test_client.py"}
    k = LocalDocsKnowledge().collect(repo, files, [], changed=changed, topics=["analytics client"])
    got = {(r.path, r.text) for r in k.rules}
    analytics = ".claude/rules/analytics/analytics.md"
    # the rule that mattered sits at line 33 of a 40-file rule set, below 30 filler lines: kept by relevance
    assert (analytics, "Never send analytics events from the client without the consent flag") in got
    assert any(r.path == analytics and r.line == 33 for r in k.rules)
    # scoped to a changed directory: always kept; nested file of another directory / non-matching globs: not used
    assert ("services/analytics/CLAUDE.md", "Batch events before sending them upstream") in got
    assert not any(r.path in ("frontend/AGENTS.md", ".claude/rules/api.md") for r in k.rules)
    assert "repo:frontend/AGENTS.md" not in k.sources
    # separate budgets: README statements do not take instruction slots, and vice versa
    instr = [r for r in k.rules if r.path != "README.md"]
    readme = [r for r in k.rules if r.path == "README.md"]
    assert len(readme) == 25 and 30 <= len(instr) <= 41
    assert any("not used: 2" in n and "frontend/AGENTS.md" in n for n in k.notes)
    assert any("over budget" in n for n in k.notes)


def test_front_matter_globs_select_the_rule_file(tmp_path):
    repo, files = _big_repo(tmp_path)
    k = LocalDocsKnowledge().collect(repo, files, [], changed={"api/orders/views.py"})
    assert any(r.path == ".claude/rules/api.md" and "changelog" in r.text for r in k.rules)
    # without a PR (system map), nothing is filtered by scope
    k = LocalDocsKnowledge().collect(repo, files, [])
    assert "repo:frontend/AGENTS.md" in k.sources and not any("not used" in n for n in k.notes)


# ---------------------------------------------------------------- precise cites + rule-backed findings

def _known():
    rule_ev = "ev:doc:base:.claude/rules/analytics/analytics.md:33:abcd"
    ev = {
        "ev:diff:a.py": Evidence("ev:diff:a.py", "diff", "a.py modified", [CodeRef("head", "a.py", 1, 270)], Producer("git_diff", "1")),
        "ev:base:b.py": Evidence("ev:base:b.py", "ast", "b.py", [CodeRef("base", "b.py", 1)], Producer("python_ast", "2")),
        rule_ev: Evidence(rule_ev, "doc", "Documented in .claude/rules/analytics/analytics.md:33",
                          [CodeRef("base", ".claude/rules/analytics/analytics.md", 33)], Producer("local_docs", "1")),
    }
    hunks = [Hunk(10, 5, 10, 8), Hunk(200, 2, 205, 60)]  # head lines 10–17 and 205–264
    files = [ChangedFile("file:a.py", "a.py", "modified", "python", 60, 7, hunks)]
    elements = {"cap:a": ["ev:base:b.py"], "file:a.py": ["ev:diff:a.py"]}
    return PrKnown(elements, ev, {"cap:a"}, set(), set(), files), rule_ev


def test_precise_cites_inside_hunks_replace_whole_element_evidence():
    known, rule_ev = _known()
    payload = {"findings": [
        {"title": "Q1?", "severity": "attention", "focus": "release_dependency", "cites": ["file:a.py:12-15"]},
        {"title": "Q2?", "severity": "info", "cites": ["file:a.py:8", "file:a.py:205-400", rule_ev]},  # margin + capped
        {"title": "Q3?", "cites": ["file:a.py:100-120"]},  # outside every hunk, nothing else → dropped
        {"title": "Q4?", "cites": ["file:ghost.py:3", "file:a.py"]},  # unknown file cite dropped, claim kept (old refs)
    ], "architecture_hypotheses": [
        {"title": "H?", "check": "documented_rule", "cites": ["file:a.py:16-17", rule_ev]},
    ], "behavior_changes": [{"after": "x", "cites": ["file:a.py:11"]}]}
    out = gate_pr(payload, known)
    by = {f.title: f for f in out.findings}
    assert set(by) == {"Q1?", "Q2?", "Q4?"}
    assert by["Q1?"].refs == [CodeRef("head", "a.py", 12, 15)] and by["Q1?"].rule == "semantic_review:release_dependency"
    assert by["Q2?"].refs == [CodeRef("head", "a.py", 8), CodeRef("head", "a.py", 205, 205 + MAX_CITE_LINES - 1)]
    assert by["Q2?"].severity == "attention" and rule_ev in by["Q2?"].evidence_refs  # rule-backed: one step up
    assert by["Q1?"].severity == "attention" and by["Q2?"].rule == "semantic_review"
    assert by["Q4?"].refs == [CodeRef("head", "a.py", 1, 270)]  # no accepted precise cite: element evidence as before
    [h] = out.hypotheses
    assert h.pr_refs == [CodeRef("head", "a.py", 16, 17)] and h.existing_refs == [CodeRef("base", ".claude/rules/analytics/analytics.md", 33)]
    assert out.behavior_changes and out.behavior_changes[0].after.evidence_refs == ["ev:diff:a.py"]
    assert sum("not inside a changed hunk" in x for x in out.reasons) == 2


# ---------------------------------------------------------------- own-data view: no permission false positive

VIEWS = (
    "from django.contrib.auth.decorators import permission_required\n\n\n"
    "@permission_required('orders.view')\ndef order_list(request):\n    return None\n\n\n"
    "@permission_required('orders.view')\ndef order_detail(request, pk):\n    return pk\n\n\n"
    "@permission_required('orders.change')\ndef order_cancel(request, pk):\n    return pk\n\n\n"
    "def my_profile(request):\n    return request.user.profile{a}\n\n\n"
    "def edit_order(request, pk):\n    return request.user, pk{a}\n"
)
URLS = ("from django.urls import path\n\nfrom . import views\n\n"
        "urlpatterns = [path('o/', views.order_list), path('o/<pk>/', views.order_detail), "
        "path('o/<pk>/cancel/', views.order_cancel), path('me/', views.my_profile), path('o/<pk>/edit/', views.edit_order)]\n")


def test_own_profile_view_is_not_asked_for_permission_decorator(tmp_path):
    """Trial false positive: a missing @permission_required on a user's own profile page. A view taking only the
    request and reading request.user is skipped; any other departure is a question, not a verified fact."""
    from test_system_map import analyze

    from pr_explorer.domain.services.graph_diff import diff_graphs
    from pr_explorer.domain.services.static_findings import find_static_issues

    src = lambda a: {"orders/__init__.py": "", "orders/views.py": VIEWS.format(a=a), "orders/urls.py": URLS}  # noqa: E731
    base = analyze(tmp_path / "b", src(""), "base")
    head = analyze(tmp_path / "h", src(" or None"), "head")
    files = [ChangedFile("file:orders/views.py", "orders/views.py", "modified", "python", 2, 2, [Hunk(16, 2, 16, 2)])]
    findings, _ = find_static_issues(diff_graphs(base, head), files)
    flagged = {f.id: f for f in findings if f.rule == "view_missing_sibling_decorator"}
    assert not any("my_profile" in i for i in flagged), flagged
    [edit] = [f for i, f in flagged.items() if "edit_order" in i]
    assert edit.status == "needs_confirmation" and edit.title.startswith("Does edit_order")


# ---------------------------------------------------------------- structured output

def _req(tmp_path, kind="pr"):
    return SemanticRequest(kind, "light", "en", {"pr": {"title": "t", "body": "b"}}, tmp_path)


def test_json_schema_flag_and_both_envelope_shapes(tmp_path):
    payload = {"purpose": {"text": "p", "cites": ["pr"]}}
    structured = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "",
                             "structured_output": payload})
    runner = RecordingRunner(lambda a, i: structured)
    d = ClaudeCliSemanticAnalyzer(runner).analyze(_req(tmp_path))
    argv = runner.calls[0]["args"]
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema == reply_schema("pr") and schema["properties"]["findings"]["type"] == "array"
    assert "focus" in schema["properties"]["findings"]["items"]["properties"]
    assert reply_schema("ask")["required"] == ["answer"] and reply_schema("domains")["properties"]["domains"]["type"] == "array"
    assert argv[argv.index("--max-turns") + 1] == "5"  # light (4) + the structured-output turn
    assert d.status == "done" and d.payload == payload
    # the result-text envelope (no structured_output) still parses, as does a fenced reply
    assert parse_reply(json.dumps({"type": "result", "is_error": False, "result": json.dumps(payload)})) == payload
    assert parse_reply(json.dumps({"type": "result", "result": "```json\n" + json.dumps(payload) + "\n```"})) == payload


def test_cli_without_json_schema_is_called_again_without_it(tmp_path):
    def handler(args, prompt):
        if "--json-schema" in args:
            return RunResult(1, "", "error: unknown option '--json-schema'")
        return json.dumps({"type": "result", "is_error": False, "result": json.dumps({"answer": "a", "cites": []})})

    runner = RecordingRunner(handler)
    an = ClaudeCliSemanticAnalyzer(runner)
    d = an.analyze(_req(tmp_path, "ask"))
    assert d.status == "done" and d.payload["answer"] == "a" and d.calls == 1
    assert ["--json-schema" in c["args"] for c in runner.calls] == [True, False]
    an.analyze(_req(tmp_path, "ask"))
    assert "--json-schema" not in runner.calls[-1]["args"]  # remembered for the analyzer's lifetime
