"""Schema 2.2.0: one-level capability split with merge, interface rings (outer / middle) and interface-change checks."""

import json
import random

from test_system_map import analyze

from pr_explorer.domain.model import ids
from pr_explorer.domain.model.convention import RepoConfig, RepoSurface
from pr_explorer.domain.model.graph import CodeRef, EntryFact, Node, SideGraph
from pr_explorer.domain.services.architecture import check_order
from pr_explorer.domain.services.graph_diff import diff_graphs
from pr_explorer.domain.services.rings import detect_interfaces, diff_interfaces, field_changes, glob_match, sig_change
from pr_explorer.domain.services.system_structure import (
    SPLIT_MAX_CHILDREN, SPLIT_MIN_CHILD_ENTRY_POINTS, build_structure,
)
from pr_explorer.infrastructure.analyzers.generic.languages import detect_language
from pr_explorer.infrastructure.knowledge.repo_surface import parse_repo_config, scan_surface
from pr_explorer.infrastructure.persistence.schema_validator import interface_change_convention


# ---------------------------------------------------------------- split: one level, merge, ≤ 12 children

def synth(subs: dict[str, int], root: str = "app/big") -> SideGraph:
    """A Django app `root` whose sub-directories hold n generated routes each (keys may nest: 'admin/reports')."""
    sg = SideGraph("base", files={f"{root}/apps.py"})
    for sub, n in subs.items():
        path = f"{root}/{sub}/views.py"
        sg.files.add(path)
        sg.add_node(Node(ids.module_id("py", path), "module", "module", path, None, [CodeRef("base", path, 1)], ["ev:m"]))
        for i in range(n):
            sg.entry_points.append(EntryFact(f"node:route:{path}::r{i}", "route", f"/{sub}/{i}/", None,
                                             CodeRef("base", path, i + 1), "ev:r"))
    return sg


def test_split_is_one_level_merges_small_sub_directories_and_caps_children():
    subs = {f"s{i:02d}": 30 for i in range(20)} | {f"tiny{i}": SPLIT_MIN_CHILD_ENTRY_POINTS - 1 for i in range(5)}
    subs |= {"tests": 0, "admin/reports": 100}  # engineering stays with the parent; nested dirs are not split again
    st = build_structure(synth(subs), detect_language)
    children = sorted(c.id for c in st.capabilities.values() if c.parent_capability_id == "cap:app/big")
    assert len(children) == SPLIT_MAX_CHILDREN and "cap:app/big/*" in children
    assert "cap:app/big/admin" in children and not any("reports" in c for c in st.capabilities)  # 100 eps: biggest child
    other = st.capabilities["cap:app/big/*"]
    assert other.technical_name == "Other" and len(other.entry_point_ids) == 10 * 30 + 5 * (SPLIT_MIN_CHILD_ENTRY_POINTS - 1)
    assert st.caps.of("app/big/tests/test_x.py") == "app/big" and st.caps.of("app/big/tiny0/views.py") == "app/big/*"
    assert "(+5 more) of app/big merged" in st.evidence["ev:cap:base:app/big/*"].description
    # stable, content-derived IDs: insertion order does not matter, and the PR head reuses the map's split verbatim
    shuffled = dict(random.Random(7).sample(sorted(subs.items()), len(subs)))
    assert sorted(build_structure(synth(shuffled), detect_language).capabilities) == sorted(st.capabilities)
    assert sorted(build_structure(synth(subs), detect_language, splits=dict(st.caps.subs)).capabilities) == sorted(st.capabilities)
    zh = build_structure(synth(subs), detect_language, lang="zh-Hant")
    assert zh.capabilities["cap:app/big/*"].technical_name == "其他"


def test_split_needs_a_qualifying_child_and_no_other_when_everything_fits():
    none = build_structure(synth({f"s{i}": 2 for i in range(30)}), detect_language)  # 60 eps, no sub-dir reaches the minimum
    assert set(none.capabilities) == {"cap:app/big"}
    exact = build_structure(synth({"a": 40, "b": 20}), detect_language)  # every sub-directory qualifies: no "other"
    assert set(exact.capabilities) == {"cap:app/big", "cap:app/big/a", "cap:app/big/b"}


# ---------------------------------------------------------------- .pr-explorer.yml + glob

