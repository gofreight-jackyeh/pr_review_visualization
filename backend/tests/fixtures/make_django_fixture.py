"""Build a deterministic local git repo with a small Django shop (`main`) and two PR branches on the same base:
`feature/discount` adds discount codes across url → view → service → model; `feature/restock-alert` makes the
restock command e-mail a low-stock alert (used to prove the system map is reused across PRs on one commit).

Apps (each has apps.py → one business capability): core, catalog, orders, inventory, payments; `shop/` is the
project package. Entry points of every kind: Django routes (incl. include()), DRF router + APIView, Celery
@shared_task + beat schedule, management command. Cross-app calls: orders → catalog / inventory / payments,
payments → external gateway (requests) and a background task (.delay).

Usage:  uv run python tests/fixtures/make_django_fixture.py [--out DIR]
Default output: <project root>/.data/fixtures/shop/

The discount branch deliberately contains review-worthy issues so every analyzer path is exercised:
  * new Order fields + new DiscountCode model, no migration           → static finding
  * create_order() gains a required arg; orders/tasks.py not updated  → static finding
  * notifications.order_cancelled removed but still called           → static finding
  * apply_discount_code view lacks @login_required (siblings have it) → finding + inferred convention
  * apply_discount_code queries the ORM directly (service-layer rule) → inferred + documented convention
  * getattr(discounts, f"apply_{kind}_discount") dispatch             → unknown edges
  * the fixed 10% member price step is removed from checkout          → a `removed` business step
  * fake credentials (scrubbed before they reach the artifact)
Architecture consistency (schema 2.1.0, `.importlinter.ini` on main: a layers and a forbidden contract):
  * catalog/services.py now imports orders.discounts                  → documented `forbidden` violation + new cycle
  * orders/models.py imports orders.services, excused by a new
    ignore_imports entry the PR adds to the layers contract            → new architecture debt
  * orders/discounts.discount_label calls catalog.utils (others use catalog.services) → inferred interface deviation
Interface rings (schema 2.2.0, `.pr-explorer.yml` on main declares orders/notifications.py as an outer path):
  * PaymentSerializer (/payments/api/history/) gains provider_ref     → outer http_api, compatible (new response field)
  * OrderSummarySerializer.total renamed to grand_total               → outer http_api, breaking (renamed field)
  * new /orders/api/discounts/<str:code>/ API route                   → outer http_api, added (compatible)
  * orders/notifications.py (declared outer) changes                  → outer outbound_format, unknown
  * orders.services.create_order gains a required argument            → middle module_api, breaking
Layering for large repos: catalog/admin_api/ has 55 generated routes (catalog > 50 entry points → split into
cap:catalog/admin_api), and deploy/ holds a release script (→ an `engineering` capability).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

MARKER = ".pr-explorer-fixture"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = PROJECT_ROOT / ".data" / "fixtures" / "shop"
# Assembled at runtime so secret scanners / push protection never see a token-shaped literal in this repository.
FAKE_GH_TOKEN = "gh" + "p_" + "FAKEfixture0123456789abcdefghijKLMNOP"
FAKE_AWS_SECRET = "wJalr" + "XUtnFEMI/K7MDENG/bPxRfiCY" + "FIXTUREKEY"

MAIN: dict[str, str] = {
    "README.md": """# Shop

一個小型的 Django 網路商店後端。顧客瀏覽商品目錄、把商品放進購物車並結帳；結帳時系統建立訂單、
為每件商品保留庫存，並建立一筆付款，實際向外部金流扣款則在背景工作中完成。

## 業務模組

- `catalog`：商品目錄與售價，顧客瀏覽商品時使用。
- `orders`：購物車、結帳、訂單查詢與取消；每晚的排程會替常客重新下單。
- `inventory`：每件商品的庫存數量與結帳時的庫存保留；過期的保留每五分鐘由排程釋放，倉管人員用 `manage.py restock` 補貨。
- `payments`：付款紀錄、在背景向外部金流扣款、接收金流回呼 (webhook)，以及提供付款查詢 API。
- `core`：所有資料表共用的建立 / 更新時間欄位。

