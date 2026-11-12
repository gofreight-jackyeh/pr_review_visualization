"""HTTP contract per docs/api.md v2, in-process (TestClient), against the local fixture."""

import time

from fastapi.testclient import TestClient

from pr_explorer.application.pipeline import STAGES
from pr_explorer.bootstrap import build_container
from pr_explorer.interface.http.app import create_app


def _wait(client, aid):
    for _ in range(300):
        st = client.get(f"/api/analyses/{aid}").json()
        if st["status"] in ("done", "failed"):
            return st
        time.sleep(0.1)
    raise AssertionError("analysis did not finish")


def test_http_flow(shop_repo, home):
    client = TestClient(create_app(build_container(home=home)), base_url="http://localhost")
    assert client.get("/api/analyses").json() == []
    body = {"local": {"repo_path": str(shop_repo), "base": "main", "head": "feature/discount"}, "semantic_mode": "off"}
    r = client.post("/api/analyses", json=body)
    assert r.status_code == 202 and r.json()["cached"] is False
    st = _wait(client, r.json()["analysis_id"])
    assert st["status"] == "done", st
    assert [s["name"] for s in st["stages"]] == STAGES == ["acquire", "system_map", "pr_static", "flows", "impact", "semantic", "assemble"]
    assert dict((s["name"], s["status"]) for s in st["stages"]) == {
        "acquire": "done", "system_map": "done", "pr_static": "done", "flows": "done", "impact": "done", "semantic": "skipped",
        "assemble": "done"}
    ov = client.get(f"/api/overlays/{st['overlay_id']}").json()
    smap = client.get(f"/api/system-maps/{st['system_map_id']}").json()
    assert ov["kind"] == "pr_overlay" and ov["overlay_id"] == st["overlay_id"] and ov["system_map_id"] == st["system_map_id"]
    assert smap["kind"] == "system_map" and smap["system_map_id"] == st["system_map_id"]

    r2 = client.post("/api/analyses", json=body)
    assert r2.status_code == 200 and r2.json() == {"analysis_id": r2.json()["analysis_id"], "status": "done", "cached": True}

    r_force = client.post("/api/analyses", json={**body, "force": True})  # retry: bypasses the cache, same overlay id
    assert r_force.status_code == 202 and r_force.json()["cached"] is False
    assert _wait(client, r_force.json()["analysis_id"])["overlay_id"] == st["overlay_id"]

    # 「重新產生 AI 說明」: the server repeats the stored request (same mode) -> same overlay id; unknown overlay -> 404
    r_regen = client.post(f"/api/overlays/{st['overlay_id']}/regenerate")
    assert r_regen.status_code == 202 and r_regen.json()["cached"] is False
    assert _wait(client, r_regen.json()["analysis_id"])["overlay_id"] == st["overlay_id"]
    assert client.post("/api/overlays/ov_00000000000000000000/regenerate").status_code == 404

    # a second PR on the same base reuses the map
    body3 = {"local": {"repo_path": str(shop_repo), "base": "main", "head": "feature/restock-alert"}, "semantic_mode": "off"}
    st3 = _wait(client, client.post("/api/analyses", json=body3).json()["analysis_id"])
    sm_stage = next(s for s in st3["stages"] if s["name"] == "system_map")
    assert (sm_stage["status"], sm_stage["message"]) == ("skipped", f"reused {st['system_map_id']}")
    assert st3["system_map_id"] == st["system_map_id"] and st3["overlay_id"] != st["overlay_id"]

    assert client.get(f"/api/overlays/{st['overlay_id']}/freshness").json()["stale"] is False
    for missing in ("/api/overlays/ov_missing", "/api/system-maps/sm_missing", "/api/overlays/ov_missing/freshness"):
        assert client.get(missing).json()["error"]["code"] == "not_found", missing
    # v1 routes are gone: there is no source-code endpoint any more (spec v2 §4)
    for gone in (f"/api/artifacts/{st['overlay_id']}", f"/api/artifacts/{st['overlay_id']}/source?side=head&path=orders/views.py"):
        r = client.get(gone)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", gone
    listing = client.get("/api/analyses").json()
    assert len(listing) == 5 and listing[0]["analysis_id"] == st3["analysis_id"]  # + the forced run and the regenerate run
    assert {"overlay_id", "system_map_id", "title", "repo", "head_sha", "created_at", "status"} <= listing[0].keys()


