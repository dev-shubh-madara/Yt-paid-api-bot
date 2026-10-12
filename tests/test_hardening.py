import base64
import json
import time
from contextlib import asynccontextmanager

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.clerk_auth import canonical_gmail, domain_from_publishable_key
from app.main import create_app

from .conftest import FakeExtractor, make_settings

DOMAIN = "clerk.test.dev"
ISS = f"https://{DOMAIN}"
PK = "pk_test_" + base64.b64encode(f"{DOMAIN}$".encode()).decode()


def _keypair():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption())
    return pem, k.public_key()


PRIV, PUB = _keypair()
OTHER_PRIV, _ = _keypair()


def google_user(email, uid="user_1", **over):
    u = {
        "id": uid, "banned": False, "locked": False, "primary_email_address_id": "idn_1",
        "email_addresses": [{"id": "idn_1", "email_address": email, "verification": {"status": "verified"}}],
        "external_accounts": [{"provider": "oauth_google", "email_address": email,
                               "verification": {"status": "verified"}}],
    }
    u.update(over)
    return u


class FakeClerk:
    def __init__(self):
        self.users = {}
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(PUB))
        jwk.update(kid="k1", alg="RS256", use="sig")
        self.jwks = {"keys": [jwk]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json=self.jwks)
        if request.url.host == "api.clerk.com" and request.url.path.startswith("/v1/users/"):
            assert request.headers["authorization"] == "Bearer sk_test_secret"
            u = self.users.get(request.url.path.rsplit("/", 1)[1])
            return httpx.Response(200, json=u) if u else httpx.Response(404, json={})
        return httpx.Response(500)


def token(sub="user_1", *, key=PRIV, exp=300, iss=ISS, azp=None, alg="RS256", extra=None):
    now = int(time.time())
    claims = {"sub": sub, "iss": iss, "iat": now - 1, "exp": now + exp, "nbf": now - 1}
    if azp:
        claims["azp"] = azp
    claims.update(extra or {})
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": "k1"})


@pytest.fixture
def clerk_env(tmp_path, media_server):
    fake = FakeClerk()

    @asynccontextmanager
    async def _make(peer="203.0.113.7", **over):
        base = dict(clerk_publishable_key=PK, clerk_secret_key="sk_test_secret")
        base.update(over)
        settings = make_settings(tmp_path, **base)
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            app.state.extractor.shutdown()
            app.state.extractor = FakeExtractor(settings, media_server.port)
            app.state.clerk.http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
            transport = httpx.ASGITransport(app=app, client=(peer, 5555))
            async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
                yield c, app

    _make.fake = fake
    return _make


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


# ── pure helpers ──────────────────────────────────────────────────────────
def test_gmail_canonicalisation():
    assert canonical_gmail("J.o.h.n+spam@GMail.com") == "john@gmail.com"
    assert canonical_gmail("john@googlemail.com") == "john@gmail.com"
    assert canonical_gmail("john@corp.com") is None
    assert canonical_gmail("+x@gmail.com") is None
    assert canonical_gmail("a@b@gmail.com") is None


def test_publishable_key_domain():
    assert domain_from_publishable_key(PK) == DOMAIN
    assert domain_from_publishable_key("garbage") == ""


# ── Google login: one key per Google account ──────────────────────────────
async def test_google_login_issues_one_key_per_account(clerk_env):
    clerk_env.fake.users["user_1"] = google_user("john@gmail.com")
    async with clerk_env() as (c, app):
        r = await c.post("/v1/keys", headers=bearer(token()))
        assert r.status_code == 201, r.text
        assert r.json()["owner"] == "g:john@gmail.com" and r.json()["daily_limit"] == 1000
        key = r.json()["api_key"]
        assert (await c.get("/v1/me", headers={"X-API-Key": key})).status_code == 200
        again = await c.post("/v1/keys", headers=bearer(token()))
        assert again.status_code == 409 and again.json()["error"] == "key_exists"


async def test_gmail_aliases_and_second_clerk_user_cannot_farm_keys(clerk_env):
    clerk_env.fake.users["user_1"] = google_user("john@gmail.com", "user_1")
    clerk_env.fake.users["user_2"] = google_user("j.o.h.n+farm@gmail.com", "user_2")
    async with clerk_env() as (c, app):
        assert (await c.post("/v1/keys", headers=bearer(token("user_1")))).status_code == 201
        r = await c.post("/v1/keys", headers=bearer(token("user_2")))
        assert r.status_code == 409 and r.json()["error"] == "key_exists"