## Conventions

- Views that read or change orders must be decorated with `@login_required`.
- Views call functions in `services.py`; they should not query the ORM directly (service layer).
- Every model change must ship with a migration in the app's `migrations/` folder.
- Money is always computed with `Decimal`, never `float`.
- Models inherit `TimeStampedModel` so every row has created/updated timestamps.
""",
    "docs/adr/0001-service-layer.md": """# ADR 0001: Service layer

## Status

Accepted.

## Decision

- Business rules live in `services.py` modules; views stay thin and never touch `Model.objects` directly.
""",
    "requirements.txt": "Django==5.1.4\n",
    ".pr-explorer.yml": """# PR Review Explorer: interfaces clients depend on (spec §8 outer ring), on top of what it detects itself
outer:
  paths:
    - orders/notifications.py   # order messages customers receive
""",
    ".importlinter.ini": """[importlinter]
root_packages =
    core
    catalog
    orders
    inventory
    payments

[importlinter:contract:layers]
name = Django app layers
type = layers
containers =
    orders
    catalog
layers =
    views
    services
    models

[importlinter:contract:catalog-independent]
name = Catalog does not depend on orders
type = forbidden
source_modules =
    catalog
forbidden_modules =
    orders
""",
    "deploy/release.py": '''"""Release helper used by the CI pipeline: tags the commit and prints the image name."""

import subprocess


def tag(version):
    subprocess.run(["git", "tag", version], check=True)
    return f"shop:{version}"
''',
    "manage.py": '''#!/usr/bin/env python
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shop.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
''',
    "shop/__init__.py": "",
    "shop/settings.py": '''from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "django-insecure-fixture-only"
DEBUG = True
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "core",
    "catalog",
    "orders",
    "inventory",
    "payments",
    "rest_framework",
]
ROOT_URLCONF = "shop.urls"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
''',
    "shop/urls.py": '''from django.urls import include, path

urlpatterns = [
    path("catalog/", include("catalog.urls")),
    path("orders/", include("orders.urls")),
    path("payments/", include("payments.urls")),
    path("catalog-admin/", include("catalog.admin_api.urls")),
]
''',
    "shop/celery.py": '''from celery import Celery

app = Celery("shop")
app.conf.beat_schedule = {
    "release-expired-reservations": {"task": "inventory.tasks.release_expired_reservations", "schedule": 300.0},
}
''',
    "core/__init__.py": "",
    "core/models.py": '''from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
''',
    "catalog/__init__.py": "",
    "catalog/models.py": '''from django.db import models

from core.models import TimeStampedModel


class Product(TimeStampedModel):
    name = models.CharField(max_length=120)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name
''',
    "catalog/utils.py": '''from decimal import Decimal

CENT = Decimal("0.01")


def round_money(amount):
    return amount.quantize(CENT)
''',
    "catalog/services.py": '''from decimal import Decimal

from .models import Product
from .utils import round_money


def get_price(product):
    return product.price


def member_price(total, user):
    """Members get a fixed 10% off every order."""
    if user.is_member:
        return round_money(total * Decimal("0.9"))
    return total


def list_active_products():
    return Product.objects.filter(active=True).order_by("name")


def get_product(pk):
    return Product.objects.get(pk=pk, active=True)
''',
    "catalog/views.py": '''from django.shortcuts import render

from . import services


def product_list(request):
    products = services.list_active_products()
    return render(request, "catalog/list.html", {"products": products})


def product_detail(request, pk):
    product = services.get_product(pk)
    return render(request, "catalog/detail.html", {"product": product})
''',
    "catalog/urls.py": '''from django.urls import path

from . import views

app_name = "catalog"
urlpatterns = [
    path("", views.product_list, name="list"),
    path("<int:pk>/", views.product_detail, name="detail"),
]
''',
    "catalog/migrations/__init__.py": "",
    "catalog/migrations/0001_initial.py": '''from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Product",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=120)),
                ("price", models.DecimalField(max_digits=10, decimal_places=2)),
                ("active", models.BooleanField(default=True)),
            ],
        ),
    ]
''',
    "orders/__init__.py": "",
    "orders/models.py": '''from decimal import Decimal

from django.conf import settings
from django.db import models

from catalog.models import Product
from core.models import TimeStampedModel


class Order(TimeStampedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    status = models.CharField(max_length=16, default="new")
    total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))


class OrderItem(TimeStampedModel):
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
''',
    "orders/admin.py": '''from .models import Order, OrderItem


def register(site):
    site.register(Order)
    site.register(OrderItem)
''',
    "orders/notifications.py": '''import logging

logger = logging.getLogger(__name__)


def order_created(order):
    logger.info("order %s created", order.pk)


def order_cancelled(order):
    logger.info("order %s cancelled", order.pk)
''',
    "orders/services.py": '''from decimal import Decimal

from catalog.services import get_price, member_price
from inventory.services import reserve_stock
from payments.services import start_payment

from . import notifications
from .models import Order, OrderItem


def get_cart(user):
    """Return the user's open cart lines (simplified for the fixture)."""
    return list(user.cart_lines.all())


