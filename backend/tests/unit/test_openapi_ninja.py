"""Interface changes of Django Ninja services: a committed openapi.json (diffed per operation) and static Ninja detection
(routers, mounts, handler parameters, schema classes). Regression: a new `?customer_ref` query param on a Ninja list
API used to be reported as no interface change."""

import json

from test_system_map import analyze

from pr_explorer.domain.model.convention import RepoConfig
from pr_explorer.domain.services.graph_diff import diff_graphs
from pr_explorer.domain.services.rings import detect_interfaces, diff_interfaces
from pr_explorer.domain.services.system_structure import build_structure
from pr_explorer.infrastructure.analyzers.generic.languages import detect_language
from pr_explorer.infrastructure.knowledge.openapi import parse_spec
from pr_explorer.infrastructure.knowledge.repo_surface import parse_repo_config, scan_surface

SCHEMAS = (
    "from ninja import Schema\n\n\n"
    "class SupportTicketOut(Schema):\n    id: int\n    name: str\n\n\n"
    "class SupportTicketIn(Schema):\n    name: str\n    note: str = ''\n"
)
API = (
    "from typing import List\n\nfrom ninja import Router\n\n"
    "from app.support_tickets.schemas import SupportTicketIn, SupportTicketOut\n\n"  # absolute import from a src/ layout
    "router = Router()\n\n\n"
    "@router.get('/', response=List[SupportTicketOut])\n"
    "def list_support_tickets(request, status: str = None):\n    return []\n\n\n"
    "@router.post('/', response=SupportTicketOut)\n"
    "def create_support_ticket(request, payload: SupportTicketIn):\n    return {}\n"
)


def _spec(list_params: list[dict], in_fields: dict) -> str:
    out = {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}, "required": ["id", "name"]}
    doc = {
        "openapi": "3.1.0", "info": {"title": "Tasks", "version": "1"},
        "paths": {"/api/task-assignments/": {
            "get": {"operationId": "app_support_tickets_api_list_support_tickets", "parameters": list_params,
                    "responses": {"200": {"content": {"application/json": {"schema": {
                        "type": "array", "items": {"$ref": "#/components/schemas/SupportTicketOut"}}}}}}},
            "post": {"operationId": "app_support_tickets_api_create_support_ticket",
                     "requestBody": {"required": True, "content": {"application/json": {"schema": {
                         "$ref": "#/components/schemas/SupportTicketIn"}}}},
                     "responses": {"200": {"content": {"application/json": {"schema": {
                         "$ref": "#/components/schemas/SupportTicketOut"}}}}}},
        }, "/api/health": {"get": {"operationId": "health", "responses": {"204": {"description": "ok"}}}}},
        "components": {"schemas": {"SupportTicketOut": out, "SupportTicketIn": {
            "type": "object", "properties": in_fields, "required": ["name"]}}},
    }
    return json.dumps(doc, indent=2)


STATUS = {"in": "query", "name": "status", "required": False, "schema": {"anyOf": [{"type": "string"}, {"type": "null"}]}}
QREF = {"in": "query", "name": "customer_ref", "required": False, "schema": {"type": "string"}}
BASE = {
    "src/app/__init__.py": "",
    "src/app/support_tickets/__init__.py": "",
    "src/app/support_tickets/apps.py": "from django.apps import AppConfig\n",
    "src/app/support_tickets/schemas.py": SCHEMAS,
    "src/app/support_tickets/api.py": API,
    "src/app/api.py": ("from ninja import NinjaAPI\n\nfrom app.support_tickets.api import router as ta_router\n\n"
                       "api = NinjaAPI()\napi.add_router('/task-assignments', ta_router)\n"),
    "src/app/urls.py": "from django.urls import path\n\nfrom app.api import api\n\nurlpatterns = [path('api/', api.urls)]\n",
    "src/manage.py": "import sys\n",
    "openapi.json": _spec([STATUS], {"name": {"type": "string"}, "note": {"type": "string"}}),
}
# the PR: an optional `customer_ref` filter on the list API (compatible) and `note` removed from the create body (breaking)
HEAD = {
    **BASE,
    "src/app/support_tickets/api.py": API.replace("status: str = None)", "status: str = None, customer_ref: str = None)"),
    "src/app/support_tickets/schemas.py": SCHEMAS.replace("    note: str = ''\n", ""),
    "openapi.json": _spec([STATUS, QREF], {"name": {"type": "string"}}),
}


def _checks(tmp_path, base: dict, head: dict, name: str):
    b, h = analyze(tmp_path / f"b{name}", base), analyze(tmp_path / f"h{name}", head, side="head")
    bst = build_structure(b, detect_language)
    hst = build_structure(h, detect_language, splits=dict(bst.caps.subs))
    bs, hs = scan_surface(tmp_path / f"b{name}", set(base)), scan_surface(tmp_path / f"h{name}", set(head))
    bi, hi = detect_interfaces(b, bst, bs, bs.config), detect_interfaces(h, hst, hs, bs.config)
    checks = diff_interfaces(bi.interfaces, hi.interfaces, head_sg=h, known=set(bi.interfaces),
                             changed_nodes=diff_graphs(b, h).changed_node_ids(),
                             changed_paths={p for p in head if head[p] != base.get(p)}, iface_ref=lambda i: i.id,
                             cap_ref=lambda c: c, diff_ev={}, lang="en")
    return bi, {c.interface_change.interface_id: c for c in checks}


