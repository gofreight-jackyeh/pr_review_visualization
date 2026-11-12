"""Incremental static analysis (per-file result cache) gives the same PR result as a full run: the shop fixture plus a
TypeScript frontend, a PR analyzed with a cache warmed by an earlier PR on an older base, vs the same PR with
PR_EXPLORER_INCREMENTAL=off. Real git, real ts-analyzer, no Claude."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pr_explorer.application.pipeline import PipelineConfig
from pr_explorer.application.ports import AnalysisRequest, LocalSpec
from pr_explorer.bootstrap import PROJECT_ROOT, build_container

CLI = PROJECT_ROOT / "tools/ts-analyzer/dist/cli.js"
pytestmark = pytest.mark.skipif(not CLI.is_file() or shutil.which("node") is None, reason="ts-analyzer not built")

FRONTEND = {
    "tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@lib/*": ["web/lib/*"]}}}',
    "web/lib/price.ts": "export function price(n: number) { return n * 2 }\nexport function tax(n: number) { return n / 10 }\n",
    "web/lib/index.ts": "export * from './price'\n",
    "web/lib/cart.ts": "import { price } from './index'\nexport class Cart { total(n: number) { return price(n) } }\n",
    "web/pages/Checkout.tsx": "import { Cart } from '@lib/cart'\nimport { tax } from '../lib'\n"
                              "export function Checkout() { const c = new Cart(); return <b>{c.total(tax(1))}</b> }\n",
    "web/pages/Orders.tsx": "import { price } from '../lib'\nexport function Orders() { return <i>{price(3)}</i> }\n",
    "web/legacy/util.js": "function fmtMoney(x) { return '$' + x }\n",
    "web/legacy/view.js": "function render(x) { return fmtMoney(x) }\n",
}
# what the docs must agree on (owner's rule: same meaning; graph-internal counts may differ — here they do not)
MUST_MAP = ("capabilities", "entry_points", "interfaces", "flows", "relations", "architecture_rules", "entities")
MUST_OVERLAY = ("changes", "impacts", "flow_changes", "head_additions", "findings", "architecture_checks", "coverage")


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_AUTHOR_DATE": "2026-02-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-02-01T00:00:00Z"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str) -> None:
    for p, text in files.items():
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _analyze(home: Path, repo: Path, base: str, head: str, incremental: bool) -> tuple[dict, dict, object]:
    c = build_container(home=home, config=PipelineConfig(output_language="en", incremental=incremental))
    paths, _ = c.analyze.analyze_sync(AnalysisRequest(local=LocalSpec(str(repo), base, head), semantic_mode="off"))
    return json.loads(Path(paths["system_map"]).read_text()), json.loads(Path(paths["overlay"]).read_text()), c


def _ts(c):
    return next(a for a in c.analyze.pipeline.d.analyzers if a.name == "typescript") if hasattr(c.analyze, "pipeline") \
        else next(a for v in vars(c.analyze).values() if hasattr(v, "d") for a in v.d.analyzers if a.name == "typescript")


def test_incremental_run_equals_full_run_on_must_items(fresh_shop, tmp_path):
    repo = fresh_shop
    _git(repo, "checkout", "-q", "main")
    _commit(repo, FRONTEND, "Add the web frontend")
    _git(repo, "checkout", "-q", "-b", "early")
    _commit(repo, {"web/pages/Orders.tsx": FRONTEND["web/pages/Orders.tsx"].replace("price(3)", "price(4)")}, "early PR")
    early_base = _git(repo, "rev-parse", "main")
    _git(repo, "checkout", "-q", "main")
    services = (repo / "catalog/services.py").read_text()
    _commit(repo, {"web/lib/price.ts": FRONTEND["web/lib/price.ts"].replace("n * 2", "n * 2 + 0"),
                   "web/legacy/util2.js": "function fmtDate(d) { return String(d) }\n",
                   "catalog/services.py": services + "\n\ndef extra_helper():\n    return 1\n"}, "base moves on")
    _git(repo, "checkout", "-q", "-b", "feature/cart")
    _commit(repo, {"web/lib/cart.ts": FRONTEND["web/lib/cart.ts"].replace("price(n)", "price(n) + 1")
                   .replace("return price", "const p = price(n); return p ||"),
                   "web/legacy/view.js": FRONTEND["web/legacy/view.js"] + "function when(d) { return fmtDate(d) }\n"},
            "cart PR")
    _git(repo, "checkout", "-q", "main")

    full_map, full_ov, _ = _analyze(tmp_path / "full", repo, "main", "feature/cart", incremental=False)
    warm = tmp_path / "inc"
    _analyze(warm, repo, early_base, "early", incremental=True)  # an earlier PR on an older base fills the cache
    inc_map, inc_ov, c = _analyze(warm, repo, "main", "feature/cart", incremental=True)

    assert full_map["commit_sha"] == inc_map["commit_sha"] != early_base
    for k in MUST_MAP:
        assert inc_map[k] == full_map[k], k
    for k in MUST_OVERLAY:
        assert inc_ov[k] == full_ov[k], k
    assert any(i["target_id"].startswith("node:ts:") for i in full_ov["impacts"])  # the TS change does matter here
    ts = _ts(c)
    assert ts.last_stats["mode"] == "incremental" and 0 < ts.last_stats["analyzed"] < ts.last_stats["files"]
    assert (warm / "analysis-cache.db").is_file() and not (tmp_path / "full" / "analysis-cache.db").exists()