def calculate_total(cart):
    total = Decimal("0")
    for line in cart:
        total += get_price(line.product) * line.quantity
    return total


def create_order(cart, user):
    total = member_price(calculate_total(cart), user)
    order = Order.objects.create(user=user, total=total)
    for line in cart:
        OrderItem.objects.create(order=order, product=line.product, quantity=line.quantity, unit_price=get_price(line.product))
    reserve_stock(order, cart)
    start_payment(order)
    notifications.order_created(order)
    return order


def get_order_for_user(pk, user):
    return Order.objects.get(pk=pk, user=user)


def list_orders(user):
    return Order.objects.filter(user=user).order_by("-created_at")


def cancel_order(order_id, user):
    order = get_order_for_user(order_id, user)
    if order.status != "new":
        raise ValueError("only new orders can be cancelled")
    order.status = "cancelled"
    order.save()
    notifications.order_cancelled(order)
    return order
''',
    "orders/api.py": '''from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services


class OrderSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)


class OrderSummaryAPIView(APIView):
    """Order summary for the mobile app."""

    def get(self, request, pk):
        order = services.get_order_for_user(pk, request.user)
        return Response(OrderSummarySerializer(order).data)
''',
    "orders/forms.py": '''from django import forms


class CheckoutForm(forms.Form):
    shipping_address = forms.CharField(max_length=200)
''',
    "orders/views.py": '''from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.views import View

from . import services
from .forms import CheckoutForm


@login_required
def cart_detail(request):
    cart = services.get_cart(request.user)
    total = services.calculate_total(cart)
    return render(request, "orders/cart.html", {"cart": cart, "total": total})


@login_required
def checkout(request):
    cart = services.get_cart(request.user)
    form = CheckoutForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        order = services.create_order(cart, request.user)
        return redirect("orders:detail", pk=order.pk)
    return render(request, "orders/checkout.html", {"form": form, "cart": cart})


@login_required
def order_detail(request, pk):
    order = services.get_order_for_user(pk, request.user)
    return render(request, "orders/detail.html", {"order": order})


@login_required
def order_history(request):
    orders = services.list_orders(request.user)
    return render(request, "orders/history.html", {"orders": orders})


class OrderCancelView(LoginRequiredMixin, View):
    def post(self, request, pk):
        services.cancel_order(pk, request.user)
        return redirect("orders:history")
''',
    "orders/urls.py": '''from django.urls import path

from . import api, views

