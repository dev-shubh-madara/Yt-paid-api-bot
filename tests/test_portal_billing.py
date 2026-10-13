"""Buying a plan from the web page: Google-login protected order endpoints, QR code, live status."""

import re
from urllib.parse import parse_qs, urlparse

from .conftest import build_email
from .test_hardening import PRIV, bearer, clerk_env, google_user, token  # noqa: F401


async def login(clerk_env, c, app, email="buyer@gmail.com", uid="user_1"):
    clerk_env.fake.users[uid] = google_user(email, uid)
    h = bearer(token(uid))
    r = await c.post("/v1/keys", headers=h)
    assert r.status_code == 201, r.text
    return h, r.json()["api_key"]


async def test_login_is_required_for_every_portal_endpoint(clerk_env):
    async with clerk_env() as (c, app):
        for method, path, body in [("get", "/v1/portal/me", None), ("post", "/v1/portal/orders", {"plan": "p2000"}),
                                   ("get", "/v1/portal/orders/abc", None), ("post", "/v1/portal/orders/abc/claim", {"utr": "412345678901"})]:
            r = await getattr(c, method)(path, **({"json": body} if body else {}))
            assert r.status_code == 401, (path, r.text)
        # a plain API key is not a login either
        r = await c.get("/v1/portal/me", headers={"X-API-Key": "myk_whatever"})
        assert r.status_code == 401


async def test_me_before_and_after_getting_a_key(clerk_env):
    async with clerk_env() as (c, app):
        clerk_env.fake.users["user_1"] = google_user("buyer@gmail.com")
        h = bearer(token())
        me = (await c.get("/v1/portal/me", headers=h)).json()
        assert me["email"] == "buyer@gmail.com" and me["has_key"] is False and me["payments_enabled"] is True
        r = await c.post("/v1/portal/orders", json={"plan": "p2000"}, headers=h)
        assert r.status_code == 404 and r.json()["error"] == "no_key"
        await c.post("/v1/keys", headers=h)
        me = (await c.get("/v1/portal/me", headers=h)).json()
        assert me["has_key"] and me["usage"]["plan"] == "free" and me["usage"]["daily_limit"] == 1000 and me["orders"] == []
        assert "api_key" not in str(me)  # the raw key is never sent again


async def test_order_comes_with_a_scannable_upi_link_and_qr(clerk_env):
    async with clerk_env() as (c, app):
        h, _ = await login(clerk_env, c, app)
        r = await c.post("/v1/portal/orders", json={"plan": "p3000"}, headers=h)
        assert r.status_code == 201
        o = r.json()
        pay = o["pay"]
        assert o["status"] == "pending" and pay["upi_id"] == "madara@fam" and pay["amount_inr"] == 39
        q = parse_qs(urlparse(pay["upi_uri"]).query)
        assert urlparse(pay["upi_uri"]).scheme == "upi"
        assert q["pa"] == ["madara@fam"] and q["am"] == ["39.00"] and q["cu"] == ["INR"] and q["tn"] == [o["code"]]
        assert pay["qr"].startswith("data:image/svg+xml") and len(pay["qr"]) > 500
        again = (await c.post("/v1/portal/orders", json={"plan": "p3000"}, headers=h)).json()
        assert again["order_id"] == o["order_id"]  # same unpaid order is reused, not duplicated
        bad = await c.post("/v1/portal/orders", json={"plan": "free"}, headers=h)
        assert bad.status_code == 422


async def test_payment_email_activates_plan_and_status_flips(clerk_env):
    async with clerk_env() as (c, app):
        h, key = await login(clerk_env, c, app)
        o = (await c.post("/v1/portal/orders", json={"plan": "p2000"}, headers=h)).json()
        st = (await c.get(f"/v1/portal/orders/{o['order_id']}", headers=h)).json()
        assert st["status"] == "pending" and st["pay"]["qr"]
        mail = build_email(body=f"You received ₹29.00 from Rahul Kumar. UTR 412345678901. Note: {o['code']}")
        assert (await app.state.payments.handle_email(mail))["result"] == "matched"
        done = (await c.get(f"/v1/portal/orders/{o['order_id']}", headers=h)).json()
        assert done["status"] == "paid" and "pay" not in done
        me = (await c.get("/v1/portal/me", headers=h)).json()
        assert me["usage"]["plan"] == "p2000" and me["usage"]["daily_limit"] == 2000 and me["key"]["plan_expires_at"]
        assert me["orders"][0]["status"] == "paid"
        # and the bot's key really has the bigger limit
        usage = (await c.get("/v1/me", headers={"X-API-Key": key})).json()["usage"]
        assert usage["daily_limit"] == 2000


async def test_claim_by_utr_from_the_page(clerk_env):
    async with clerk_env() as (c, app):
        h, _ = await login(clerk_env, c, app)
        o = (await c.post("/v1/portal/orders", json={"plan": "p4000"}, headers=h)).json()
        mail = build_email(body="received ₹44.00 from Rahul. UTR 612345678901")  # forgot the note
        assert (await app.state.payments.handle_email(mail))["result"] == "unmatched"
        bad = await c.post(f"/v1/portal/orders/{o['order_id']}/claim", json={"utr": "12"}, headers=h)
        assert bad.status_code == 422
        ok = await c.post(f"/v1/portal/orders/{o['order_id']}/claim", json={"utr": "612345678901"}, headers=h)
        assert ok.status_code == 200 and ok.json()["status"] == "paid"