def test_repo_config_parsing():
    cfg = parse_repo_config(
        "# outer interfaces\nouter:\n  paths:\n    - orders/notifications.py   # e-mails\n    - 'edi/**'\n"
        "  routes: [\"/partner/**\", /hooks/]\n  packages:\n    - shop-sdk\nexclude_outer:\n  routes:\n    - /api/internal/**\n"
        "colour: blue\nouter_extra:\n  - x\n")
    assert cfg.outer == {"paths": [("orders/notifications.py", 4), ("edi/**", 5)], "routes": [("/partner/**", 6), ("/hooks/", 6)],
                         "packages": [("shop-sdk", 8)]}
    assert cfg.exclude_outer == {"routes": [("/api/internal/**", 11)]}
    assert len(cfg.notes) == 3  # unknown keys and the stray list item are reported, not guessed
    assert parse_repo_config("outer:\n  - a/**\n").outer == {"paths": [("a/**", 2)]}  # bare list = paths
    assert glob_match("**/edi/**", "x/edi/out.py") and glob_match("edi/**", "edi/a/b.py") and not glob_match("edi/*", "edi/a/b.py")
    assert glob_match("/api/internal/**", "/api/internal/x/") and glob_match("/hooks/", "/hooks")


# ---------------------------------------------------------------- outer + middle detection, interface diff

BASE = {
    "api_app/__init__.py": "",
    "api_app/apps.py": "from django.apps import AppConfig\n",
    "api_app/serializers.py": (
        "from rest_framework import serializers\n\n\n"
        "class ItemSerializer(serializers.Serializer):\n"
        "    id = serializers.IntegerField(read_only=True)\n"
        "    name = serializers.CharField()\n"
        "    note = serializers.CharField(required=False)\n\n\n"
        "class AllSerializer(serializers.ModelSerializer):\n"
        "    class Meta:\n        model = object\n        fields = '__all__'\n"
    ),
    "api_app/views.py": (
        "from rest_framework.decorators import api_view\nfrom rest_framework.response import Response\n"
        "from rest_framework.views import APIView\n\nfrom .serializers import ItemSerializer\n\n\n"
        "class ItemAPIView(APIView):\n    serializer_class = ItemSerializer\n\n"
        "    def get(self, request):\n        return Response(ItemSerializer([], many=True).data)\n\n"
        "    def post(self, request):\n        return Response(ItemSerializer(data=request.data).data)\n\n\n"
        "class LegacyView(APIView):\n    def get(self, request):\n        return Response({'a': 1})\n\n\n"
        "@api_view(['GET', 'POST'])\ndef ping(request):\n    return Response({})\n\n\n"
        "def home(request):\n    return 'home'\n"
    ),
    "api_app/urls.py": (
        "from django.urls import path\n\nfrom . import views\n\nurlpatterns = [\n"
        "    path('api/items/', views.ItemAPIView.as_view()),\n    path('api/legacy/', views.LegacyView.as_view()),\n"
        "    path('api/ping/', views.ping),\n    path('home/', views.home),\n]\n"
    ),
    "api_app/tasks.py": "from celery import shared_task\n\n\n@shared_task\ndef sync(item_id, force=False):\n    return item_id\n",
    "api_app/services.py": "def make(item, qty):\n    return item\n",
    "other_app/__init__.py": "",
    "other_app/apps.py": "from django.apps import AppConfig\n",
    "other_app/logic.py": "from api_app.services import make\n\n\ndef go():\n    return make(1, 2)\n",
    "integrations/__init__.py": "",
    "integrations/edi/__init__.py": "",
    "integrations/edi/out.py": "def build(order):\n    return 'ISA*00'\n",
}
HEAD = dict(BASE)
HEAD["api_app/serializers.py"] = BASE["api_app/serializers.py"].replace(
    "    note = serializers.CharField(required=False)\n", "    note = serializers.CharField(required=False)\n    qty = serializers.IntegerField()\n")
HEAD["api_app/views.py"] = (BASE["api_app/views.py"]
                            .replace("    def post(self, request):\n        return Response(ItemSerializer(data=request.data).data)\n\n", "")
                            .replace("return Response({'a': 1})", "return Response({'a': 1, 'b': 2})")
                            + "\n\nclass NewView(APIView):\n    def get(self, request):\n        return Response({})\n")