app_name = "orders"
urlpatterns = [
    path("api/<int:pk>/summary/", api.OrderSummaryAPIView.as_view(), name="api-summary"),
    path("cart/", views.cart_detail, name="cart"),
    path("checkout/", views.checkout, name="checkout"),
    path("<int:pk>/", views.order_detail, name="detail"),
    path("history/", views.order_history, name="history"),
    path("<int:pk>/cancel/", views.OrderCancelView.as_view(), name="cancel"),
]
''',
    "orders/tasks.py": '''from celery import shared_task

from . import services


@shared_task
def reorder_last(user):
    """Re-create the user's most recent order (called by the nightly job)."""
    last = services.list_orders(user).first()
    if last is None:
        return None
    cart = list(last.items.all())
    return services.create_order(cart, user)
''',
    "orders/jobs.py": '''from .tasks import reorder_last


def nightly_reorder(users):
    for user in users:
        reorder_last(user)
''',
    "orders/dispatch.py": '''"""Admin console: runs a service function chosen by name at runtime."""

from . import services


def run_action(name, *args):
    action = getattr(services, name)
    return action(*args)
''',
    "orders/migrations/__init__.py": "",
    "orders/migrations/0001_initial.py": '''from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [("catalog", "0001_initial")]
    operations = [
        migrations.CreateModel(
            name="Order",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("status", models.CharField(max_length=16, default="new")),
                ("total", models.DecimalField(max_digits=10, decimal_places=2)),
            ],
        ),
    ]
''',
    "orders/static/orders/api.js": '''export function postJSON(url, body) {
  return fetch(url, { method: "POST", body, headers: { "X-Requested-With": "fetch" } }).then((r) => r.json());
}
''',
    "orders/static/orders/checkout.js": '''import { postJSON } from "./api.js";

