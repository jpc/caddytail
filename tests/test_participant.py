"""Participant tokens: the PASETO v4.public format, key handling and the middleware."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from caddytail import participant as P

# Official PASETO test vector 4-S-1 (paseto-standard/test-vectors, v4.json).
V_SECRET = ("b4cbfb43df4ce210727d953e4a713307fa19bb7d9f85041438d9e11b942a3774"
            "1eb9dbbbbc047c03fd70604e0071f0987e16b28b757225c11f00415d0e20b1a2")
V_PAYLOAD = b'{"data":"this is a signed message","exp":"2022-01-01T00:00:00+00:00"}'
V_TOKEN = ("v4.public.eyJkYXRhIjoidGhpcyBpcyBhIHNpZ25lZCBtZXNzYWdlIiwiZXhwIjoiMjAyMi0wMS0wMVQwMDowMDowMCswMDowMCJ9"
           "bg_XBBzds8lTZShVlwwKSgeKpLT3yukTw6JUz3W4h_ExsQV-P0V54zemZDcAxFaSeef1QlXEFtkqxT1ciiQEDA")


def test_signing_matches_the_official_vector():
    secret = P.SECRET_PREFIX + P._b64(bytes.fromhex(V_SECRET))
    assert P.sign_raw(secret, V_PAYLOAD) == V_TOKEN
    assert P.public_key(secret) == P.PUBLIC_PREFIX + P._b64(bytes.fromhex(V_SECRET[64:]))


@pytest.fixture
def key():
    return P.generate_key()


def test_mint_then_verify(key):
    tok = P.mint(key, {"sub": "p1", "study": "s", "test": False, "iss": "hsv"}, ttl_s=60)
    c = P.verify(tok, [P.public_key(key)])
    assert c["sub"] == "p1" and c["study"] == "s" and c["exp"] > c["iat"]
    assert P.verify(tok, [P.public_key(key)], issuer="hsv")["sub"] == "p1"


def test_refusals(key):
    pub = P.public_key(key)
    tok = P.mint(key, {"sub": "p1", "iss": "hsv", "aud": "app-a"}, ttl_s=60)
    bad = lambda *a, **k: pytest.raises(P.BadToken, P.verify, *a, **k)  # noqa: E731
    bad(tok, [P.public_key(P.generate_key())])                  # another key
    bad(tok, [])                                                # no keys
    bad(tok[:-2] + ("AA" if tok[-2:] != "AA" else "BB"), [pub])  # footer changed
    body = tok.split(".")[2]
    flipped = ("A" if body[10] != "A" else "B")
    bad(".".join(["v4", "public", body[:10] + flipped + body[11:], tok.split(".")[3]]), [pub])
    bad(tok, [pub], issuer="someone-else")
    bad(tok, [pub], audience="app-b")
    bad(tok, [pub], now=datetime.now(timezone.utc) + timedelta(seconds=200))    # expired
    bad(tok, [pub], now=datetime.now(timezone.utc) - timedelta(seconds=200))    # from the future
    bad("v4.local." + body, [pub])
    bad("x" * 5000, [pub])
    # no exp at all, however well signed
    bad(P.sign_raw(key, b'{"sub":"p1"}'), [pub])
    with pytest.raises(ValueError):
        P.mint(key, {"study": "s"}, ttl_s=60)


def test_key_source_fetches_caches_and_picks_up_a_new_key(key):
    k2 = P.generate_key()
    served = {"keys": [P.public_key(key)]}
    calls = []
    src = P.KeySource("https://issuer.test/keys.json", retry_s=0,
                      fetch=lambda url: calls.append(url) or json.dumps(served).encode())
    assert src.keys() == [P.public_key(key)] and src.keys() == [P.public_key(key)]
    assert len(calls) == 1                                     # cached
    served["keys"].append(P.public_key(k2))
    assert P.public_key(k2) in src.keys(P.key_id(P.public_key(k2)))   # unknown key id: fetched again
    served.clear()                                             # the issuer breaks
    src._at = 1e-9
    src.ttl_s = 0
    assert len(src.keys()) == 2                                # what was known is kept
    with pytest.raises(ValueError):
        P.KeySource("http://issuer.test/keys.json")            # plain http refused
    assert P.KeySource(P.public_key(key)).keys() == [P.public_key(key)]


def _wsgi(env_cfg):
    seen = {}

    def app(environ, start_response):
        seen["p"] = P.get_participant(environ)
        seen["environ"] = environ
        return []
    return P.WSGIMiddleware(app, P.Verifier(env_cfg)), seen


def test_wsgi_strips_forged_headers_and_adds_verified_ones(key):
    cfg = {P.ENV_KEYS: P.public_key(key)}
    mw, seen = _wsgi(cfg)
    forged = {"HTTP_PARTICIPANT_ID": "admin", "HTTP_PARTICIPANT_CLAIMS": "{}"}
    mw(dict(forged), None)
    assert seen["p"] is None
    tok = P.mint(key, {"sub": "p1", "kind": "prolific", "study": "s", "cohort": "A", "block": "b1",
                       "test": True, "name": "Zoë"}, ttl_s=60)
    for carrier in ({"HTTP_PARTICIPANT_TOKEN": tok}, {"HTTP_AUTHORIZATION": "Bearer " + tok}):
        mw({**forged, **carrier}, None)
        p = seen["p"]
        assert (p.id, p.kind, p.study, p.cohort, p.block, p.test) == ("p1", "prolific", "s", "A", "b1", True)
        assert p.claims["name"] == "Zoë" and p.expires
    mw({"HTTP_PARTICIPANT_TOKEN": P.mint(P.generate_key(), {"sub": "p1"}, ttl_s=60)}, None)
    assert seen["p"] is None                                   # signed by a stranger
    # with nothing configured, nothing is verified and forged headers still go
    off, seen2 = _wsgi({})
    off({**forged, "HTTP_PARTICIPANT_TOKEN": tok}, None)
    assert seen2["p"] is None
    # another token header can be named; a staff bearer token is left alone
    named, seen3 = _wsgi({**cfg, P.ENV_HEADERS: "X-HSV-Session"})
    named({"HTTP_X_HSV_SESSION": tok, "HTTP_AUTHORIZATION": "Bearer some-cli-token"}, None)
    assert seen3["p"].id == "p1" and seen3["environ"]["HTTP_AUTHORIZATION"] == "Bearer some-cli-token"


def test_asgi(key):
    import asyncio
    got = {}

    async def app(scope, receive, send):
        got["h"] = dict(scope["headers"])
    mw = P.ASGIMiddleware(app, P.Verifier({P.ENV_KEYS: P.public_key(key)}))
    tok = P.mint(key, {"sub": "p9", "study": "s"}, ttl_s=60)
    asyncio.run(mw({"type": "http", "headers": [(b"participant-id", b"admin"),
                                                 (b"participant-token", tok.encode())]}, None, None))
    assert got["h"][b"participant-id"] == b"p9" and got["h"][b"participant-study"] == b"s"
    asyncio.run(mw({"type": "http", "headers": [(b"participant-id", b"admin")]}, None, None))
    assert b"participant-id" not in got["h"]


def test_flask_app_gets_the_middleware(key, monkeypatch):
    flask = pytest.importorskip("flask")
    from caddytail import get_participant
    from caddytail.api import apply_middleware
    monkeypatch.setenv(P.ENV_KEYS, P.public_key(key))
    app = flask.Flask(__name__)

    @app.get("/")
    def index():
        p = get_participant()
        return p.id if p else "nobody"
    apply_middleware(app)
    c = app.test_client()
    assert c.get("/", headers={"Participant-Id": "admin"}).data == b"nobody"
    tok = P.mint(key, {"sub": "p1"}, ttl_s=60)
    assert c.get("/", headers={"Participant-Token": tok}).data == b"p1"
