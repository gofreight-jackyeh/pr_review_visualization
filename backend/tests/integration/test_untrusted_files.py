"""A PR that commits symlinks out of the repository must not leak outside content into the analysis, nor hang it."""

import json
import os
import subprocess
import threading
from pathlib import Path

from conftest import local_request

from pr_explorer.bootstrap import build_container

SECRET = "zz_outside_root_secret_fn"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True)


def test_symlinks_in_a_pr_are_not_followed(tmp_path, home):
    outside = tmp_path / "outside"
    outside.mkdir()
    for name in ("secret.py", "secret.ts", "secret.rb", "SECRET.md"):
        (outside / name).write_text(f"def {SECRET}():\n    pass\n" if name.endswith(".py") else f"// {SECRET}\nexport function {SECRET}() {{}}\n")
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "core.py").write_text("def run():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "feature")
    (repo / "app" / "core.py").write_text("from .leak import *\n\n\ndef run():\n    return 2\n")
    for link, target in (("app/leak.py", outside / "secret.py"), ("app/zero.py", "/dev/zero"), ("web/leak.ts", outside / "secret.ts"),
                         ("lib/leak.rb", outside / "secret.rb"), ("LEAK.md", outside / "SECRET.md")):
        (repo / link).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, repo / link)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "symlinks")

    c = build_container(home=home)
    result = []
    t = threading.Thread(target=lambda: result.append(c.analyze.analyze_sync(local_request(repo, head="feature"))), daemon=True)
    t.start()
    t.join(120)
    assert not t.is_alive(), "analysis hung on a symlinked device file"
    paths, _ = result[0]
    for kind in ("system_map", "overlay"):
        text = Path(paths[kind]).read_text()
        json.loads(text)
        assert SECRET not in text, kind
