"""Is this repository an application, or infrastructure as code (Terraform / Helm / Kubernetes manifests)? Pure.

An IaC repository has no entry points (routes, tasks, commands, pages) and at least as many IaC files as application
source files: its system map and flows are empty by construction, so the UI leads with the PR summary, the review focus
and the changed files grouped by module instead. Path-based on purpose (cheap, no file reads); the ceiling is a
manifest directory with an unusual name, which counts as plain YAML (upgrade path: read `apiVersion:` / `kind:`).
"""

from __future__ import annotations

TERRAFORM_EXT = (".tf", ".tfvars", ".hcl", ".tf.json")
HELM_FILES = {"chart.yaml", "values.yaml", "helmfile.yaml", "requirements.yaml"}
KUSTOMIZE_FILES = {"kustomization.yaml", "kustomization.yml"}
K8S_DIRS = {"k8s", "kubernetes", "manifests", "kustomize", "overlays", "argocd", "helm", "charts"}
CODE_EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rb", ".java", ".kt", ".rs", ".php", ".cs",
            ".swift", ".scala", ".vue", ".svelte")


def iac_kind(path: str) -> str | None:
    """terraform | helm | kubernetes for an infrastructure-as-code file, None otherwise."""
    parts = path.lower().split("/")
    if any(p.startswith(".") for p in parts[:-1]):
        return None  # .github/workflows and other tool directories
    name = parts[-1]
    if name.endswith(TERRAFORM_EXT):
        return "terraform"
    if name in HELM_FILES or ("templates" in parts[:-1] and any(p in ("helm", "charts") for p in parts[:-1])):
        return "helm"
    if name in KUSTOMIZE_FILES or (name.endswith((".yaml", ".yml")) and any(p in K8S_DIRS for p in parts[:-1])):
        return "kubernetes"
    return None


def repo_kind(files: set[str], entry_points: int) -> str:
    """'infrastructure' or 'application' (the default whenever there is anything to run)."""
    if entry_points:
        return "application"
    iac = sum(1 for f in files if iac_kind(f))
    code = sum(1 for f in files if f.lower().endswith(CODE_EXT))
    return "infrastructure" if iac and iac >= code else "application"


__all__ = ["iac_kind", "repo_kind"]
