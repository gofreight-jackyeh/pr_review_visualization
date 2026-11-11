"""System map construction (spec v2 Layer 4/5): grouping, entry points, business steps, step diff, flow budget."""

from pathlib import Path

from pr_explorer.domain.model.document import Step
from pr_explorer.domain.model.graph import CodeRef, EntryFact, Evidence, Node, Producer, SideGraph
from pr_explorer.domain.services.flow_steps import build_timeline, diff_steps
from pr_explorer.domain.services.system_map_builder import build_flows
from pr_explorer.domain.services.system_structure import LITE_REASON, build_structure, group_capabilities
from pr_explorer.infrastructure.analyzers.generic.languages import detect_language
from pr_explorer.infrastructure.analyzers.python.graph_builder import PythonAnalyzer

SRC = {
    "proj/__init__.py": "",
    "proj/urls.py": (
        "from django.urls import include, path\n\n"
        "urlpatterns = [\n    path('shop/', include('shop.urls')),\n    path('pay/', include('billing.urls')),\n]\n"
    ),
    "proj/celery.py": (
        "from celery import Celery\n\napp = Celery('proj')\n\n\n"
        "@app.task\ndef nightly():\n    return 1\n"
    ),
    "shop/__init__.py": "",
    "shop/apps.py": "from django.apps import AppConfig\n\n\nclass ShopConfig(AppConfig):\n    name = 'shop'\n",
    "shop/models.py": (
        "from django.db import models\n\n\n"
        "class Order(models.Model):\n    total = models.IntegerField()\n\n\n"
        "class Line(models.Model):\n    qty = models.IntegerField()\n"
    ),
    "shop/helpers.py": "def norm(x):\n    return x\n",
    "shop/views.py": (
        "import requests\n"
        "from billing import services as billing\n"
        "from . import helpers\n"
        "from .models import Line, Order\n"
        "from .tasks import notify\n\n\n"
        "def checkout(request):\n"
        "    total = helpers.norm(request.POST['total'])\n"
        "    order = Order.objects.create(total=total)\n"
        "    Line.objects.create(qty=1)\n"
        "    Line.objects.create(qty=2)\n"
        "    if total > 100:\n"
        "        billing.charge(order)\n"
        "    notify.delay(order.pk)\n"
        "    requests.post('https://hooks.example.invalid', json={})\n"
        "    fn = getattr(helpers, request.GET['how'])\n"
        "    fn(order)\n"
        "    if not order.pk:\n"
        "        raise ValueError('not saved')\n"
        "    return order\n"
    ),
    "shop/urls.py": "from django.urls import path\n\nfrom . import views\n\nurlpatterns = [path('checkout/', views.checkout)]\n",
    "shop/tasks.py": "from celery import shared_task\n\n\n@shared_task\ndef notify(pk):\n    return pk\n",
    "shop/management/__init__.py": "",
    "shop/management/commands/__init__.py": "",
    "shop/management/commands/close_day.py": (
        "from django.core.management.base import BaseCommand\n\nfrom shop.models import Order\n\n\n"
        "class Command(BaseCommand):\n    def handle(self, *args, **opts):\n        Order.objects.filter(total=0).delete()\n"
    ),
    "billing/__init__.py": "",
    "billing/apps.py": "from django.apps import AppConfig\n\n\nclass BillingConfig(AppConfig):\n    name = 'billing'\n",
    "billing/services.py": (
        "from shop.models import Order\n\n\n"
        "def charge(order):\n    paid = Order.objects.get(pk=order.pk)\n    paid.save()\n"
    ),
    "billing/api.py": (
        "from rest_framework import viewsets\nfrom rest_framework.views import APIView\n\n\n"
        "class InvoiceViewSet(viewsets.ViewSet):\n    def list(self, request):\n        return []\n\n\n"
        "class HookView(APIView):\n    def post(self, request):\n        return None\n"
    ),
    "billing/urls.py": (
        "from django.urls import include, path\nfrom rest_framework.routers import DefaultRouter\n\nfrom . import api\n\n"
        "router = DefaultRouter()\nrouter.register(r'invoices', api.InvoiceViewSet)\n\n"
        "urlpatterns = [path('hook/', api.HookView.as_view()), path('api/', include(router.urls))]\n"
    ),
}