def test_ninja_routes_are_detected_with_full_path_and_contract(tmp_path):
    sg = analyze(tmp_path, BASE)
    eps = {(e.kind, e.label) for e in sg.entry_points}
    assert ("api", "/api/task-assignments/") in eps  # urls.py prefix + add_router prefix + operation path
    assert not any(label == "/api/" for _, label in eps)  # the `api.urls` mount itself is not an endpoint
    handler = sg.nodes["node:py:src/app/support_tickets/api.py::list_support_tickets"]
    assert handler.meta["api_methods"] == ["GET"]
    assert handler.meta["ninja"]["params"] == {"status": {"in": "query", "required": False, "type": "str"}}
    assert handler.meta["ninja"]["response"] == "node:py:src/app/support_tickets/schemas.py::SupportTicketOut"


def test_openapi_document_operations(tmp_path):
    spec, err = parse_spec(BASE["openapi.json"], "openapi.json")
    assert err is None
    ops = {(o.method, o.route): o for o in spec.operations}
    get = ops[("GET", "/api/task-assignments/")]
    assert get.params == {"status": {"in": "query", "required": False, "type": "null|string"}}
    assert get.response == {"id": {"required": True, "type": "integer"}, "name": {"required": True, "type": "string"}}
    assert get.request == {} and ops[("GET", "/api/health")].response == {}  # no body / 204
    post = ops[("POST", "/api/task-assignments/")]
    assert post.request == {"name": {"required": True, "type": "string"}, "note": {"required": False, "type": "string"}}
    assert BASE["openapi.json"].splitlines()[post.line - 1].strip().startswith('"post"')  # evidence = the spec line


def test_openapi_diff_classifies_each_operation(tmp_path):
    bi, checks = _checks(tmp_path, BASE, HEAD, "spec")
    # each operation is merged into the Ninja route it describes (path + method), so there is one interface per operation
    merged = [i for i in bi.interfaces.values() if "openapi" in i.meta["contract"] and i.meta.get("handler")]
    assert len(merged) == 2 and all(any(r.path == "openapi.json" for r in i.refs) for i in merged)
    health = bi.interfaces["iface:openapi:GET /api/health"]  # only in the document: the capability of its matched siblings
    assert (health.kind, health.ring, health.capability_id) == ("http_api", "outer", merged[0].capability_id)
    got = {k: (c.interface_change.compatibility, c.interface_change.detail) for k, c in checks.items()}
    lst = next(v for k, v in got.items() if "list_support_tickets" in k or k.endswith("ninja:GET:/"))
    create = next(v for k, v in got.items() if k.endswith("ninja:POST:/"))
    assert lst == ("compatible", "new optional query parameter customer_ref")
    assert create == ("breaking", "request field note removed")
    assert len(got) == 2  # the unchanged /api/health operation is not reported
    assert sorted(c.title for c in checks.values()) == ["Compatible interface change: GET /api/task-assignments/",
                                                         "May break clients: POST /api/task-assignments/"]


def test_ninja_signature_alone_without_a_document(tmp_path):
    base = {k: v for k, v in BASE.items() if k != "openapi.json"}
    head = {k: v for k, v in HEAD.items() if k != "openapi.json"}
    _, checks = _checks(tmp_path, base, head, "ninja")
    got = {k.rsplit(":", 1)[0].rsplit("ninja:", 1)[-1]: (c.interface_change.compatibility, c.interface_change.detail)
           for k, c in checks.items()}
    assert got == {"GET": ("compatible", "new optional query parameter customer_ref"),
                   "POST": ("breaking", "request field note removed")}


def test_configured_openapi_path(tmp_path):
    cfg = parse_repo_config("openapi:\n  - docs/api-v1.json\n")
    assert cfg.openapi == [("docs/api-v1.json", 2)] and cfg.notes == []
    assert parse_repo_config("openapi: [a.json, b.json]\n").openapi == [("a.json", 1), ("b.json", 1)]
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/api-v1.json").write_text(BASE["openapi.json"])
    (tmp_path / ".pr-explorer.yml").write_text("openapi:\n  - docs/api-v1.json\n  - missing.json\n")
    surf = scan_surface(tmp_path, {"docs/api-v1.json", ".pr-explorer.yml"})
    assert [s.path for s in surf.api_specs] == ["docs/api-v1.json"] and surf.api_specs[0].configured_line == 2
    assert any("missing.json" in n for n in surf.notes)
    assert RepoConfig().openapi == []
