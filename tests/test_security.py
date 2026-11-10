"""Security controls: SSRF guard on AI_INFRA_URL, path-segment quoting,
optional API key, CORS lock, request limits and object caps."""
import pytest

from app import aiinfra, security
from app.rest import common

P = "/v2/org/neocloud/nico"


def test_ai_infra_url_must_be_allowlisted(monkeypatch):
    for bad in ("http://169.254.169.254", "file:///etc", "http://10.2.3.4:9100"):
        monkeypatch.setattr(aiinfra, "AI_INFRA_URL", bad)
        with pytest.raises(aiinfra.AIInfraError, match="rejected"):
            aiinfra._request("GET", "/healthz")
        assert aiinfra.ping()["reachable"] is False


def test_ai_infra_allowlist_env(monkeypatch):
    monkeypatch.setenv("EMU_OUTBOUND_ALLOWLIST", "ai-infra.internal,10.0.0.0/8")
    assert security.check_outbound_url("http://10.2.3.4:9100/healthz")
    with pytest.raises(security.UnsafeURL):
        security.check_outbound_url("http://127.0.0.1:9100/")


def test_path_segments_are_quoted(monkeypatch):
    seen = []

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, **kw):
            seen.append((url, kw.get("headers")))
            raise aiinfra.httpx.ConnectError("x")

    monkeypatch.setattr(aiinfra.httpx, "Client", FakeClient)
    monkeypatch.setenv("AI_INFRA_API_KEY", "peer")
    with pytest.raises(aiinfra.AIInfraError):
        aiinfra.get_rack("../../emulator/v1/reset?x=")
    url, headers = seen[0]
    assert url.endswith("/racks/..%2F..%2Femulator%2Fv1%2Freset%3Fx%3D")
    assert headers["X-API-Key"] == "peer"          # peer auth forwarded


def test_api_key_optional(client, monkeypatch):
    body = {"name": "v1", "siteId": "x"}
    assert client.post(f"{P}/vpc", json=body).status_code != 401
    monkeypatch.setenv("EMU_API_KEY", "k")
    assert client.post(f"{P}/vpc", json=body).status_code == 401
    assert client.post(f"{P}/vpc", json=body,
                       headers={"Authorization": "Bearer k"}).status_code != 401
    assert client.post("/emulator/v1/reset").status_code == 401
    assert client.get("/versions").status_code == 200


def test_cors_locked(client):
    pre = {"Access-Control-Request-Method": "POST"}
    ok = client.options("/versions", headers={"Origin": "http://localhost:8000", **pre})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:8000"
    bad = client.options("/versions", headers={"Origin": "http://evil.example", **pre})
    assert "access-control-allow-origin" not in bad.headers


def test_limits(client, monkeypatch):
    assert client.get("/emulator/v1/events?limit=-1").status_code == 422
    assert client.get(f"{P}/vpc?pageSize=99999999").status_code == 422
    big = "x" * (1024 * 1024 + 1)
    assert client.post(f"{P}/vpc", content=big,
                       headers={"Content-Type": "application/json"}).status_code == 413
    monkeypatch.setattr(common, "MAX_OBJECTS", 0)
    with pytest.raises(Exception):
        common.new_obj("vpc", {"name": "x"})