export function submitCheckout(form) {
  return postJSON("/orders/checkout/", new FormData(form));
}
''',
}

MAIN.update({
    "inventory/__init__.py": "",
    "inventory/models.py": """from django.db import models

from catalog.models import Product
from core.models import TimeStampedModel


class StockItem(TimeStampedModel):
    product = models.OneToOneField(Product, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=0)


class Reservation(TimeStampedModel):
    order_id = models.PositiveIntegerField()
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField()
    expires_at = models.DateTimeField()
""",
    "inventory/services.py": """from datetime import timedelta

from django.utils import timezone

from .models import Reservation, StockItem

HOLD_MINUTES = 30


def reserve_stock(order, cart):
    \"\"\"Hold stock for every cart line until payment completes.\"\"\"
    for line in cart:
        stock = StockItem.objects.get(product=line.product)
        if stock.quantity < line.quantity:
            raise ValueError(f"not enough stock for {line.product}")
        stock.quantity -= line.quantity
        stock.save()
        Reservation.objects.create(order_id=order.pk, product=line.product, quantity=line.quantity,
                                   expires_at=timezone.now() + timedelta(minutes=HOLD_MINUTES))


def release_expired():
    \"\"\"Give back stock held by reservations whose payment never completed.\"\"\"
    expired = Reservation.objects.filter(expires_at__lt=timezone.now())
    for r in expired:
        stock = StockItem.objects.get(product=r.product)
        stock.quantity += r.quantity
        stock.save()
    expired.delete()


def restock(product_id, quantity):
    stock, _ = StockItem.objects.get_or_create(product_id=product_id)
    stock.quantity += quantity
    stock.save()
    return stock
""",
    "inventory/tasks.py": """from celery import shared_task

from . import services


@shared_task
def release_expired_reservations():
    services.release_expired()
""",
    "inventory/management/__init__.py": "",
    "inventory/management/commands/__init__.py": "",
    "inventory/management/commands/restock.py": """from django.core.management.base import BaseCommand

from inventory import services


class Command(BaseCommand):
    help = "Add stock for a product (used by the warehouse team)."

    def add_arguments(self, parser):
        parser.add_argument("product_id", type=int)
        parser.add_argument("quantity", type=int)

    def handle(self, *args, **options):
        stock = services.restock(options["product_id"], options["quantity"])
        self.stdout.write(f"{stock.product_id}: {stock.quantity} in stock")
""",
    "inventory/migrations/__init__.py": "",
    "inventory/migrations/0001_initial.py": """from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [("catalog", "0001_initial")]
    operations = [
        migrations.CreateModel(name="StockItem", fields=[("id", models.BigAutoField(primary_key=True, serialize=False))]),
        migrations.CreateModel(name="Reservation", fields=[("id", models.BigAutoField(primary_key=True, serialize=False))]),
    ]
""",
    "payments/__init__.py": "",
    "payments/models.py": """from django.db import models

from core.models import TimeStampedModel


class Payment(TimeStampedModel):
    order = models.ForeignKey("orders.Order", related_name="payments", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=16, default="pending")
    provider_ref = models.CharField(max_length=64, blank=True, default="")
""",
    "payments/gateway.py": """import requests

GATEWAY_URL = "https://payments.example.invalid/v1/charges"


def charge(amount, reference):
    \"\"\"Ask the external payment provider to charge the customer.\"\"\"
    response = requests.post(GATEWAY_URL, json={"amount": str(amount), "reference": reference}, timeout=10)
    response.raise_for_status()
    return response.json()["id"]
""",
    "payments/services.py": """from . import gateway, tasks
from .models import Payment


def start_payment(order):
    payment = Payment.objects.create(order=order, amount=order.total)
    tasks.capture_payment.delay(payment.pk)
    return payment


def capture(payment_id):
    payment = Payment.objects.get(pk=payment_id)
    payment.provider_ref = gateway.charge(payment.amount, f"order-{payment.order_id}")
    payment.status = "captured"
    payment.save()
    return payment


def mark_failed(provider_ref):
    payment = Payment.objects.get(provider_ref=provider_ref)
    payment.status = "failed"
    payment.save()


def payments_for(user):
    return Payment.objects.filter(order__user=user).order_by("-created_at")
""",
    "payments/tasks.py": """from celery import shared_task

from . import services


@shared_task(bind=True, max_retries=3)
def capture_payment(self, payment_id):
    services.capture(payment_id)
""",
    "payments/serializers.py": """from rest_framework import serializers

from .models import Payment


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ["id", "status", "amount"]
        read_only_fields = ["id", "status", "amount"]
""",
    "payments/api.py": """from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .serializers import PaymentSerializer


class PaymentViewSet(viewsets.ViewSet):
    serializer_class = PaymentSerializer

    def list(self, request):
        payments = services.payments_for(request.user)
        return Response(PaymentSerializer(payments, many=True).data)


class GatewayWebhookView(APIView):
    authentication_classes = []

    def post(self, request):
        if request.data.get("event") == "charge.failed":
            services.mark_failed(request.data["reference"])
        return Response({"ok": True})
""",
    "payments/urls.py": """from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import api

router = DefaultRouter()
router.register(r"history", api.PaymentViewSet, basename="payment")

urlpatterns = [
    path("webhook/", api.GatewayWebhookView.as_view(), name="webhook"),
    path("api/", include(router.urls)),
]
""",
    "payments/migrations/__init__.py": "",
    "payments/migrations/0001_initial.py": """from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [("orders", "0001_initial")]
    operations = [
        migrations.CreateModel(name="Payment", fields=[("id", models.BigAutoField(primary_key=True, serialize=False))]),
    ]
""",
})
ADMIN_REPORTS = 55  # > SPLIT_MAX_ENTRY_POINTS: the catalog capability is split by sub-directory
MAIN["catalog/admin_api/__init__.py"] = ""
MAIN["catalog/admin_api/views.py"] = "from django.http import JsonResponse\n" + "".join(
    f"\n\ndef report_{i:02d}(request):\n    return JsonResponse({{\"report\": {i}}})\n" for i in range(1, ADMIN_REPORTS + 1))
MAIN["catalog/admin_api/urls.py"] = "from django.urls import path\n\nfrom . import views\n\nurlpatterns = [\n" + "".join(
    f"    path(\"reports/{i:02d}/\", views.report_{i:02d}),\n" for i in range(1, ADMIN_REPORTS + 1)) + "]\n"

for _app in ("core", "catalog", "orders", "inventory", "payments"):
    MAIN[f"{_app}/apps.py"] = (
        "from django.apps import AppConfig\n\n\n"
        f"class {_app.capitalize()}Config(AppConfig):\n    name = \"{_app}\"\n"
    )

FEATURE: dict[str, str | None] = {
    "orders/models.py": MAIN["orders/models.py"].replace(
        '''    total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))