def analyze(tmp_path: Path, src=SRC, side="base") -> SideGraph:
    for p, s in src.items():
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text(s)
    return PythonAnalyzer().analyze(tmp_path, set(src), side)


def test_capability_grouping():
    files = {
        "apps/orders/apps.py", "apps/orders/models.py", "apps/orders/sub/apps.py", "apps/orders/sub/x.py",
        "apps/orders/static/cart.js", "web/src/billing/page.tsx", "web/src/billing/api.ts", "web/src/index.ts",
        "lib/util.py", "lib/__init__.py", "manage.py", "README.md", "docs/a.md", ".github/workflows/ci.yml",
        "node_modules/pkg/index.js", "apps/orders/migrations/0001.py",
    }
    idx = group_capabilities(files, detect_language)
    assert idx.of("apps/orders/models.py") == "apps/orders"  # Django app
    assert idx.of("apps/orders/sub/x.py") == "apps/orders/sub"  # deepest app wins
    assert idx.of("apps/orders/static/cart.js") == "apps/orders"  # JS inside an app belongs to the app
    assert idx.of("web/src/billing/page.tsx") == idx.of("web/src/billing/api.ts") == "web/src/billing"  # TS feature dir
    assert idx.of("lib/util.py") == "lib"  # top-level package
    assert idx.of("manage.py") is None and idx.of(".github/workflows/ci.yml") is None
    assert idx.kinds == {"apps/orders": "django_app", "apps/orders/sub": "django_app", "web/src/billing": "ts_feature",
                         "web/src": "ts_feature", "lib": "package"}
    assert "docs" not in idx.top_dirs and "node_modules" not in idx.top_dirs


def test_entry_point_detection_route_drf_task_command(tmp_path):
    sg = analyze(tmp_path)
    eps = {(e.kind, e.label) for e in sg.entry_points}
    assert ("route", "/shop/checkout/") in eps  # include() prefix resolved
    assert ("api", "/pay/hook/") in eps  # APIView
    assert ("api", "/pay/api/invoices/") in eps  # DRF router registration
    assert ("task", "task shop.tasks.notify") in eps  # @shared_task
    assert ("task", "task proj.celery.nightly") in eps  # @app.task
    assert ("command", "manage.py close_day") in eps
    st = build_structure(sg, detect_language)
    by_label = {e.technical_name: e for e in st.entry_points.values()}
    assert by_label["/pay/api/invoices/"].capability_id == "cap:billing"
    assert by_label["manage.py close_day"].handler_component_id == "node:py:shop/management/commands/close_day.py::Command.handle"
    assert set(st.capabilities) == {"cap:shop", "cap:billing", "cap:proj"}
    assert {e.technical_name for e in st.entities.values()} == {"Order", "Line"}


def test_business_steps_collapse_and_kinds(tmp_path):
    sg = analyze(tmp_path)
    st = build_structure(sg, detect_language)
    ep = next(e for e in st.entry_points.values() if e.technical_name == "/shop/checkout/")
    tl = build_timeline(sg, st, ep.id, "flow:x")
    kinds = [(s.kind, s.capability_id, s.evidence_status) for s in tl.steps]
    assert tl.mode == "animation"
    assert kinds[0] == ("enter", "cap:shop", "confirmed")
    assert ("create", "cap:shop", "confirmed") in kinds
    lines = [s for s in tl.steps if s.kind == "create" and "Line" in s.technical_label]
    assert len(lines) == 1 and "(×2)" in lines[0].technical_label  # consecutive identical steps collapse
    # a confirmed same-capability call (helpers.norm) folds into the current step instead of creating one
    assert not any(s.kind == "call" and s.evidence_status == "confirmed" and "norm" in s.technical_label for s in tl.steps)
    assert "node:py:shop/helpers.py::norm" in tl.steps[0].component_ids
    cross = next(s for s in tl.steps if s.capability_id == "cap:billing" and s.kind == "call")
    assert cross.evidence_status == "possible" and cross.branch.condition == "total > 100"  # branch → possible
    write = next(s for s in tl.steps if s.kind == "write")
    assert write.capability_id == "cap:billing" and write.entity_id.endswith("Order")
    assert any(s.kind == "emit" and "notify" in s.technical_label for s in tl.steps)
    assert any(s.kind == "external" for s in tl.steps)
    assert any(s.kind == "call" and s.evidence_status == "unknown" for s in tl.steps)  # getattr dispatch
    assert any(s.kind == "raise" and s.evidence_status == "possible" for s in tl.steps)
    assert [s.order for s in tl.steps] == list(range(len(tl.steps)))


