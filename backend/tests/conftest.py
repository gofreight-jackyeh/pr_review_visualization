from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import make_django_fixture  # noqa: E402

from pr_explorer.application.ports import AnalysisRequest, LocalSpec  # noqa: E402
from pr_explorer.bootstrap import build_container  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="session")
def shop_repo(tmp_path_factory) -> Path:
    return make_django_fixture.build(tmp_path_factory.mktemp("fixture") / "shop")


@pytest.fixture()
def fresh_shop(tmp_path) -> Path:
    """A private copy for tests that mutate the repo (freshness)."""
    return make_django_fixture.build(tmp_path / "shop")


@pytest.fixture()
def home(tmp_path) -> Path:
    return tmp_path / "home"


def local_request(repo: Path, mode: str = "off", head: str = "feature/discount", **kw) -> AnalysisRequest:
    return AnalysisRequest(local=LocalSpec(str(repo), "main", head), semantic_mode=mode, **kw)


def load_docs(paths: dict) -> tuple[dict, dict]:
    import json

    return json.loads(Path(paths["system_map"]).read_text()), json.loads(Path(paths["overlay"]).read_text())


@pytest.fixture(scope="session")
def shop_docs(shop_repo, tmp_path_factory):
    """(system map, overlay, paths, container) for the discount PR analyzed with semantic off."""
    c = build_container(home=tmp_path_factory.mktemp("home"))
    paths, _ = c.analyze.analyze_sync(local_request(shop_repo))
    smap, overlay = load_docs(paths)
    return smap, overlay, paths, c