class OrderItem''',
        '''    total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    discount_code = models.CharField(max_length=32, blank=True, default="")
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))

    def recalculate(self):
        self.total = calculate_total(self.items.all())


class DiscountCode(models.Model):
    KIND_CHOICES = [("percent", "Percent off"), ("fixed", "Fixed amount off")]

    code = models.CharField(max_length=32, unique=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    value = models.DecimalField(max_digits=10, decimal_places=2)
    active = models.BooleanField(default=True)


class OrderItem''',
    ).replace("from core.models import TimeStampedModel\n",
              "from core.models import TimeStampedModel\n\nfrom .services import calculate_total\n"),
    # the PR excuses the models → services import with a new exception instead of fixing it (new architecture debt)
    ".importlinter.ini": MAIN[".importlinter.ini"].replace(
        "    services\n    models\n",
        "    services\n    models\nignore_imports =\n    orders.models -> orders.services\n"),
    # the catalog now shows discounted prices by importing from orders: breaks the forbidden contract, closes a cycle
    "catalog/services.py": MAIN["catalog/services.py"].replace(
        "from .utils import round_money\n",
        "from .utils import round_money\nfrom orders.discounts import apply_percent_discount\n",
    ) + '''

def sale_price(product, discount):
    return apply_percent_discount(product.price, discount)
''',
    "orders/discounts.py": '''from decimal import Decimal

from catalog.utils import round_money

CENT = Decimal("0.01")


def apply_percent_discount(total, discount):
    return (total * (Decimal("100") - discount.value) / Decimal("100")).quantize(CENT)


def apply_fixed_discount(total, discount):
    return max(total - discount.value, Decimal("0"))


def discount_label(discount):
    """Shown next to the code at checkout, e.g. "10.00 off"."""
    return f"{round_money(discount.value)} off" if discount.kind == "fixed" else f"{discount.value}% off"
''',
    "orders/notifications.py": '''import logging

logger = logging.getLogger(__name__)


def order_created(order):
    logger.info("order %s created (discount %s)", order.pk, order.discount_code or "-")


def order_status_changed(order, status):
    logger.info("order %s is now %s", order.pk, status)
''',
    "orders/services.py": MAIN["orders/services.py"]
    .replace("from catalog.services import get_price, member_price", "from catalog.services import get_price")
    .replace("from .models import Order, OrderItem", "from . import discounts\nfrom .models import DiscountCode, Order, OrderItem")
    .replace("from . import notifications\nfrom . import discounts", "from . import discounts, notifications")
    .replace(
        '''def create_order(cart, user):
    total = member_price(calculate_total(cart), user)
    order = Order.objects.create(user=user, total=total)''',
        '''def find_discount(code):
    return DiscountCode.objects.filter(code__iexact=code, active=True).first()


def apply_discount(total, discount):
    handler = getattr(discounts, f"apply_{discount.kind}_discount")
    return handler(total, discount)


def create_order(cart, user, discount_code):
    total = calculate_total(cart)
    discount_amount = Decimal("0")
    if discount_code:
        discount = find_discount(discount_code)
        if discount is None:
            raise ValueError(f"unknown discount code {discount_code!r}")
        discounted = apply_discount(total, discount)
        discount_amount = total - discounted
        total = discounted
    order = Order.objects.create(user=user, total=total, discount_code=discount_code or "", discount_amount=discount_amount)''',
    ),
    "orders/api.py": '''from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services


class OrderSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)