async def test_nobody_can_see_or_claim_someone_elses_order(clerk_env):
    async with clerk_env() as (c, app):
        ha, _ = await login(clerk_env, c, app, "alice@gmail.com", "user_a")
        hb, _ = await login(clerk_env, c, app, "bob@gmail.com", "user_b")
        o = (await c.post("/v1/portal/orders", json={"plan": "p2000"}, headers=ha)).json()
        assert (await c.get(f"/v1/portal/orders/{o['order_id']}", headers=hb)).status_code == 404
        r = await c.post(f"/v1/portal/orders/{o['order_id']}/claim", json={"utr": "412345678901"}, headers=hb)
        assert r.status_code == 404
        assert (await c.get("/v1/portal/me", headers=hb)).json()["orders"] == []


async def test_buying_is_refused_when_payments_are_not_configured(clerk_env):
    async with clerk_env(upi_id="") as (c, app):
        h, _ = await login(clerk_env, c, app)
        assert (await c.get("/v1/portal/me", headers=h)).json()["payments_enabled"] is False
        r = await c.post("/v1/portal/orders", json={"plan": "p2000"}, headers=h)
        assert r.status_code == 503 and r.json()["error"] == "payments_disabled"


async def test_portal_endpoints_are_rate_limited(clerk_env):
    async with clerk_env() as (c, app):
        h, _ = await login(clerk_env, c, app)
        codes = [(await c.get("/v1/portal/me", headers=h)).status_code for _ in range(70)]
        assert 429 in codes and codes[0] == 200


async def test_portal_page_has_the_shop_and_stays_csp_safe(clerk_env):
    async with clerk_env() as (c, app):
        r = await c.get("/portal")
        assert r.status_code == 200
        for needle in ('id="plans"', 'id="checkout"', 'id="qr"', 'id="utr"'):
            assert needle in r.text
        assert "<script>" not in r.text and "onclick=" not in r.text  # CSP forbids inline scripts
        assert "data:" in r.headers["content-security-policy"]  # the QR image
        js = (await c.get("/portal/portal.js")).text
        assert "innerHTML" not in js  # everything is written with textContent


async def test_signing_in_again_shows_the_same_key_and_usage_not_a_new_one(clerk_env):
    async with clerk_env() as (c, app):
        h, key = await login(clerk_env, c, app)
        me = (await c.get("/v1/portal/me", headers=h)).json()
        assert me["has_key"] and me["key_recoverable"] is True
        # a later visit (a fresh login token) gets the very same key back
        h2 = bearer(token("user_1"))
        r = await c.get("/v1/portal/key", headers=h2)
        assert r.status_code == 200 and r.json()["api_key"] == key
        assert "no-store" in r.headers["cache-control"]
        assert (await c.get("/v1/portal/me", headers=h2)).json()["usage"]["used_today"] == 0
        # and the key still works
        assert (await c.get("/v1/search", params={"q": "x"}, headers={"X-API-Key": key})).status_code == 200


async def test_one_free_key_per_google_account_even_with_dots_and_plus(clerk_env):
    async with clerk_env() as (c, app):
        h, _ = await login(clerk_env, c, app, "john.doe@gmail.com", "user_1")
        again = await c.post("/v1/keys", headers=h)
        assert again.status_code == 409 and again.json()["error"] == "key_exists"
        clerk_env.fake.users["user_2"] = google_user("j.o.h.n.d.o.e+spam@gmail.com", "user_2")
        other = await c.post("/v1/keys", headers=bearer(token("user_2")))
        assert other.status_code == 409  # same real Gmail account under another login = same single key


async def test_rotated_key_is_what_the_portal_shows_and_nobody_else_can_see_it(clerk_env):
    async with clerk_env() as (c, app):
        ha, old = await login(clerk_env, c, app, "alice@gmail.com", "user_a")
        hb, _ = await login(clerk_env, c, app, "bob@gmail.com", "user_b")
        new = (await c.post("/v1/keys/rotate", headers=ha)).json()["api_key"]
        assert new != old
        assert (await c.get("/v1/portal/key", headers=ha)).json()["api_key"] == new
        assert (await c.get("/v1/portal/key", headers=hb)).json()["api_key"] != new
        assert (await c.get("/v1/portal/key")).status_code == 401


async def test_old_keys_without_a_sealed_copy_ask_for_one_rotation(clerk_env):
    async with clerk_env() as (c, app):
        h, _ = await login(clerk_env, c, app)
        await app.state.db.execute("UPDATE api_keys SET key_enc=NULL")
        assert (await c.get("/v1/portal/me", headers=h)).json()["key_recoverable"] is False
        r = await c.get("/v1/portal/key", headers=h)
        assert r.status_code == 409 and r.json()["error"] == "key_not_recoverable"
        await c.post("/v1/keys/rotate", headers=h)
        assert (await c.get("/v1/portal/key", headers=h)).status_code == 200
