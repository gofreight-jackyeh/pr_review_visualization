"""Infrastructure-as-code repositories (trial 2026-11: a Terraform repo's map had one capability and empty flows)."""

from pr_explorer.domain.services.repo_kind import iac_kind, repo_kind

TERRAFORM = {"modules/vpc/main.tf", "modules/vpc/variables.tf", "envs/prod/main.tf", "envs/prod/terraform.tfvars",
             "README.md", ".github/workflows/plan.yml", "scripts/fmt.sh"}


def test_iac_kind_by_path():
    assert iac_kind("modules/vpc/main.tf") == "terraform" and iac_kind("live/terragrunt.hcl") == "terraform"
    assert iac_kind("charts/api/templates/deployment.yaml") == "helm" and iac_kind("charts/api/Chart.yaml") == "helm"
    assert iac_kind("k8s/base/service.yaml") == "kubernetes" and iac_kind("apps/x/kustomization.yaml") == "kubernetes"
    assert iac_kind(".github/workflows/plan.yml") is None and iac_kind("config/settings.yaml") is None


def test_repo_kind_needs_no_entry_points_and_mostly_iac():
    assert repo_kind(TERRAFORM, 0) == "infrastructure"
    assert repo_kind(TERRAFORM | {"lambda/handler.py"}, 0) == "infrastructure"  # a helper script does not make it an app
    assert repo_kind(TERRAFORM, 3) == "application"  # anything with routes / tasks / commands is an application
    assert repo_kind({f"app/m{i}.py" for i in range(10)} | {"terraform/main.tf"}, 0) == "application"  # a library
    assert repo_kind({"README.md"}, 0) == "application"


def test_system_map_records_the_kind(shop_docs):
    smap, *_ = shop_docs
    assert smap["system"]["repo_kind"] == "application" and smap["schema_version"] == "2.6.0"