class OrderSummaryAPIView(APIView):
    """Order summary for the mobile app."""

    def get(self, request, pk):
        order = services.get_order_for_user(pk, request.user)
        return Response(OrderSummarySerializer(order).data)
''',
    "orders/forms.py": '''from django import forms


class CheckoutForm(forms.Form):
    shipping_address = forms.CharField(max_length=200)
    discount_code = forms.CharField(max_length=32, required=False)
''',
    "orders/views.py": MAIN["orders/views.py"]
    .replace(
        "from django.shortcuts import redirect, render",
        "from django.http import JsonResponse\nfrom django.shortcuts import redirect, render",
    )
    .replace("from .forms import CheckoutForm", "from .forms import CheckoutForm\nfrom .models import DiscountCode")
    .replace(
        "        order = services.create_order(cart, request.user)\n",
        '        order = services.create_order(cart, request.user, form.cleaned_data.get("discount_code"))\n',
    )
    .replace(
        "\n\n@login_required\ndef order_detail(request, pk):",
        f'''


def apply_discount_code(request):
    """Preview a discount code against the current cart (called from checkout.js)."""
    if request.headers.get("X-Partner-Token") == "{FAKE_GH_TOKEN}":
        partner_total = services.calculate_total(services.get_cart(request.user))
        return JsonResponse({{"valid": True, "partner": True, "total": str(partner_total)}})
    code = request.GET.get("code", "")
    discount = DiscountCode.objects.filter(code__iexact=code, active=True).first()
    if discount is None:
        return JsonResponse({{"valid": False}})
    cart = services.get_cart(request.user)
    total = services.calculate_total(cart)
    return JsonResponse({{"valid": True, "total": str(services.apply_discount(total, discount))}})


@login_required
def order_detail(request, pk):''',
    ),
    "orders/urls.py": MAIN["orders/urls.py"].replace(
        '''    path("checkout/", views.checkout, name="checkout"),
''',
        '''    path("checkout/", views.checkout, name="checkout"),
    path("checkout/apply-discount/", views.apply_discount_code, name="apply-discount"),
''',
    ),
    "orders/static/orders/checkout.js": '''import { postJSON } from "./api.js";

export function submitCheckout(form) {
  return postJSON("/orders/checkout/", new FormData(form));
}

export function applyDiscount(code) {
  return fetch("/orders/checkout/apply-discount/?code=" + encodeURIComponent(code)).then((r) => r.json());
}
''',
    "docs/deploy.md": f"""# Deployment

## Rules

- Never commit credentials; an example of what must stay out of git: `AWS_SECRET_ACCESS_KEY={FAKE_AWS_SECRET}`.
- Deploys must run `python manage.py migrate` before switching traffic.
""",
}

# interface changes (schema 2.2.0): one compatible response field, one renamed field, one new API route
FEATURE["payments/serializers.py"] = MAIN["payments/serializers.py"].replace(
    'fields = ["id", "status", "amount"]\n        read_only_fields = ["id", "status", "amount"]',
    'fields = ["id", "status", "amount", "provider_ref"]\n        read_only_fields = ["id", "status", "amount", "provider_ref"]')
FEATURE["orders/api.py"] = MAIN["orders/api.py"].replace(
    "    total = serializers.DecimalField(", "    grand_total = serializers.DecimalField(") + '''

class DiscountCheckAPIView(APIView):
    """Lets the mobile app check a discount code before checkout."""

    def get(self, request, code):
        discount = services.find_discount(code)
        return Response({"valid": discount is not None})