def test_without_statement_order_the_flow_is_static():
    ev = Evidence("ev:1", "ts_compiler", "x", [CodeRef("base", "web/src/a/x.ts", 1)], Producer("t", "1"))
    sg = SideGraph(side="base", files={"web/src/a/x.ts", "web/src/b/y.ts"}, evidence={"ev:1": ev})
    for nid, path in (("node:ts:x", "web/src/a/x.ts"), ("node:ts:y", "web/src/b/y.ts")):
        sg.add_node(Node(nid, "function", "symbol", nid, None, [CodeRef("base", path, 1)], ["ev:1"], language="typescript"))
    from pr_explorer.domain.model.graph import Edge
    sg.add_edge(Edge("e1", "node:ts:x", "node:ts:y", "calls", "confirmed", ["ev:1"]))
    sg.entry_points.append(EntryFact("node:ts:x", "ui_route", "/a", "node:ts:x", CodeRef("base", "web/src/a/x.ts", 1), "ev:1"))
    st = build_structure(sg, detect_language)
    ep = next(iter(st.entry_points))
    tl = build_timeline(sg, st, ep, "flow:t")
    assert tl.mode == "static_flow" and "statement order" in tl.mode_reason
    assert [s.kind for s in tl.steps] == ["enter", "call"]


def _step(key, label="l", status="confirmed", comps=()):
    kind, cap, ent, anchor = key
    return Step(f"step:{key}", 0, kind, cap, list(comps), label, status, ["ev"], ent,
                meta={"key": key, "loose": (kind, cap, ent)})


def test_step_change_status_diff():
    enter, read, call_b, call_c = ("enter", "c:a", None, "enter"), ("read", "c:a", "e:o", "read"), \
        ("call", "c:b", None, "n:b"), ("call", "c:c", None, "n:c")
    write_old, write_new = ("write", "c:a", "e:o", "write"), ("create", "c:a", "e:o", "create")
    base = [_step(enter, comps=["n:h"]), _step(read), _step(call_b), _step(write_old)]
    head = [_step(enter, comps=["n:h"]), _step(read, status="possible"), _step(call_c), _step(write_new)]
    out = diff_steps(base, head, changed={"n:h"})
    status = [(s.kind, s.step_change_status) for s in out]
    assert status == [("enter", "changed"),  # primary component changed in the PR
                      ("read", "changed"),  # same step, evidence status differs
                      # in an unmatched gap: base steps are removed, then head steps are added
                      ("call", "removed"), ("write", "removed"), ("call", "added"), ("create", "added")]
    assert [s.order for s in out] == list(range(len(out))) and len({s.id for s in out}) == len(out)
    before = [s for s in out if s.step_change_status != "added"]
    after = [s for s in out if s.step_change_status != "removed"]
    assert [s.kind for s in before] == ["enter", "read", "call", "write"]
    assert [s.kind for s in after] == ["enter", "read", "call", "create"]
    same = diff_steps(base, base, changed=set())
    assert all(s.step_change_status == "unchanged" for s in same)
    # unmatched steps of the same (kind, capability, entity) pair up as `changed`, not removed + added
    loose = diff_steps([_step(("read", "c:a", "e:o", "x"))], [_step(("read", "c:a", "e:o", "y"))], set())
    assert [s.step_change_status for s in loose] == ["changed"]


def test_flow_budget(tmp_path):
    sg = analyze(tmp_path)
    st = build_structure(sg, detect_language)
    total = len(st.entry_points)
    assert total >= 6
    flows, built = build_flows(sg, st, budget=2)
    assert len(flows) == total and len(built) == 2
    full = [f for f in flows if f.id in built]
    lite = [f for f in flows if f.id not in built]
    assert all(f.timeline.steps for f in full)
    assert all(not f.timeline.steps and f.timeline.mode == "static_flow" and f.timeline.mode_reason == LITE_REASON for f in lite)
    # the pick is PR-independent and spreads over capabilities (richest flow of each capability first)
    assert len({st.entry_points[f.entry_point_id].capability_id for f in full}) == 2
    assert build_flows(sg, st, budget=2)[1] == built
