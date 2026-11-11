"""A data home copied to another machine (different $PR_EXPLORER_HOME) must still serve cache hits."""
import shutil
import sqlite3

from pr_explorer.application.ports import ResolvedPr
from pr_explorer.infrastructure.persistence.sqlite.analysis_repository import DB_NAME, SqliteAnalysisRepository

PR = ResolvedPr(source="github", repo_key="gh-acme-shop-0", repo_name="shop", repo_owner="acme", base_ref="main",
                head_ref="feature", base_sha="a" * 40, head_sha="b" * 40, merge_base_sha="a" * 40, title="t", body="")


def _write(home, rel):
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    return p


def test_paths_are_relative_and_resolve_under_the_new_home(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    repo = SqliteAnalysisRepository(a / DB_NAME)
    repo.index_system_map("sm_1", "k1", str(_write(a, "system-maps/sm_1.v2.json")), "gh-acme-shop-0", "a" * 40, "complete")
    repo.index_overlay("ov_1", "k2", str(_write(a, "overlays/ov_1.v2.json")), "an_1", PR, "sm_1")
    raw = sqlite3.connect(a / DB_NAME).execute("SELECT path FROM system_maps").fetchone()[0]
    assert raw == "system-maps/sm_1.v2.json"

    shutil.copytree(a, b)
    moved = SqliteAnalysisRepository(b / DB_NAME)
    assert moved.find_system_map("k1")["path"] == str(b.resolve() / "system-maps/sm_1.v2.json")
    assert moved.find_overlay("k2")["path"] == str(b.resolve() / "overlays/ov_1.v2.json")


def test_absolute_paths_from_an_older_database_are_rebased(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    SqliteAnalysisRepository(a / DB_NAME)
    con = sqlite3.connect(a / DB_NAME)
    con.execute("INSERT INTO system_maps VALUES (?,?,?,?,?,?,?)",
                ("sm_1", "k1", "/elsewhere/home/system-maps/sm_1.v2.json", "r", "a" * 40, "complete", "t"))
    con.commit()
    con.close()
    shutil.copytree(a, b)
    _write(b, "system-maps/sm_1.v2.json")
    assert SqliteAnalysisRepository(b / DB_NAME).get_system_map("sm_1")["path"] == str(b.resolve() / "system-maps/sm_1.v2.json")