async def test_non_google_and_non_gmail_rejected(clerk_env):
    fake = clerk_env.fake
    no_google = google_user("a@gmail.com", "u_pw", external_accounts=[])
    unverified = google_user("b@gmail.com", "u_unv")
    unverified["email_addresses"][0]["verification"] = {"status": "unverified"}
    corp = google_user("boss@corp.com", "u_corp")
    banned = google_user("c@gmail.com", "u_ban", banned=True)
    fake.users.update({"u_pw": no_google, "u_unv": unverified, "u_corp": corp, "u_ban": banned})
    async with clerk_env() as (c, _):
        for uid, status, err in [("u_pw", 403, "google_required"), ("u_unv", 403, "google_required"),
                                 ("u_corp", 403, "gmail_required"), ("u_ban", 403, "account_blocked"),
                                 ("u_missing", 401, "invalid_login")]:
            r = await c.post("/v1/keys", headers=bearer(token(uid)))
            assert (r.status_code, r.json()["error"]) == (status, err), uid


async def test_forged_or_bad_tokens_rejected(clerk_env):
    clerk_env.fake.users["user_1"] = google_user("john@gmail.com")
    bad = {
        "wrong key": token(key=OTHER_PRIV),
        "expired": token(exp=-100),
        "wrong issuer": token(iss="https://evil.example"),
        "wrong origin": token(azp="https://evil.example"),
        "pending": token(extra={"sts": "pending"}),
        "hs256": jwt.encode({"sub": "user_1", "iss": ISS, "iat": int(time.time()), "exp": int(time.time()) + 99},
                            "x" * 40, algorithm="HS256", headers={"kid": "k1"}),
        "garbage": "not.a.jwt",
    }
    async with clerk_env(clerk_authorized_parties="https://yt.example.com") as (c, _):
        for name, t in bad.items():
            r = await c.post("/v1/keys", headers=bearer(t))
            assert r.status_code == 401 and r.json()["error"] == "invalid_login", name
        ok = await c.post("/v1/keys", headers=bearer(token(azp="https://yt.example.com")))
        assert ok.status_code == 201


async def test_unsigned_alg_none_rejected(clerk_env):
    clerk_env.fake.users["user_1"] = google_user("john@gmail.com")
    h = base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": "k1"}).encode()).rstrip(b"=").decode()
    p = base64.urlsafe_b64encode(json.dumps({"sub": "user_1", "iss": ISS, "exp": int(time.time()) + 99,
                                             "iat": int(time.time())}).encode()).rstrip(b"=").decode()
    async with clerk_env() as (c, _):
        r = await c.post("/v1/keys", headers=bearer(f"{h}.{p}."))
        assert r.status_code == 401


async def test_no_token_and_owner_in_body_do_not_bypass_login(clerk_env):
    async with clerk_env() as (c, _):
        r = await c.post("/v1/keys", json={"owner": "freeloader"})
        assert r.status_code == 401 and r.json()["error"] == "login_required"


async def test_server_side_secret_still_works_when_login_enabled(clerk_env):
    async with clerk_env(key_creation_secret="s3cret") as (c, _):
        r = await c.post("/v1/keys", json={"owner": "tg:12345"}, headers={"X-Creation-Secret": "s3cret"})
        assert r.status_code == 201
        r = await c.post("/v1/keys", json={"owner": "tg:999"}, headers={"X-Creation-Secret": "wrong"})
        assert r.status_code == 401  # wrong secret -> falls to login path


async def test_rotate_and_revoked_account_stays_blocked(clerk_env):
    clerk_env.fake.users["user_1"] = google_user("john@gmail.com")
    async with clerk_env() as (c, app):
        r = await c.post("/v1/keys/rotate", headers=bearer(token()))
        assert r.status_code == 404 and r.json()["error"] == "no_key"
        old = (await c.post("/v1/keys", headers=bearer(token()))).json()["api_key"]
        new = (await c.post("/v1/keys/rotate", headers=bearer(token()))).json()["api_key"]
        assert new != old
        assert (await c.get("/v1/me", headers={"X-API-Key": old})).status_code == 401
        assert (await c.get("/v1/me", headers={"X-API-Key": new})).status_code == 200
        kid = (await app.state.keys.lookup(new)).id
        await c.post(f"/admin/keys/{kid}/revoke", headers={"X-Admin-Token": "admintoken"})
        r = await c.post("/v1/keys", headers=bearer(token()))
        assert r.status_code == 403 and r.json()["error"] == "key_revoked"


