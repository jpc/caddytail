"""Participant tokens: who a public visitor is, signed by the app that let them in.

Staff reach an app over the tailnet and caddytail says who they are with
``Tailscale-User-*`` headers. A participant (someone recruited for a study, say)
arrives over the public funnel and has no such identity. The app that admits
them, the issuer, signs a short-lived token saying who they are. Any other app
can then recognise them without asking the issuer, and without being able to
mint tokens itself.

The token is PASETO ``v4.public``: an Ed25519 signature (libsodium) over a JSON
payload. There is one algorithm and nothing to configure.

An issuer does this::

    from caddytail import participant
    key = participant.generate_key()              # "k4.secret....": keep it private
    token = participant.mint(key, {"sub": pid, "study": "s1"}, ttl_s=4 * 3600)
    participant.public_key(key)                   # "k4.public....": publish this

and serves ``{"keys": ["k4.public...."]}`` at an https address.

An app behind caddytail sets ``CADDYTAIL_PARTICIPANT_KEYS`` to that address (or
to the ``k4.public...`` keys themselves, comma-separated) and then::

    from caddytail import get_participant
    p = get_participant()        # None, or Participant(id=..., study=..., claims={...})

caddytail verifies the token on each request and passes the result on as
``Participant-*`` request headers. Headers of that name sent by a client are
always removed, whether or not keys are configured, so they can be trusted.

The token is read from ``Participant-Token: <token>`` or
``Authorization: Bearer <token>``. Further header names can be listed in
``CADDYTAIL_PARTICIPANT_HEADERS``.

A token proves who signed it and until when. It cannot be revoked and carries
no state: limits such as "one submission" stay with the issuer.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

HEADER = b"v4.public."
SECRET_PREFIX, PUBLIC_PREFIX, ID_PREFIX = "k4.secret.", "k4.public.", "k4.pid."
MAX_TOKEN = 4096
LEEWAY_S = 30

ENV_KEYS = "CADDYTAIL_PARTICIPANT_KEYS"
ENV_ISSUER = "CADDYTAIL_PARTICIPANT_ISSUER"
ENV_AUDIENCE = "CADDYTAIL_PARTICIPANT_AUDIENCE"
ENV_HEADERS = "CADDYTAIL_PARTICIPANT_HEADERS"

# Request headers an app sees for a verified participant. Claims holds all of them as JSON.
H_ID, H_KIND, H_STUDY = "Participant-Id", "Participant-Kind", "Participant-Study"
H_COHORT, H_BLOCK, H_TEST = "Participant-Cohort", "Participant-Block", "Participant-Test"
H_ISSUER, H_EXPIRES, H_CLAIMS = "Participant-Issuer", "Participant-Expires", "Participant-Claims"
TOKEN_HEADER = "Participant-Token"
_CLAIM_HEADERS = ((H_ID, "sub"), (H_KIND, "kind"), (H_STUDY, "study"), (H_COHORT, "cohort"),
                  (H_BLOCK, "block"), (H_ISSUER, "iss"), (H_EXPIRES, "exp"))


class BadToken(ValueError):
    """The token is malformed, not signed by a known key, or out of date."""


# ── encoding ────────────────────────────────────────────────────────────────

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    if not s or "=" in s:
        raise BadToken("bad encoding")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except Exception:  # noqa: BLE001
        raise BadToken("bad encoding") from None


def _pae(*pieces: bytes) -> bytes:
    """PASETO's pre-authentication encoding: unambiguous concatenation."""
    le64 = lambda n: (n & 0x7FFFFFFFFFFFFFFF).to_bytes(8, "little")  # noqa: E731
    return le64(len(pieces)) + b"".join(le64(len(p)) + p for p in pieces)


# ── Ed25519 (libsodium through PyNaCl; `cryptography` when that is what is installed) ──

def _sign(seed: bytes, msg: bytes) -> bytes:
    try:
        from nacl.signing import SigningKey
    except ImportError:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        return Ed25519PrivateKey.from_private_bytes(seed).sign(msg)
    return SigningKey(seed).sign(msg).signature


def _verify(public: bytes, msg: bytes, sig: bytes) -> bool:
    try:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey
    except ImportError:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(sig, msg)
            return True
        except InvalidSignature:
            return False
    try:
        VerifyKey(public).verify(msg, sig)
        return True
    except BadSignatureError:
        return False