HEAD["api_app/urls.py"] = BASE["api_app/urls.py"].replace("    path('api/ping/', views.ping),\n",
                                                          "    path('api/new/', views.NewView.as_view()),\n")
HEAD["api_app/tasks.py"] = BASE["api_app/tasks.py"].replace("def sync(item_id, force=False)", "def sync(item_id, mode, force=False)")
HEAD["api_app/services.py"] = "def make(item, qty, unit='pcs'):\n    return item\n"
HEAD["integrations/edi/out.py"] = "def build(order):\n    return 'ISA*01'\n"


def _sides(tmp_path, cfg: RepoConfig = RepoConfig()):
    b, h = analyze(tmp_path / "b", BASE), analyze(tmp_path / "h", HEAD, side="head")
    bst = build_structure(b, detect_language)
    hst = build_structure(h, detect_language, splits=dict(bst.caps.subs))
    return b, h, bst, hst, detect_interfaces(b, bst, RepoSurface(), cfg), detect_interfaces(h, hst, RepoSurface(), cfg)


def test_outer_and_middle_detection(tmp_path):
    *_, bi, _ = _sides(tmp_path)
    by = {i.technical_name: i for i in bi.interfaces.values()}
    items = by["/api/items/"]
    assert (items.ring, items.kind, items.detected_by) == ("outer", "http_api", "static") and items.entry_point_id
    assert items.meta["contract"]["methods"] == ["GET", "POST"]
    [fields] = items.meta["contract"]["serializers"].values()  # request / response shape from the serializer
    assert fields == {"id": {"read_only": True, "write_only": False, "required": False},
                      "name": {"read_only": False, "write_only": False, "required": True},
                      "note": {"read_only": False, "write_only": False, "required": False}}
    assert by["/api/ping/"].meta["contract"]["methods"] == ["GET", "POST"]  # @api_view(["GET", "POST"])
    assert by["/api/legacy/"].meta["contract"]["serializers"] is None  # no serializer: shape not resolvable
    assert "/home/" not in by  # a plain Django page route is not an API
    assert by["integrations/edi/out.py"].kind == "outbound_format" and by["integrations/edi/out.py"].ring == "outer"
    assert (by["api_app.services"].ring, by["api_app.services"].kind) == ("middle", "module_api")
    assert by["api_app.services"].meta["contract"]["symbols"]["make"]["positional"] == ["item", "qty"]
    assert (by["api_app.tasks.sync"].ring, by["api_app.tasks.sync"].kind) == ("middle", "task")


def test_config_overrides_detection(tmp_path):
    cfg = parse_repo_config("outer:\n  routes: ['/home/']\nexclude_outer:\n  paths: ['integrations/**']\n")
    *_, bi, _ = _sides(tmp_path, cfg)
    by = {i.technical_name: i for i in bi.interfaces.values()}
    assert by["/home/"].detected_by == "config" and by["/home/"].kind == "http_api"
    assert any(e.refs[0].path == ".pr-explorer.yml" for e in bi.evidence.values())
    assert "integrations/edi/out.py" not in by


def test_interface_diff_rules_and_order(tmp_path):
    b, h, bst, hst, bi, hi = _sides(tmp_path)
    g = diff_graphs(b, h)
    added: list[str] = []
    checks = diff_interfaces(
        bi.interfaces, hi.interfaces, head_sg=h, known=set(bi.interfaces), changed_nodes=g.changed_node_ids(),
        changed_paths={p for p in HEAD if HEAD[p] != BASE.get(p)}, iface_ref=lambda i: added.append(i.id) or i.id,
        cap_ref=lambda c: c, diff_ev={}, lang="en")
    got = {c.title.split(": ", 1)[-1]: (c.ring, c.interface_change.change, c.interface_change.compatibility, c.verdict, c.status)
           for c in checks}
    assert got == {
        "/api/items/": ("outer", "modified", "breaking", "violates", "verified"),  # POST gone + new required field qty
        "/api/legacy/": ("outer", "modified", "unknown", "unclear", "needs_confirmation"),  # handler changed, shape unresolvable
        "/api/ping/": ("outer", "removed", "breaking", "violates", "verified"),
        "/api/new/": ("outer", "added", "compatible", "follows", "verified"),
        "integrations/edi/out.py": ("outer", "modified", "unknown", "unclear", "needs_confirmation"),
        "api_app.services": ("middle", "modified", "compatible", "follows", "verified"),  # new optional parameter
        "api_app.tasks.sync": ("middle", "modified", "breaking", "violates", "verified"),  # new required task argument
    }
    items = next(c for c in checks if c.title.endswith("/api/items/"))
    assert "HTTP method POST no longer accepted" in items.interface_change.detail and "new required field qty" in items.interface_change.detail
    assert items.origin == "observed" and items.check == "interface_change" and "versioning" in items.description
    assert added == ["iface:ep:route:api_app/urls.py::api/new/"]
    assert checks == sorted(checks, key=check_order) and [c.ring for c in checks][:5] == ["outer"] * 5
    for c in checks:  # the convention the validator enforces
        assert interface_change_convention({"change": c.interface_change.change, "compatibility": c.interface_change.compatibility}) == []