def test_http_input_errors(home):
    client = TestClient(create_app(build_container(home=home)), base_url="http://localhost")
    for body in [{}, {"pr_url": "https://github.com/a/b/pull/1", "local": {"repo_path": "/x", "base": "a", "head": "b"}},
                 {"local": {"repo_path": "/x", "base": "a", "head": "b"}, "semantic_mode": "turbo"}]:
        r = client.post("/api/analyses", json=body)
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_input", body
    r = client.post("/api/analyses", json={"local": {"repo_path": "/definitely/not/here", "base": "a", "head": "b"}})
    assert r.status_code == 400 and r.json()["error"]["code"] == "acquire_failed"
    r = client.post("/api/analyses", json={"pr_url": "https://example.com/not-a-pr"})
    assert r.json()["error"]["code"] == "acquire_failed"


def test_host_header_allowlist_blocks_dns_rebinding(home, monkeypatch):
    import socket

    monkeypatch.setenv("PR_EXPLORER_ALLOWED_HOSTS", " Review.Example.com , other.test")
    monkeypatch.setattr(socket, "gethostname", lambda: "Dev-Box.local")
    client = TestClient(create_app(build_container(home=home)))
    for host in ("evil.example", "evil.example:8766", "localhost.evil.example", "127.0.0.1.evil.example", "", "dev-box.evil"):
        r = client.get("/api/analyses", headers={"host": host})
        assert r.status_code == 400 and r.json()["error"]["code"] == "bad_host", host
    for host in ("127.0.0.1:8766", "localhost", "LOCALHOST:8766", "app.localhost", "192.168.1.5", "[::1]", "[::1]:8766",
                 "dev-box", "DEV-BOX.local:8766", "review.example.com:8766", "other.test"):
        assert client.get("/api/analyses", headers={"host": host}).status_code == 200, host
    monkeypatch.setenv("PR_EXPLORER_ALLOWED_HOSTS", "*")
    assert TestClient(create_app(build_container(home=home))).get("/api/analyses", headers={"host": "evil.example"}).status_code == 200


def test_gzip_workers_and_queue_position(shop_repo, home, monkeypatch):
    import pytest
    from pr_explorer.application.use_cases.queries import AnalysisStatus
    from pr_explorer.interface.http.app import workers_from_env

    client = TestClient(create_app(build_container(home=home)), base_url="http://localhost")
    body = {"local": {"repo_path": str(shop_repo), "base": "main", "head": "feature/discount"}, "semantic_mode": "off"}
    st = _wait(client, client.post("/api/analyses", json=body).json()["analysis_id"])
    r = client.get(f"/api/system-maps/{st['system_map_id']}", headers={"accept-encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip" and r.json()["kind"] == "system_map"  # TestClient inflates it
    assert "content-encoding" not in client.get(f"/api/system-maps/{st['system_map_id']}", headers={"accept-encoding": "identity"}).headers

    monkeypatch.delenv("PR_EXPLORER_WORKERS", raising=False)
    assert workers_from_env() == 2
    monkeypatch.setenv("PR_EXPLORER_WORKERS", "4")
    assert workers_from_env() == 4
    for bad in ("0", "x", "-1", "99"):
        monkeypatch.setenv("PR_EXPLORER_WORKERS", bad)
        with pytest.raises(ValueError):
            workers_from_env()

    class Repo:  # newest first, as the repository returns it
        def list(self, limit):
            return [dict(id=i, status=s, title=None, repo=None, head_sha=None, created_at="", overlay_id=None, system_map_id=None)
                    for i, s in (("c", "queued"), ("b", "queued"), ("a", "running"), ("z", "done"))]
    got = {r["analysis_id"]: r["queue_position"] for r in AnalysisStatus(Repo()).list()}
    assert got == {"c": 2, "b": 1, "a": None, "z": None}


def test_page_is_revalidated_and_hashed_assets_are_immutable(home, tmp_path, monkeypatch):
    """After an upgrade the browser must fetch the new index.html, which names the new hashed bundle."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    (dist / "assets" / "index-abc123.js").write_text("x")
    monkeypatch.setenv("PR_EXPLORER_FRONTEND_DIST", str(dist))
    client = TestClient(create_app(build_container(home=home)), base_url="http://localhost")
    for path in ("/", "/index.html", "/some/route"):
        assert client.get(path).headers["cache-control"] == "no-cache", path
    assert "immutable" in client.get("/assets/index-abc123.js").headers["cache-control"]