async def test_portal_page(clerk_env):
    async with clerk_env() as (c, _):
        r = await c.get("/portal")
        assert r.status_code == 200 and DOMAIN in r.text and PK in r.text
        assert DOMAIN in r.headers["content-security-policy"]
        assert (await c.get("/portal/portal.js")).status_code == 200
    async with clerk_env(clerk_publishable_key="", clerk_secret_key="") as (c, _):
        assert (await c.get("/portal")).status_code == 404


# ── network hardening ─────────────────────────────────────────────────────
async def test_spoofed_ip_headers_are_ignored_from_untrusted_peers(clerk_env):
    async with clerk_env(clerk_publishable_key="", clerk_secret_key="",
                         key_creations_per_ip_per_hour=2) as (c, _):
        for i in range(2):
            r = await c.post("/v1/keys", json={"owner": f"owner{i}"},
                             headers={"CF-Connecting-IP": f"9.9.9.{i}", "X-Forwarded-For": f"8.8.8.{i}"})
            assert r.status_code == 201
        r = await c.post("/v1/keys", json={"owner": "owner9"}, headers={"CF-Connecting-IP": "9.9.9.99"})
        assert r.status_code == 429  # same real peer, the fake header changed nothing


async def test_cloudflare_peer_is_believed(clerk_env):
    async with clerk_env(peer="173.245.48.9", trust_cloudflare=True, clerk_publishable_key="",
                         clerk_secret_key="", key_creations_per_ip_per_hour=1) as (c, _):
        a = await c.post("/v1/keys", json={"owner": "owner1"}, headers={"CF-Connecting-IP": "9.9.9.1"})
        b = await c.post("/v1/keys", json={"owner": "owner2"}, headers={"CF-Connecting-IP": "9.9.9.2"})
        c2 = await c.post("/v1/keys", json={"owner": "owner3"}, headers={"CF-Connecting-IP": "9.9.9.1"})
        assert (a.status_code, b.status_code, c2.status_code) == (201, 201, 429)


async def test_origin_lock(clerk_env):
    async with clerk_env(peer="203.0.113.7", trust_cloudflare=True, require_trusted_proxy=True) as (c, _):
        assert (await c.get("/health")).status_code == 403
    async with clerk_env(peer="173.245.48.9", trust_cloudflare=True, require_trusted_proxy=True) as (c, _):
        assert (await c.get("/health")).status_code == 200
    async with clerk_env(peer="127.0.0.1", trusted_proxies="127.0.0.1/32", require_trusted_proxy=True) as (c, _):
        assert (await c.get("/health")).status_code == 200


async def test_host_allowlist_and_admin_ip_allowlist(clerk_env):
    async with clerk_env(allowed_hosts="yt.example.com") as (c, _):
        assert (await c.get("/health")).status_code == 400
        assert (await c.get("/health", headers={"Host": "yt.example.com"})).status_code == 200
    async with clerk_env(admin_ip_allowlist="198.51.100.0/24") as (c, _):
        r = await c.get("/admin/keys", headers={"X-Admin-Token": "admintoken"})
        assert r.status_code == 403
    async with clerk_env(peer="198.51.100.4", admin_ip_allowlist="198.51.100.0/24") as (c, _):
        assert (await c.get("/admin/keys", headers={"X-Admin-Token": "admintoken"})).status_code == 200


async def test_size_limits_headers_docs_and_anon_limit(clerk_env):
    async with clerk_env(anon_rate_limit_per_minute=5) as (c, _):
        r = await c.post("/v1/keys", content=b"x" * 20000, headers={"Content-Type": "application/json"})
        assert r.status_code == 413
        h = (await c.get("/health")).headers
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
        assert "default-src 'none'" in h["content-security-policy"] and h["cache-control"] == "no-store"
        assert "max-age" in h["strict-transport-security"]
        for p in ("/docs", "/redoc", "/openapi.json"):
            assert (await c.get(p)).status_code == 404
        codes = [(await c.get("/v1/me")).status_code for _ in range(7)]
        assert codes[-1] == 429 and codes[0] == 401
        # keyed callers are not hit by the anonymous limit
        assert (await c.get("/v1/me", headers={"X-API-Key": "myk_x"})).status_code == 401
    async with clerk_env(expose_docs=True) as (c, _):
        assert (await c.get("/docs")).status_code == 200