def test_field_and_signature_rules():
    ro = {"read_only": True, "write_only": False, "required": False}
    req = {"read_only": False, "write_only": False, "required": True}
    opt = {"read_only": False, "write_only": False, "required": False}
    model = {"read_only": False, "write_only": False, "required": None}
    rules = lambda b, h: [c for c, _ in field_changes(b, h, "en")]  # noqa: E731
    assert rules({"a": ro}, {"a": ro, "b": ro}) == ["compatible"]  # new response field
    assert rules({"a": ro}, {"a": ro, "b": opt}) == ["compatible"]  # new optional request field
    assert rules({"a": ro, "b": ro}, {"a": ro}) == ["breaking"]  # removed
    renamed = field_changes({"total": ro}, {"grand_total": ro}, "en")
    assert renamed[0][0] == "breaking" and "renamed to grand_total" in renamed[0][1]
    assert rules({"a": ro}, {"a": ro, "b": req}) == ["breaking"]  # new required field
    assert rules({"a": ro}, {"a": ro, "b": model}) == ["unknown"]  # required-ness decided by the model
    assert rules({"a": opt}, {"a": req}) == ["breaking"] and rules({"a": req}, {"a": ro}) == ["unknown"]
    sig = lambda pos, req_n, kw=(): {"positional": list(pos), "required_positional": req_n, "kwonly": list(kw),  # noqa: E731
                                     "required_kwonly": [], "vararg": False, "kwarg": False}
    assert sig_change(sig("ab", 2), sig("ab", 2), "en") is None
    assert sig_change(sig("ab", 2), sig("abc", 3), "en")[0] == "breaking"  # new required parameter
    assert sig_change(sig("ab", 2), sig("a", 1), "en")[0] == "breaking"  # parameter removed
    assert sig_change(sig("ab", 2), sig("abc", 2), "en")[0] == "compatible"  # new optional parameter
    assert sig_change(sig("ab", 2), sig("ba", 2), "en")[0] == "unknown"  # reordered
    assert sig_change(None, sig("a", 1), "en")[0] == "unknown"


def test_validator_convention_for_interface_changes():
    ok = [("added", "compatible"), ("removed", "breaking"), ("removed", "unknown"), ("modified", "compatible"),
          ("modified", "breaking"), ("modified", "unknown")]
    assert all(interface_change_convention({"change": a, "compatibility": b}) == [] for a, b in ok)
    assert interface_change_convention({"change": "added", "compatibility": "breaking"})
    assert interface_change_convention({"change": "added", "compatibility": "unknown"})
    assert interface_change_convention({"change": "removed", "compatibility": "compatible"})


# ---------------------------------------------------------------- web routes + published package exports