'''
FEATURE["orders/urls.py"] = FEATURE["orders/urls.py"].replace(
    '    path("cart/", views.cart_detail, name="cart"),\n',
    '    path("api/discounts/<str:code>/", api.DiscountCheckAPIView.as_view(), name="api-discount"),\n'
    '    path("cart/", views.cart_detail, name="cart"),\n')
assert FEATURE["payments/serializers.py"] != MAIN["payments/serializers.py"] and "grand_total" in FEATURE["orders/api.py"]
assert "api-discount" in FEATURE["orders/urls.py"]

# Second PR on the same base: the restock command e-mails purchasing when stock is still low.
RESTOCK_ALERT: dict[str, str | None] = {
    "inventory/alerts.py": """from django.core.mail import send_mail

LOW_STOCK = 5


def notify_low_stock(stock):
    if stock.quantity < LOW_STOCK:
        send_mail("Low stock", f"product {stock.product_id}: {stock.quantity} left", None, ["purchasing@example.invalid"])
""",
    "inventory/services.py": MAIN["inventory/services.py"]
    .replace("from .models import Reservation, StockItem", "from . import alerts\nfrom .models import Reservation, StockItem")
    .replace("    stock.save()\n    return stock\n", "    stock.save()\n    alerts.notify_low_stock(stock)\n    return stock\n"),
}

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fixture Bot", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture Bot", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}


def _git(repo: Path, *args: str, date: str = "2026-01-01T09:00:00+00:00") -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_ENV, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date, GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull)
    out = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=repo, env=env, check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def _write(repo: Path, files: dict[str, str | None]) -> None:
    for rel, content in files.items():
        p = repo / rel
        if content is None:
            p.unlink(missing_ok=True)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, "utf-8")


def build(out: Path = DEFAULT_OUT) -> Path:
    out = out.resolve()
    if out.exists():
        if not (out / MARKER).exists() and any(out.iterdir()):
            raise SystemExit(f"refusing to overwrite {out}: not a fixture directory (no {MARKER})")
        shutil.rmtree(out)  # regenerable: the fixture is fully deterministic
    out.mkdir(parents=True)
    _git(out, "init", "-q")
    (out / MARKER).write_text("generated by make_django_fixture.py\n")
    (out / ".git" / "info" / "exclude").write_text(f"{MARKER}\n")
    _write(out, MAIN)
    _git(out, "add", "-A")
    _git(out, "commit", "-q", "-m", "Shop: catalog, orders, inventory and payments apps")
    _git(out, "checkout", "-q", "-b", "feature/discount")
    _write(out, FEATURE)
    _git(out, "add", "-A", date="2026-01-02T09:00:00+00:00")
    _git(out, "commit", "-q", "-m", "Add discount codes to checkout", "-m",
         "Customers can enter a discount code at checkout; percent and fixed discounts are supported. "
         "Discount codes replace the fixed 10% member discount.",
         date="2026-01-02T09:00:00+00:00")
    _git(out, "checkout", "-q", "main")
    _git(out, "checkout", "-q", "-b", "feature/restock-alert")
    _write(out, RESTOCK_ALERT)
    _git(out, "add", "-A", date="2026-01-03T09:00:00+00:00")
    _git(out, "commit", "-q", "-m", "E-mail purchasing when a restock leaves stock low",
         "-m", "After `manage.py restock`, purchasing gets an e-mail if fewer than 5 units are left.",
         date="2026-01-03T09:00:00+00:00")
    _git(out, "checkout", "-q", "main")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    path = build(ap.parse_args().out)
    print(path)
    print("main            ", _git(path, "rev-parse", "main"))
    print("feature/discount", _git(path, "rev-parse", "feature/discount"))
    print("feature/restock-alert", _git(path, "rev-parse", "feature/restock-alert"))


if __name__ == "__main__":
    main()