def _public_from_seed(seed: bytes) -> bytes:
    try:
        from nacl.signing import SigningKey
    except ImportError:
        from cryptography.hazmat.primitives import serialization as ser
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
            ser.Encoding.Raw, ser.PublicFormat.Raw)
    return bytes(SigningKey(seed).verify_key)


# ── keys (PASERK text forms) ────────────────────────────────────────────────

def generate_key() -> str:
    """A new signing key, ``k4.secret....``. Whoever holds it can mint participants."""
    seed = os.urandom(32)
    return SECRET_PREFIX + _b64(seed + _public_from_seed(seed))


def _secret_parts(secret: str) -> Tuple[bytes, bytes]:
    if not secret.startswith(SECRET_PREFIX):
        raise ValueError("not a k4.secret key")
    raw = _unb64(secret[len(SECRET_PREFIX):])
    if len(raw) != 64 or _public_from_seed(raw[:32]) != raw[32:]:
        raise ValueError("bad k4.secret key")
    return raw[:32], raw[32:]


def public_key(secret: str) -> str:
    """The ``k4.public....`` key to publish for a signing key."""
    return PUBLIC_PREFIX + _b64(_secret_parts(secret)[1])


def key_id(public: str) -> str:
    """The ``k4.pid....`` name of a public key; tokens carry it so a verifier can pick the key."""
    return ID_PREFIX + _b64(hashlib.blake2b((ID_PREFIX + public).encode(), digest_size=33).digest())


def _public_bytes(public: str) -> bytes:
    if not public.startswith(PUBLIC_PREFIX):
        raise ValueError("not a k4.public key")
    raw = _unb64(public[len(PUBLIC_PREFIX):])
    if len(raw) != 32:
        raise ValueError("bad k4.public key")
    return raw


# ── mint and verify ─────────────────────────────────────────────────────────

def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _when(v: Any) -> datetime:
    if not isinstance(v, str):
        raise BadToken("bad time")
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise BadToken("bad time") from None
    if t.tzinfo is None:
        raise BadToken("bad time")
    return t


def sign_raw(secret: str, payload: bytes, footer: bytes = b"") -> str:
    """A v4.public token over exactly these bytes. `mint` is what an issuer normally calls."""
    seed, _ = _secret_parts(secret)
    sig = _sign(seed, _pae(HEADER, payload, footer, b""))
    return HEADER.decode() + _b64(payload + sig) + ("." + _b64(footer) if footer else "")


def mint(secret: str, claims: Dict[str, Any], *, ttl_s: float,
         now: Optional[datetime] = None) -> str:
    """Sign `claims` for `ttl_s` seconds. ``sub`` (who the participant is) is required;
    ``iat`` and ``exp`` are added. Other claims are the issuer's own: this module passes
    ``kind``, ``study``, ``cohort``, ``block``, ``test`` and ``iss`` on as headers."""
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise ValueError("claims need a sub")
    if ttl_s <= 0:
        raise ValueError("ttl_s must be positive")
    now = now or datetime.now(timezone.utc)
    body = {**claims, "iat": _iso(now), "exp": _iso(now + timedelta(seconds=ttl_s))}
    footer = json.dumps({"kid": key_id(public_key(secret))}, separators=(",", ":")).encode()
    return sign_raw(secret, json.dumps(body, separators=(",", ":")).encode(), footer)