def test_web_routes_and_sdk_exports(tmp_path):
    files = {
        "web/src/pages/orders/[id].tsx": "export default function Order() { return null }\n",
        "web/src/pages/_app.tsx": "export default function App() { return null }\n",
        "web/src/pages/api/hello.ts": "export default function h() {}\n",
        "web/src/router.ts": "export const routes = [\n  { path: '/settings', component: S },\n  { path: 'reports' },\n]\n",
        "web/package.json": json.dumps({"name": "@shop/web", "private": True}),
        "sdk/package.json": json.dumps({"name": "shop-sdk", "main": "dist/index.js"}),
        "sdk/src/index.ts": "export function createClient(url: string) {}\nconst a = 1\nexport { a as version }\n",
        "pysdk/pyproject.toml": "[project]\nname = \"shop-client\"\n",
        "pysdk/shop_client/__init__.py": "__all__ = ['Client']\n\n\nclass Client:\n    pass\n\n\ndef _private():\n    pass\n",
        "pysdk/internal/pyproject.toml": "[project]\nname = \"hidden\"\nclassifiers = [\"Private :: Do Not Upload\"]\n",
    }
    for p, s in files.items():
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text(s)
    surf = scan_surface(tmp_path, set(files))
    assert [(w.route, w.how) for w in surf.web_routes] == [("/orders/[id]", "file_convention"), ("/reports", "router_config"),
                                                          ("/settings", "router_config")]
    pk = {p.name: p for p in surf.packages}
    assert not pk["@shop/web"].published and pk["shop-sdk"].published and not pk["hidden"].published
    assert [x.name for x in pk["shop-sdk"].exports] == ["createClient", "version"]
    assert [x.name for x in pk["shop-client"].exports] == ["Client"]
    sg = SideGraph("base", files=set(files))
    st = build_structure(sg, detect_language)
    got = detect_interfaces(sg, st, surf, RepoConfig())
    kinds = {i.id: (i.ring, i.kind) for i in got.interfaces.values()}
    assert kinds["iface:web:/orders/[id]"] == ("outer", "web_route")
    assert kinds["iface:sdk:shop-sdk:createClient"] == ("outer", "sdk_export")
    assert kinds["iface:sdk:shop-client:Client"] == ("outer", "sdk_export")
    assert not any(":@shop/web:" in i for i in kinds)  # private package: not published
    forced = detect_interfaces(sg, st, surf, parse_repo_config("outer:\n  packages: ['@shop/*']\n"))
    assert not any("@shop/web" in i for i in forced.interfaces)  # declared, but it has no entry module exports


def test_integrity_resolves_interface_references(shop_docs):
    import copy

    from pr_explorer.infrastructure.persistence.schema_validator import overlay_integrity, system_map_integrity

    smap, ov, _, _ = shop_docs
    assert system_map_integrity(smap) == [] and overlay_integrity(ov, smap) == []
    m = copy.deepcopy(smap)
    m["interfaces"][0]["entry_point_id"] = "ep:nope"
    m["interfaces"][1]["capability_id"] = "cap:nope"
    errs = system_map_integrity(m)
    assert any("unknown entry_point ep:nope" in e for e in errs) and any("unknown capabilit" in e and "cap:nope" in e for e in errs)
    o = copy.deepcopy(ov)
    ic = [c for c in o["architecture_checks"] if c["check"] == "interface_change"]
    ic[0]["interface_change"]["interface_id"] = "iface:nope"
    added = next(c for c in ic if c["interface_change"]["change"] == "added")
    added["interface_change"]["compatibility"] = "breaking"
    o["head_additions"]["interfaces"][0]["entry_point_id"] = "ep:nope"
    errs = overlay_integrity(o, smap)
    assert any("unknown interface iface:nope" in e for e in errs)
    assert any("'added' must be 'compatible'" in e for e in errs)
    assert any("head_additions interface" in e and "ep:nope" in e for e in errs)


# ---------------------------------------------------------------- file exports (outbound file formats behind an API)

EXPORT = {
    "exports_app/__init__.py": "",
    "exports_app/apps.py": "from django.apps import AppConfig\n",
    "exports_app/keys.py": "CUSTOMER_KEY = 'customer'\n",
    "exports_app/sheet.py": "from openpyxl import Workbook\n\n\ndef write_xlsx(rows, columns):\n    return Workbook()\n",
    "exports_app/columns.py": (
        "from exports_app.keys import CUSTOMER_KEY\n\nNOTE_KEY = 'note'\n\n\n"
        "class OrderSheetField:\n"
        "    def column_set(self):\n"
        "        return {'order_no': 1, 'total': 2, CUSTOMER_KEY: 3, NOTE_KEY: 4}\n\n"
        "    def width(self, key):\n        return {'order_no': 16}.get(key, 10)\n"
    ),
    "exports_app/views.py": (
        "from rest_framework.views import APIView\n\nfrom exports_app.columns import OrderSheetField\n"
        "from exports_app.sheet import write_xlsx\n\n\n"
        "class OrderExportView(APIView):\n    def get(self, request):\n"
        "        return write_xlsx([], OrderSheetField().column_set())\n"
    ),
    "exports_app/urls.py": "from django.urls import path\n\nfrom . import views\n\nurlpatterns = [\n    path('api/orders/export/', views.OrderExportView.as_view()),\n]\n",
}


