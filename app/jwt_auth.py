"""F07 — accept NOCP-issued OIDC JWTs (`Authorization: Bearer <JWT>`) next to the
existing API key (app/security.py, unchanged and byte-identical with AI-Infra).

Off unless EMU_JWT_KEY (HS256 shared secret = NOCP_JWT_SECRET) or EMU_JWT_JWKS_URL
(RS256; NOCP `/realms/{realm}/protocol/openid-connect/certs`) is set.
  EMU_JWT_ISSUER    exact `iss`, or the NOCP base URL: then any
                    `<base>/realms/<realm>` issuer is accepted (one realm per tenant)
  EMU_JWT_AUDIENCE  required `aud` (default "nico"; empty = not checked)

Verification follows RFC 7519 §7.2 / Keycloak TokenVerifier (signature, exp, iss,
aud). A bearer that is not a JWT goes on to the API-key check untouched.

Valid JWT:
  - satisfies EMU_API_KEY (the key is injected for app.security downstream)
  - operator token (realm_access.roles has "operator", no `tid`): full access
  - tenant token (`tid` claim): X-Tenant-Id is bound to `tid`; writes only on
    tenant resources (infra-controller rest-api tenant objects) with a matching
    `tenantId`; another tenant's resource → 403; provider surfaces → 403; list
    responses keep only the tenant's items.
Invalid or expired JWT → 401 `{"detail"}` + `WWW-Authenticate: Bearer error="invalid_token"`
(RFC 6750 §3).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from . import jwt_lite

_REST_RE = re.compile(r"^/v2/org/[^/]+/nico(?:/(?P<rest>.*))?$")
# infra-controller rest-api objects a tenant owns (tenantId)
TENANT_WRITABLE = frozenset({"allocation", "instance", "vpc", "subnet", "sshkey", "sshkeygroup",
                             "network-security-group", "infiniband-partition",
                             "nvlink-logical-partition", "spectrumx-partition",
                             "operating-system"})
# provider (infrastructure) surfaces closed to tenant tokens
PROVIDER_ONLY = frozenset({"machine", "expected-machine", "expected-power-shelf", "expected-rack",
                           "rack", "measured-boot", "power-shelf", "switch", "firmware"})
_OPEN_PATHS = ("/healthz", "/docs", "/openapi.json")
_jwks_cache: dict = {"at": 0.0, "url": None, "keys": []}


def enabled() -> bool:
    return bool(os.environ.get("EMU_JWT_KEY") or os.environ.get("EMU_JWT_JWKS_URL"))


def _issuer_ok(iss) -> bool:
    want = os.environ.get("EMU_JWT_ISSUER", "").rstrip("/")
    if not want:
        return True
    if not isinstance(iss, str):
        return False
    return iss == want or re.fullmatch(re.escape(want) + r"/realms/[^/]+", iss) is not None


def _jwks_keys() -> list:
    url = os.environ.get("EMU_JWT_JWKS_URL")
    if not url:
        return []
    c = _jwks_cache
    if c["url"] == url and time.time() - c["at"] < 300:
        return c["keys"]
    from .security import check_outbound_url, UnsafeURL
    try:
        check_outbound_url(url)
        with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310 (allowlisted)
            data = json.loads(r.read())
        keys = [jwt_lite.RSAKey.from_jwk(k) for k in data.get("keys", []) if k.get("kty") == "RSA"]
    except (UnsafeURL, OSError, ValueError, KeyError):
        keys = c["keys"] if c["url"] == url else []
    c.update(at=time.time(), url=url, keys=keys)
    return keys


def verify(token: str) -> dict:
    aud = os.environ.get("EMU_JWT_AUDIENCE", "nico") or None
    secret = os.environ.get("EMU_JWT_KEY", "").encode()
    claims = jwt_lite.decode(token, secret=secret, keys=_jwks_keys(),
                             issuer=_issuer_ok, audience=aud)
    if claims.get("typ", "Bearer") != "Bearer":
        raise jwt_lite.JWTError("typ", "not an access token")
    return claims


def is_operator(claims: dict) -> bool:
    roles = (claims.get("realm_access") or {}).get("roles") or []
    return not claims.get("tid") and "operator" in roles


def _owner(oid: str) -> Optional[str]:
    from .rest.common import OBJ
    for coll in OBJ.values():
        o = coll.get(oid)
        if isinstance(o, dict) and "tenantId" in o:
            return o.get("tenantId") or None
    from . import bridge
    for h in bridge._hosts.values():
        if h.get("instance_id") == oid:
            return h.get("tenant_id") or h.get("tenant_ref")
    return None


def _deny(status: int, msg: str) -> JSONResponse:
    hdr = {"WWW-Authenticate": 'Bearer error="invalid_token"'} if status == 401 else {}
    return JSONResponse({"detail": msg}, status_code=status, headers=hdr)


def _set_header(request, name: bytes, value: Optional[str]) -> None:
    hdrs = [(k, v) for k, v in request.scope["headers"] if k != name]
    if value is not None:
        hdrs.append((name, value.encode()))
    request.scope["headers"] = hdrs


class JwtAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        auth = request.headers.get("authorization", "")
        if not enabled() or not auth.lower().startswith("bearer "):
            return await call_next(request)
        tok = auth[7:].strip()
        if not jwt_lite.looks_like_jwt(tok):
            return await call_next(request)            # API key path (app.security)
        try:
            claims = verify(tok)
        except jwt_lite.JWTError as e:
            return _deny(401, f"invalid token: {e}")
        request.state.jwt = claims
        key = os.environ.get("EMU_API_KEY")
        if key:
            _set_header(request, b"x-api-key", key)
        if is_operator(claims):
            return await call_next(request)
        tid = claims.get("tid")
        if not tid:
            return _deny(403, "token carries neither a tenant nor the operator role")
        _set_header(request, b"x-tenant-id", tid)
        path = request.scope.get("path", "")
        method = request.method.upper()
        if path.startswith(_OPEN_PATHS) or method == "OPTIONS":
            return await call_next(request)
        m = _REST_RE.match(path)
        if not m:
            return _deny(403, "surface not allowed for tenant scope")
        segs = [s for s in (m.group("rest") or "").split("/") if s]
        head = segs[0] if segs else ""
        if head in PROVIDER_ONLY:
            return _deny(403, "provider surface not allowed for tenant scope")
        for s in segs[1:]:
            owner = _owner(s)
            if owner is not None and owner != tid:
                return _deny(403, "tenant scope violation")
        if method not in ("GET", "HEAD"):
            if head not in TENANT_WRITABLE and head != "tenant":
                return _deny(403, "read-only for tenant scope")
            body = await request.body()
            try:
                data = json.loads(body) if body else None
            except (ValueError, UnicodeDecodeError):
                data = None
            if isinstance(data, dict) and data.get("tenantId") not in (None, tid):
                return _deny(403, "tenant scope violation")
        response = await call_next(request)
        if method == "GET" and response.status_code == 200 and \
                "json" in response.headers.get("content-type", ""):
            raw = b"".join([c async for c in response.body_iterator])
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("tenantId") not in (None, tid):
                return _deny(403, "tenant scope violation")
            if isinstance(data, list):
                data = [x for x in data if not (isinstance(x, dict)
                                                and x.get("tenantId") not in (None, tid))]
                raw = json.dumps(data).encode()
            headers = dict(response.headers)
            headers.pop("content-length", None)
            return Response(raw, status_code=200, headers=headers, media_type="application/json")
        return response
