"""Django-aware rules: class/function views, DRF, models, ORM managers, forms, serializers, tasks."""
from __future__ import annotations

VIEW_BASES = {
    "View", "TemplateView", "ListView", "DetailView", "FormView", "CreateView", "UpdateView", "DeleteView",
    "RedirectView", "APIView", "GenericAPIView", "ViewSet", "ModelViewSet", "ReadOnlyModelViewSet", "GenericViewSet",
}
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
VIEWSET_ACTIONS = ("list", "create", "retrieve", "update", "partial_update", "destroy")
MANAGER_ATTRS = {"objects", "_default_manager", "_base_manager"}
TASK_DECORATORS = {"shared_task", "task", "periodic_task", "actor"}
# ORM manager / queryset method -> business action on the entity
ORM_ACTIONS = {
    "create": "create", "get_or_create": "create", "bulk_create": "create", "update_or_create": "write",
    "update": "write", "bulk_update": "write", "save": "write", "delete": "delete",
}
INSTANCE_ACTIONS = {"save": "write", "delete": "delete", "refresh_from_db": "read"}
# Calls whose root module talks to the outside world (spec: emit / external steps)
EXTERNAL_MODULES = {"requests", "httpx", "urllib", "urllib3", "aiohttp", "boto3", "botocore", "stripe", "smtplib",
                    "paramiko", "ftplib", "socket", "redis", "kafka", "pika", "twilio", "sendgrid", "slack_sdk"}
EXTERNAL_CALLS = {"django.core.mail.send_mail", "django.core.mail.send_mass_mail", "django.core.mail.mail_admins",
                  "django.core.mail.EmailMessage", "django.core.mail.EmailMultiAlternatives"}


def classify_class(base_texts: list[str], resolved_base_kinds: list[str]) -> str:
    """Kind for a class given its base expressions and the kinds of project bases already classified."""
    for kind in ("model", "view", "serializer", "form"):
        if kind in resolved_base_kinds:
            return kind
    for b in base_texts:
        short = b.rsplit(".", 1)[-1]
        if b in {"models.Model", "django.db.models.Model"} or short == "Model" and b.startswith("models"):
            return "model"
    for b in base_texts:
        short = b.rsplit(".", 1)[-1]
        if short in VIEW_BASES or short.endswith("View") or short.endswith("ViewSet"):
            return "view"
    for b in base_texts:
        if b.rsplit(".", 1)[-1].endswith("Serializer"):
            return "serializer"
    for b in base_texts:
        short = b.rsplit(".", 1)[-1]
        if short in {"Form", "ModelForm"} or short.endswith("Form"):
            return "form"
    return "class"


def is_drf(base_texts: list[str], decorators: list[str]) -> bool:
    shorts = {b.rsplit(".", 1)[-1] for b in base_texts}
    return bool("api_view" in decorators or any(s.endswith("APIView") or s.endswith("ViewSet") for s in shorts))


def is_viewset(base_texts: list[str]) -> bool:
    return any(b.rsplit(".", 1)[-1].endswith("ViewSet") for b in base_texts)


def is_function_view(path: str, first_param: str | None) -> bool:
    in_views = path.endswith("views.py") or "/views/" in path
    return in_views and first_param == "request"


def orm_action(method: str) -> str:
    return ORM_ACTIONS.get(method, "read")


def command_name(path: str) -> str | None:
    """`<app>/management/commands/<name>.py` → name (Django management command)."""
    parts = path.split("/")
    if len(parts) >= 3 and parts[-3] == "management" and parts[-2] == "commands" and parts[-1].endswith(".py"):
        name = parts[-1][:-3]
        return None if name.startswith("_") else name
    return None
