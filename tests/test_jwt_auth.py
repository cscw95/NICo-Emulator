"""F07 — NICo accepts NOCP OIDC JWTs (Bearer) alongside the API key."""

import time

import pytest

from app import jwt_auth, jwt_lite

P = "/v2/org/neocloud/nico"
SECRET = "shared-dev-secret"
BASE = "http://127.0.0.1:20042"


def _tok(tid=None, roles=("admin",), exp_in=300, iss=None, secret=SECRET, aud=("nocp", "nico")):
    now = int(time.time())
    realm = tid or "neocloud"
    c = {"iss": iss or f"{BASE}/realms/{realm}", "sub": "u1", "aud": list(aud), "exp": now + exp_in,
         "iat": now, "jti": "j", "typ": "Bearer", "azp": f"{realm}-portal", "scope": "openid",
         "realm_access": {"roles": list(roles)}}
    if tid:
        c.update(tid=tid, trole=roles[0])
    return {"Authorization": "Bearer " + jwt_lite.encode(c, secret=secret.encode())}


@pytest.fixture
def jwt_on(monkeypatch):
    monkeypatch.setenv("EMU_JWT_KEY", SECRET)
    monkeypatch.setenv("EMU_JWT_ISSUER", BASE)


def _vpc(client, tenant, headers):
    return client.post(f"{P}/vpc", json={"name": f"vpc-{tenant}", "siteId": "gasan",
                                         "tenantId": tenant}, headers=headers)


def test_off_by_default_jwt_ignored(client):
    # not configured: a JWT bearer is just another bearer (no API key set → allowed)
    assert client.get(f"{P}/vpc", headers=_tok("t-a")).status_code == 200


def test_expired_bad_sig_wrong_issuer_401(client, jwt_on):
    for h in (_tok("t-a", exp_in=-120), _tok("t-a", secret="other"),
              _tok("t-a", iss="https://evil.example/realms/t-a"), _tok("t-a", aud=("nocp",))):
        r = client.get(f"{P}/vpc", headers=h)
        assert r.status_code == 401 and "invalid_token" in r.headers["www-authenticate"], r.text


def test_jwt_satisfies_api_key_and_hmac_key_still_works(client, jwt_on, monkeypatch):
    monkeypatch.setenv("EMU_API_KEY", "k-123")
    assert _vpc(client, "t-a", {}).status_code == 401
    assert _vpc(client, "t-a", {"X-API-Key": "k-123"}).status_code == 201
    assert _vpc(client, "t-b", {"Authorization": "Bearer k-123"}).status_code == 201
    assert _vpc(client, "t-c", _tok(roles=("operator",))).status_code == 201
    assert _vpc(client, "t-d", _tok("t-d")).status_code == 201


def test_tenant_scope_403(client, jwt_on):
    a, b = _tok("t-a"), _tok("t-b")
    va = _vpc(client, "t-a", a).json()
    assert _vpc(client, "t-a", b).status_code == 403                 # forged tenantId
    assert client.get(f"{P}/vpc/{va['id']}", headers=b).status_code == 403
    assert client.delete(f"{P}/vpc/{va['id']}", headers=b).status_code == 403
    assert client.get(f"{P}/vpc/{va['id']}", headers=a).status_code == 200
    _vpc(client, "t-b", b)
    assert [v["tenantId"] for v in client.get(f"{P}/vpc", headers=a).json()] == ["t-a"]
    assert len(client.get(f"{P}/vpc", headers=_tok(roles=("operator",))).json()) == 2
    # provider surfaces and bridge are closed to tenant tokens
    assert client.get(f"{P}/machine", headers=a).status_code == 403
    assert client.post(f"{P}/expected-machine", json={}, headers=a).status_code == 403
    assert client.get("/nico-bridge/hosts", headers=a).status_code == 403
    # tenantId defaults to the token's tenant
    r = client.post(f"{P}/vpc", json={"name": "v2", "siteId": "gasan"}, headers=a)
    assert r.status_code == 201 and r.json()["tenantId"] == "t-a"


def test_rs256_via_jwks_url(client, monkeypatch):
    key = jwt_lite.RSAKey.generate(1024)
    monkeypatch.setenv("EMU_JWT_JWKS_URL", "http://127.0.0.1:1/certs")
    monkeypatch.setattr(jwt_auth, "_jwks_keys", lambda: [jwt_lite.RSAKey.from_jwk(key.public_jwk())])
    now = int(time.time())
    c = {"iss": f"{BASE}/realms/t-a", "sub": "u", "aud": ["nico"], "exp": now + 60, "iat": now,
         "typ": "Bearer", "tid": "t-a", "trole": "admin", "realm_access": {"roles": ["admin"]}}
    h = {"Authorization": "Bearer " + jwt_lite.encode(c, alg="RS256", rsa=key)}
    assert _vpc(client, "t-a", h).status_code == 201
    h2 = {"Authorization": "Bearer " + jwt_lite.encode(c, alg="RS256", rsa=jwt_lite.RSAKey.generate(1024))}
    assert client.get(f"{P}/vpc", headers=h2).status_code == 401


def test_jwt_lite_identical_to_nocp_copy():
    from pathlib import Path
    here = Path(__file__).resolve().parents[1] / "app" / "jwt_lite.py"
    nocp = here.parents[2] / "NeoCloud-Control-Plane" / "app" / "jwt_lite.py"
    if not nocp.exists():
        pytest.skip("NOCP checkout not beside this repo")
    assert here.read_bytes() == nocp.read_bytes()