def _export_check(tmp_path, head_files: dict[str, str], name: str):
    head = {**EXPORT, **head_files}
    b, h = analyze(tmp_path / f"b{name}", EXPORT), analyze(tmp_path / f"h{name}", head, side="head")
    bst = build_structure(b, detect_language)
    hst = build_structure(h, detect_language, splits=dict(bst.caps.subs))
    bi, hi = detect_interfaces(b, bst, RepoSurface(), RepoConfig()), detect_interfaces(h, hst, RepoSurface(), RepoConfig())
    checks = diff_interfaces(bi.interfaces, hi.interfaces, head_sg=h, known=set(bi.interfaces),
                             changed_nodes=diff_graphs(b, h).changed_node_ids(),
                             changed_paths={p for p in head if head[p] != EXPORT.get(p)}, iface_ref=lambda i: i.id,
                             cap_ref=lambda c: c, diff_ev={}, lang="en")
    return bi, [c for c in checks if c.title.endswith("/api/orders/export/")]


def test_file_export_columns_are_its_outbound_format(tmp_path):
    bi, _ = _export_check(tmp_path, {}, "0")
    export = next(i for i in bi.interfaces.values() if i.technical_name == "/api/orders/export/")
    cols = export.meta["contract"]["file_columns"]
    assert cols["exports_app/columns.py::OrderSheetField.column_set"]["keys"] == ["order_no", "total", "customer", "note"]  # constants resolved
    assert "returns a file with 4 column key(s)" in export.description.text

    # added columns (as a list of (key, label) tuples, plus keys named at runtime) → compatible, naming the file
    _, [c] = _export_check(tmp_path, {"exports_app/columns.py": EXPORT["exports_app/columns.py"].replace(
        "    def width(self, key):",
        "    def _tax_columns(self, regions):\n        metrics = (('tax', 'Tax', 't'), ('net', 'Net', 'n'))\n"
        "        cols = {}\n        for region in regions:\n            for key, label, _ in metrics:\n"
        "                cols[f'{region}_{key}'] = label\n        return cols\n\n    def width(self, key):")}, "1")
    ic = c.interface_change
    assert (ic.change, ic.compatibility, c.verdict) == ("modified", "compatible", "follows")
    assert "export file adds 2 column key(s): net, tax (defined in exports_app/columns.py)" in ic.detail
    assert "named at runtime in OrderSheetField._tax_columns.cols" in ic.detail

    # a removed / renamed key → breaking
    _, [c] = _export_check(tmp_path, {"exports_app/columns.py": EXPORT["exports_app/columns.py"].replace("'total': 2", "'grand_total': 2")}, "2")
    assert c.interface_change.compatibility == "breaking" and "loses 1 column key(s): total" in c.interface_change.detail

    # the definition file changed, the keys did not → unknown, with the concrete file and key count
    _, [c] = _export_check(tmp_path, {"exports_app/columns.py": EXPORT["exports_app/columns.py"].replace("'total': 2", "'total': 22")}, "3")
    assert c.interface_change.compatibility == "unknown"
    assert "export column definitions in exports_app/columns.py changed but no column key was added or removed (4 keys)" in c.interface_change.detail

    # moving keys between two methods (same union) is no column change; an API that writes no file has no file_columns
    _, checks = _export_check(tmp_path, {"exports_app/columns.py": EXPORT["exports_app/columns.py"].replace(
        "        return {'order_no': 1, 'total': 2, CUSTOMER_KEY: 3, NOTE_KEY: 4}\n",
        "        return {**self._base_columns(), NOTE_KEY: 4}\n\n    def _base_columns(self):\n"
        "        return {'order_no': 1, 'total': 2, CUSTOMER_KEY: 3}\n")}, "4")
    assert [x.interface_change.compatibility for x in checks] in ([], ["unknown"])
    assert all("adds" not in x.interface_change.detail and "loses" not in x.interface_change.detail for x in checks)
    *_, plain, _ = _sides(tmp_path / "plain")
    assert all(i.meta["contract"].get("file_columns") is None for i in plain.interfaces.values() if i.kind == "http_api")
