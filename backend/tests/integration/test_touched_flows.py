"""Flows a PR touches are rebuilt on demand, even when the change sits deep behind a service layer or a Celery task.

A tiny Django repo: the project urls mount a *named* pattern list (`include((billing_urlpatterns, "billing"))`); a view
builds a service object (`svc = InvoiceService(); svc.sorted_invoices()`) whose helper the PR changes; a Celery task
imports its HTTP client inside the function body and calls `TrackingClient().push()`, whose private method the PR
changes; another view enqueues that task from `transaction.on_commit(lambda: push_shipment.delay(...))`."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import load_docs, local_request
from pr_explorer.bootstrap import build_container
from pr_explorer.domain.services import overlay_builder

APPS = ("billing", "tracking", "orders")
BASE = {
    "manage.py": "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'shop.settings')\n",
    "shop/__init__.py": "",
    "shop/settings.py": "INSTALLED_APPS = ['billing', 'tracking', 'orders']\n",
    "shop/urls.py": (
        "from django.urls import include, path\n"
        "from billing.urls import billing_urlpatterns\n"
        "from orders import views as order_views\n\n"
        "urlpatterns = [\n"
        "    path('api/', include((billing_urlpatterns, 'billing'), namespace='billing')),\n"
        "    path('orders/<int:pk>/ship/', order_views.ship),\n"
        "]\n"
    ),
    **{f"{a}/__init__.py": "" for a in APPS},
    **{f"{a}/apps.py": f"from django.apps import AppConfig\n\n\nclass {a.title()}Config(AppConfig):\n    name = '{a}'\n"
       for a in APPS},
    "billing/urls.py": "from django.urls import path\nfrom . import views\n\nbilling_urlpatterns = [\n"
                       "    path('invoices/', views.list_invoices),\n]\n",
    "billing/views.py": (
        "from django.http import JsonResponse\n"
        "from .services import InvoiceService\n\n\n"
        "def list_invoices(request):\n"
        "    svc = InvoiceService(request.user)\n"
        "    return JsonResponse({'rows': svc.sorted_invoices()})\n"
    ),
    "billing/services.py": (
        "from .helpers import sort_invoices\n\n\n"
        "class InvoiceService:\n"
        "    def __init__(self, user):\n"
        "        self.user = user\n\n"
        "    def sorted_invoices(self):\n"
        "        return sort_invoices(self.user.invoices)\n"
    ),
    "billing/helpers.py": "def sort_invoices(rows):\n    return sorted(rows, key=lambda r: r['no'])\n",
    "tracking/client.py": (
        "import requests\n\n\n"
        "class TrackingClient:\n"
        "    def push(self, shipment_id):\n"
        "        return self._send({'id': shipment_id})\n\n"
        "    def _send(self, body):\n"
        "        return requests.post('https://tracking.invalid/api', json=body)\n"
    ),
    "tracking/tasks.py": (
        "from celery import shared_task\n\n\n"
        "@shared_task\n"
        "def push_shipment(shipment_id):\n"
        "    from tracking.client import TrackingClient\n\n"
        "    TrackingClient().push(shipment_id)\n"
    ),
    "orders/views.py": (
        "from django.db import transaction\n"
        "from django.http import JsonResponse\n"
        "from tracking.tasks import push_shipment\n\n\n"
        "def ship(request, pk):\n"
        "    transaction.on_commit(lambda: push_shipment.delay(pk))\n"
        "    return JsonResponse({'ok': True})\n"
    ),
}
HEAD = {
    "billing/helpers.py": "def sort_invoices(rows):\n    return sorted(rows, key=lambda r: (r['date'], r['no']), reverse=True)\n",
    "tracking/client.py": BASE["tracking/client.py"].replace(
        "        return requests.post('https://tracking.invalid/api', json=body)\n",
        "        res = requests.post('https://tracking.invalid/api', json=body, timeout=10)\n"
        "        res.raise_for_status()\n        return res\n"),
}


def _repo(root: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                       capture_output=True)

    def write(files: dict[str, str]) -> None:
        for p, text in files.items():
            (root / p).parent.mkdir(parents=True, exist_ok=True)
            (root / p).write_text(text)

    root.mkdir(parents=True)
    git("init", "-q", "-b", "main")
    write(BASE)
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "fix/deep")
    write(HEAD)
    git("add", "-A")
    git("commit", "-q", "-m", "Sort invoices by date; check tracking responses")
    git("checkout", "-q", "main")
    return root


def _analyze(tmp_path: Path):
    c = build_container(home=tmp_path / "home")
    paths, rec = c.analyze.analyze_sync(local_request(_repo(tmp_path / "repo"), head="fix/deep"))
    _, overlay = load_docs(paths)
    flows_msg = next(s["message"] for s in rec["stages"] if s["name"] == "flows")
    return overlay, flows_msg


def test_deep_changes_behind_a_service_and_a_celery_task_produce_flow_changes(tmp_path):
    overlay, msg = _analyze(tmp_path)
    fcs = {fc["flow_id"]: fc for fc in overlay["flow_changes"]}

    def changed_steps(fid: str) -> list[str]:
        return [s["technical_label"] for s in fcs[fid]["steps"] if s["step_change_status"] != "unchanged"]

    route = next(f for f in fcs if f.startswith("flow:route:billing/urls.py::invoices/"))  # mounted from a named list
    assert any("sort_invoices" in label for label in changed_steps(route)), fcs[route]
    task = "flow:py:tracking/tasks.py::push_shipment"
    assert task in fcs and any("TrackingClient" in label for label in changed_steps(task)), fcs.get(task)
    ship = next(f for f in fcs if f.startswith("flow:route:shop/urls.py::orders/"))  # enqueues the task (on_commit lambda)
    assert any("push_shipment" in label for label in changed_steps(ship)), fcs[ship]
    assert "flow change(s)" in msg and "stopped" not in msg


def test_touched_flow_caps_are_noted(tmp_path, monkeypatch):
    monkeypatch.setattr(overlay_builder, "TOUCH_MAX_DEPTH", 1)
    monkeypatch.setattr(overlay_builder, "MAX_RELATED_FLOWS", 1)
    overlay, msg = _analyze(tmp_path)
    assert len(overlay["flow_changes"]) <= 1
    assert "backward walk from the changed code stopped at 1 calls" in msg
    assert "entry points reach the changed code; the 1 nearest were compared" in msg
