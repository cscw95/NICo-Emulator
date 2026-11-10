"""Minimal JWS/JWT (RFC 7515 / 7519) and JWK (RFC 7517) — stdlib only.

Shared byte-identical by NOCP (`app/jwt_lite.py`, issuer) and NICo-Emulator
(`app/jwt_lite.py`, verifier); keep the two copies identical.

Algorithms: HS256 (shared secret, dev default) and RS256 (RSASSA-PKCS1-v1_5 +
SHA-256, RFC 7518 §3.3). RSA keys are generated in pure Python (Miller-Rabin);
this is an emulator, not a hardened crypto library — swap for PyJWT/cryptography
at the adapter boundary in production.

Claim checks follow RFC 7519 §4.1 (exp/nbf with leeway, iss, aud) the way
Keycloak's `TokenVerifier` applies them (keycloak/keycloak e514445f9dcd,
core/src/main/java/org/keycloak/TokenVerifier.java).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Optional

# DER DigestInfo prefix for SHA-256 (RFC 8017 §9.2 note 1)
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
LEEWAY_S = 30


class JWTError(Exception):
    """Token rejected; `reason` is a short machine-readable code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _int_b64(n: int) -> str:
    return b64e(n.to_bytes((n.bit_length() + 7) // 8, "big"))


def _b64_int(s: str) -> int:
    return int.from_bytes(b64d(s), "big")


# ── RSA (pure Python) ──────────────────────────────────────────────────────

def _probable_prime(n: int, rounds: int = 24) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int) -> int:
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _probable_prime(c):
            return c


class RSAKey:
    """RSA key pair; `d` is None for a public-only key (from a JWK)."""

    def __init__(self, n: int, e: int, d: Optional[int] = None, kid: str = ""):
        self.n, self.e, self.d = n, e, d
        self.kid = kid or b64e(hashlib.sha256(_int_b64(n).encode()).digest())[:16]

    @classmethod
    def generate(cls, bits: int = 2048, kid: str = "") -> "RSAKey":
        e = 65537
        while True:
            p, q = _prime(bits // 2), _prime(bits // 2)
            if p == q:
                continue
            phi = (p - 1) * (q - 1)
            try:
                d = pow(e, -1, phi)
            except ValueError:
                continue
            return cls(p * q, e, d, kid)

    @classmethod
    def from_jwk(cls, jwk: dict) -> "RSAKey":
        return cls(_b64_int(jwk["n"]), _b64_int(jwk["e"]), kid=jwk.get("kid", ""))

    @property
    def size(self) -> int:
        return (self.n.bit_length() + 7) // 8

    def public_jwk(self) -> dict:
        # Keycloak JWKS key shape (`GET /realms/{realm}/protocol/openid-connect/certs`)
        return {"kid": self.kid, "kty": "RSA", "alg": "RS256", "use": "sig",
                "n": _int_b64(self.n), "e": _int_b64(self.e)}

    def _em(self, msg: bytes) -> int:
        t = _SHA256_PREFIX + hashlib.sha256(msg).digest()
        pad = self.size - len(t) - 3
        return int.from_bytes(b"\x00\x01" + b"\xff" * pad + b"\x00" + t, "big")

    def sign(self, msg: bytes) -> bytes:
        if self.d is None:
            raise ValueError("public key cannot sign")
        return pow(self._em(msg), self.d, self.n).to_bytes(self.size, "big")

    def verify(self, msg: bytes, sig: bytes) -> bool:
        if len(sig) != self.size:
            return False
        return hmac.compare_digest(
            pow(int.from_bytes(sig, "big"), self.e, self.n).to_bytes(self.size, "big"),
            self._em(msg).to_bytes(self.size, "big"))


# ── JWS compact ────────────────────────────────────────────────────────────

def encode(claims: dict, *, alg: str = "HS256", secret: bytes = b"",
           rsa: Optional[RSAKey] = None, kid: Optional[str] = None,
           typ: str = "JWT") -> str:
    header: dict[str, Any] = {"alg": alg, "typ": typ}
    if alg == "RS256":
        if rsa is None:
            raise ValueError("RS256 needs an RSA key")
        header["kid"] = kid or rsa.kid
    elif alg == "HS256":
        if not secret:
            raise ValueError("HS256 needs a secret")
        if kid:
            header["kid"] = kid
    else:
        raise ValueError(f"unsupported alg {alg}")
    h = b64e(json.dumps(header, separators=(",", ":")).encode())
    p = b64e(json.dumps(claims, separators=(",", ":")).encode())
    signing = f"{h}.{p}".encode()
    sig = (hmac.new(secret, signing, hashlib.sha256).digest() if alg == "HS256"
           else rsa.sign(signing))
    return f"{h}.{p}.{b64e(sig)}"


def unverified(token: str) -> tuple[dict, dict]:
    """(header, claims) without checking anything — for routing only."""
    try:
        h, p, _ = token.split(".")
        return json.loads(b64d(h)), json.loads(b64d(p))
    except (ValueError, json.JSONDecodeError) as e:
        raise JWTError("malformed", "not a compact JWS") from e


def looks_like_jwt(token: str) -> bool:
    if token.count(".") != 2 or token.startswith("nocp1."):
        return False
    try:
        h, _ = unverified(token)
    except JWTError:
        return False
    return isinstance(h, dict) and h.get("alg") in ("HS256", "RS256")


def decode(token: str, *, secret: bytes = b"", keys: Optional[list] = None,
           issuer: Any = None, audience: Optional[str] = None,
           algorithms: tuple = ("HS256", "RS256"), now: Optional[float] = None,
           leeway: int = LEEWAY_S) -> dict:
    """Verify signature + registered claims; return the claims or raise JWTError.

    `keys`: RSAKey list (JWKS). `issuer`: exact string, a tuple of accepted
    strings, or a callable(iss) -> bool."""
    header, claims = unverified(token)
    alg = header.get("alg")
    if alg not in algorithms:
        raise JWTError("alg", f"algorithm {alg!r} not accepted")
    h, p, s = token.split(".")
    signing, sig = f"{h}.{p}".encode(), b64d(s)
    if alg == "HS256":
        if not secret or not hmac.compare_digest(
                hmac.new(secret, signing, hashlib.sha256).digest(), sig):
            raise JWTError("signature", "signature check failed")
    else:
        cands = [k for k in (keys or []) if not header.get("kid") or k.kid == header["kid"]]
        if not any(k.verify(signing, sig) for k in cands):
            raise JWTError("signature", "signature check failed")
    if not isinstance(claims, dict):
        raise JWTError("malformed", "claims must be an object")
    t = time.time() if now is None else now
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp + leeway < t:
        raise JWTError("expired", "token is expired")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and nbf - leeway > t:
        raise JWTError("nbf", "token not yet valid")
    if issuer is not None:
        iss = claims.get("iss")
        ok = (issuer(iss) if callable(issuer)
              else iss in issuer if isinstance(issuer, (tuple, list, set))
              else iss == issuer)
        if not ok:
            raise JWTError("issuer", f"invalid issuer {iss!r}")
    if audience is not None:
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if audience not in auds and claims.get("azp") != audience:
            raise JWTError("audience", "invalid audience")
    return claims