def verify(token: str, keys: Iterable[str], *, issuer: Optional[str] = None,
           audience: Optional[str] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The claims of a token signed by one of `keys` (``k4.public....``) and in date.
    Raises BadToken otherwise. With `issuer` or `audience`, the token's ``iss`` or ``aud``
    must be exactly that."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN or not token.startswith(HEADER.decode()):
        raise BadToken("not a v4.public token")
    parts = token[len(HEADER):].split(".")
    if len(parts) not in (1, 2):
        raise BadToken("not a v4.public token")
    body = _unb64(parts[0])
    footer = _unb64(parts[1]) if len(parts) == 2 else b""
    if len(body) <= 64:
        raise BadToken("not a v4.public token")
    payload, sig = body[:-64], body[-64:]
    kid = None
    if footer:
        try:
            kid = json.loads(footer).get("kid")
        except Exception:  # noqa: BLE001
            raise BadToken("bad footer") from None
    signed = _pae(HEADER, payload, footer, b"")
    for k in keys:
        if kid is not None and key_id(k) != kid:
            continue
        if _verify(_public_bytes(k), signed, sig):
            break
    else:
        raise BadToken("no key signed this")
    try:
        claims = json.loads(payload)
    except Exception:  # noqa: BLE001
        raise BadToken("bad payload") from None
    if not isinstance(claims, dict) or not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise BadToken("no sub")
    now = now or datetime.now(timezone.utc)
    leeway = timedelta(seconds=LEEWAY_S)
    if _when(claims.get("exp")) + leeway < now:        # a token without exp is refused
        raise BadToken("expired")
    for early in ("nbf", "iat"):
        if early in claims and _when(claims[early]) - leeway > now:
            raise BadToken("not valid yet")
    if issuer is not None and claims.get("iss") != issuer:
        raise BadToken("wrong issuer")
    if audience is not None and claims.get("aud") != audience:
        raise BadToken("wrong audience")
    return claims


# ── where a verifier gets its keys ──────────────────────────────────────────

class KeySource:
    """Public keys from a spec: ``k4.public....`` keys and/or https addresses serving
    ``{"keys": ["k4.public....", ...]}``, comma-separated.

    An address is fetched on first use and again every `ttl_s`, or sooner (at most once
    a minute) when a token names a key not yet seen, which is how a new key at the issuer
    is picked up. A failed fetch keeps the keys already known."""

    def __init__(self, spec: str, *, ttl_s: float = 3600, retry_s: float = 60, timeout_s: float = 5,
                 fetch: Optional[Callable[[str], bytes]] = None):
        self.literal: List[str] = []
        self.urls: List[str] = []
        for item in (s.strip() for s in (spec or "").split(",")):
            if not item:
                continue
            if item.startswith(PUBLIC_PREFIX):
                _public_bytes(item)
                self.literal.append(item)
            elif item.startswith("https://") or item.startswith(("http://127.0.0.1", "http://localhost")):
                self.urls.append(item)
            else:
                raise ValueError(f"{ENV_KEYS}: expected a k4.public key or an https address, got {item[:40]!r}")
        self.ttl_s, self.retry_s, self.timeout_s = ttl_s, retry_s, timeout_s
        self._fetch = fetch or self._http
        self._fetched: Dict[str, List[str]] = {}
        self._at = 0.0
        self._lock = threading.Lock()

    def _http(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "caddytail"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:  # noqa: S310  (scheme checked above)
            return r.read(65536)

    def _refresh(self) -> None:
        for url in self.urls:
            try:
                keys = json.loads(self._fetch(url)).get("keys")
                if not isinstance(keys, list) or len(keys) > 16:
                    raise ValueError("no keys list")
                for k in keys:
                    _public_bytes(k)
                self._fetched[url] = list(keys)
            except Exception as e:  # noqa: BLE001  keep what we had
                print(f"caddytail: participant keys from {url} not refreshed: {type(e).__name__}: {e}", flush=True)
        self._at = time.monotonic()

    def keys(self, kid: Optional[str] = None) -> List[str]:
        with self._lock:
            age = time.monotonic() - self._at if self._at else None
            known = self.literal + [k for ks in self._fetched.values() for k in ks]
            unseen = kid is not None and all(key_id(k) != kid for k in known)
            if self.urls and (age is None or age > self.ttl_s or (unseen and age > self.retry_s)):
                self._refresh()
                known = self.literal + [k for ks in self._fetched.values() for k in ks]
            return known


def _kid_of(token: str) -> Optional[str]:
    try:
        return json.loads(_unb64(token.split(".")[3])).get("kid")
    except Exception:  # noqa: BLE001
        return None


# ── what an app sees ────────────────────────────────────────────────────────

@dataclass
class Participant:
    id: str
    kind: str = ""               # how the issuer knows them, e.g. "prolific"
    study: str = ""
    cohort: str = ""
    block: str = ""
    test: bool = False           # a staff member trying the study, not a result
    issuer: str = ""
    expires: str = ""
    claims: Dict[str, Any] = field(default_factory=dict)


def _printable(v: Any) -> Optional[str]:
    s = v if isinstance(v, str) else None
    return s if s is not None and len(s) <= 512 and all(32 <= ord(c) < 127 for c in s) else None


def headers_for(claims: Dict[str, Any]) -> List[Tuple[str, str]]:
    """The request headers that stand for a verified token."""
    out = [(h, s) for h, c in _CLAIM_HEADERS for s in [_printable(claims.get(c))] if s]
    out.append((H_TEST, "1" if claims.get("test") is True else "0"))
    out.append((H_CLAIMS, json.dumps(claims, separators=(",", ":"), ensure_ascii=True)))
    return out


def from_headers(get: Callable[[str], Optional[str]]) -> Optional[Participant]:
    """A Participant from a header lookup function, or None when the request carries none."""
    pid = get(H_ID)
    if not pid:
        return None
    try:
        claims = json.loads(get(H_CLAIMS) or "{}")
    except ValueError:
        claims = {}
    g = lambda h: get(h) or ""  # noqa: E731
    return Participant(id=pid, kind=g(H_KIND), study=g(H_STUDY), cohort=g(H_COHORT), block=g(H_BLOCK),
                       test=get(H_TEST) == "1", issuer=g(H_ISSUER), expires=g(H_EXPIRES),
                       claims=claims if isinstance(claims, dict) else {})


def get_participant(request: Any = None) -> Optional[Participant]:
    """The verified participant making this request, or None.

    Flask: no argument (uses flask.request). FastAPI/Starlette: pass the Request.
    WSGI: pass the environ dict."""
    if isinstance(request, dict):
        return from_headers(lambda h: request.get("HTTP_" + h.upper().replace("-", "_")))
    if request is None:
        from flask import request as request_  # type: ignore
        request = request_
    return from_headers(lambda h: request.headers.get(h))


class Verifier:
    """Reads the token off a request and says which headers to add. Configured from the
    environment; with no keys configured it verifies nothing and adds nothing."""

    def __init__(self, env: Optional[Dict[str, str]] = None):
        env = os.environ if env is None else env
        spec = env.get(ENV_KEYS, "")
        self.source = KeySource(spec) if spec.strip() else None
        self.issuer = env.get(ENV_ISSUER) or None
        self.audience = env.get(ENV_AUDIENCE) or None
        self.token_headers = [TOKEN_HEADER.lower()] + [
            h.strip().lower() for h in env.get(ENV_HEADERS, "").split(",") if h.strip()]

    def headers(self, get: Callable[[str], Optional[str]]) -> List[Tuple[str, str]]:
        """`get` looks a request header up by lower-case name."""
        if self.source is None:
            return []
        token = next((t for t in (get(h) for h in self.token_headers) if t), None)
        if token is None:
            auth = get("authorization") or ""
            if auth[:7].lower() == "bearer " and auth[7:].strip().startswith(HEADER.decode()):
                token = auth[7:]
        if not token:
            return []
        token = token.strip()
        try:
            claims = verify(token, self.source.keys(_kid_of(token)), issuer=self.issuer, audience=self.audience)
        except (BadToken, ValueError):
            return []
        return headers_for(claims)


class WSGIMiddleware:
    """Removes client-sent Participant-* headers and adds the verified ones."""

    def __init__(self, app: Any, verifier: Optional[Verifier] = None):
        self.app, self.verifier = app, verifier or Verifier()

    def __call__(self, environ, start_response):
        for k in [k for k in environ if k.startswith("HTTP_PARTICIPANT_") and k != "HTTP_PARTICIPANT_TOKEN"]:
            del environ[k]
        for h, v in self.verifier.headers(lambda n: environ.get("HTTP_" + n.upper().replace("-", "_"))):
            environ["HTTP_" + h.upper().replace("-", "_")] = v
        return self.app(environ, start_response)


class ASGIMiddleware:
    """The same for ASGI apps (FastAPI, Starlette, plain ASGI callables)."""

    def __init__(self, app: Any, verifier: Optional[Verifier] = None):
        self.app, self.verifier = app, verifier or Verifier()

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            given = [(k, v) for k, v in scope.get("headers") or []]
            kept = [(k, v) for k, v in given
                    if not k.lower().startswith(b"participant-") or k.lower() == b"participant-token"]
            look = {k.lower().decode("latin1"): v.decode("latin1") for k, v in kept}
            added = [(h.lower().encode(), v.encode()) for h, v in self.verifier.headers(look.get)]
            scope = {**scope, "headers": kept + added}
        await self.app(scope, receive, send)
