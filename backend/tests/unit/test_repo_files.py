"""Untrusted PR content: symlinks and non-regular files must read as missing, promptly (spec §11)."""

import os
import threading

from pr_explorer.infrastructure.security.repo_files import read_repo_bytes


def _within(seconds, fn):
    out = []
    t = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "read did not return promptly"
    return out[0]


def test_regular_file_is_read_up_to_the_cap(tmp_path):
    (tmp_path / "a.py").write_bytes(b"x" * 100)
    assert read_repo_bytes(tmp_path, "a.py", 10) == b"x" * 10
    assert read_repo_bytes(tmp_path, "missing.py", 10) is None


def test_symlinks_fifos_and_devices_read_as_missing(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (tmp_path / "outside.py").write_text("def outside_secret_fn(): pass\n")
    (root / "inside.py").write_text("ok\n")
    os.symlink(tmp_path / "outside.py", root / "leak.py")
    os.symlink(root / "inside.py", root / "alias.py")  # even an in-repo symlink: never followed
    os.symlink(tmp_path, root / "pkg" / "up")  # symlinked directory escaping the root
    os.symlink("/dev/zero", root / "zero.py")
    os.mkfifo(root / "pipe.py")
    for rel in ("leak.py", "alias.py", "pkg/up/outside.py", "zero.py", "pipe.py", "pkg"):
        assert _within(5, lambda: read_repo_bytes(root, rel, 1_000_000)) is None, rel
    assert read_repo_bytes(root, "inside.py", 100) == b"ok\n"
