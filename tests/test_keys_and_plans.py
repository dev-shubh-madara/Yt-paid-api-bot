import sqlite3

from app.plans import FREE, PLANS, paid_plans


def test_plan_table_matches_the_spec():
    assert (FREE.daily_limit, FREE.price_inr) == (1000, 0)
    table = {p.daily_limit: p.price_inr for p in paid_plans()}
    assert table == {2000: 29, 3000: 39, 4000: 44, 10000: 79}
    assert PLANS["p2000"].price_paise == 2900


async def test_root_health_plans_are_public(api):
    assert (await api.http.get("/")).status_code == 200
    h = (await api.http.get("/health")).json()
    assert h["status"] == "ok" and h["yt_dlp"]
    p = (await api.http.get("/v1/plans")).json()
    assert p["free"]["daily_limit"] == 1000
    assert [(x["daily_limit"], x["price_inr"]) for x in p["paid"]] == [(2000, 29), (3000, 39), (4000, 44), (10000, 79)]
    assert p["pay_to"]["upi_id"] == "madara@fam"


async def test_key_creation_and_me(api):
    r = await api.http.post("/v1/keys", json={"owner": "@Shubh_123", "name": "bot"})
    assert r.status_code == 201
    body = r.json()
    key = body["api_key"]
    assert key.startswith("myk_") and len(key) > 30
    assert body["plan"] == "free" and body["daily_limit"] == 1000

    me = (await api.http.get("/v1/me", headers=api.h(key))).json()
    assert me["key"]["owner"] == "@shubh_123"  # normalised to lower case
    assert me["usage"]["daily_limit"] == 1000
    assert me["usage"]["used_today"] == 0 and me["usage"]["remaining_today"] == 1000


async def test_only_the_hash_of_the_key_is_stored(api, tmp_path):
    key = await api.new_key("hashcheck")
    con = sqlite3.connect(str(tmp_path / "api.db"))
    dump = "\n".join(str(r) for r in con.execute("SELECT * FROM api_keys"))
    assert key not in dump
    assert key[:8] in dump  # only the short display prefix


async def test_one_active_key_per_owner(api):
    await api.new_key("same-owner")
    r = await api.http.post("/v1/keys", json={"owner": "SAME-owner"})
    assert r.status_code == 409 and r.json()["error"] == "key_exists"


async def test_invalid_owner_rejected(api):
    for bad in ["ab", "has space", "x" * 80, "bad;drop"]:
        r = await api.http.post("/v1/keys", json={"owner": bad})
        assert r.status_code == 422, bad
        assert r.json()["error"] == "invalid_owner"


async def test_auth_failures(api):
    r = await api.http.get("/v1/me")
    assert r.status_code == 401 and r.json()["error"] == "api_key_required"
    r = await api.http.get("/v1/me", headers={"X-API-Key": "myk_nope"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_api_key"
    key = await api.new_key("bearer-user")
    r = await api.http.get("/v1/me", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    # keys in the query string are NOT accepted (they leak into logs)
    r = await api.http.get("/v1/me", params={"key": key})
    assert r.status_code == 401


async def test_per_ip_key_creation_limit(make_api):
    async with make_api(key_creations_per_ip_per_hour=3) as api:
        for i in range(3):
            assert (await api.http.post("/v1/keys", json={"owner": f"owner{i}"})).status_code == 201
        r = await api.http.post("/v1/keys", json={"owner": "owner99"})
        assert r.status_code == 429 and r.json()["error"] == "too_many_keys"
        assert "retry-after" in r.headers


async def test_creation_secret_protects_signup(make_api):
    async with make_api(key_creation_secret="s3cret") as api:
        assert (await api.http.post("/v1/keys", json={"owner": "nosecret"})).status_code == 403
        r = await api.http.post("/v1/keys", json={"owner": "withsecret"}, headers={"X-Creation-Secret": "s3cret"})
        assert r.status_code == 201


async def test_public_signup_can_be_disabled(make_api):
    async with make_api(allow_public_key_creation=False) as api:
        r = await api.http.post("/v1/keys", json={"owner": "someone"})
        assert r.status_code == 403 and r.json()["error"] == "creation_disabled"


async def test_request_rate_limit_per_key(make_api):
    async with make_api(rate_limit_per_minute=5) as api:
        key = await api.new_key("rl")
        codes = [(await api.http.get("/v1/me", headers=api.h(key))).status_code for _ in range(7)]
        assert codes == [200] * 5 + [429] * 2


async def test_revoke_rotate_and_reactivate(api):
    key = await api.new_key("lifecycle")
    kid = await api.key_id(key)
    r = await api.http.post(f"/admin/keys/{kid}/revoke", headers=api.admin)
    assert r.status_code == 200 and r.json()["active"] is False
    assert (await api.http.get("/v1/me", headers=api.h(key))).status_code == 401

    # the owner can get a fresh key while the old one is revoked
    new_key = await api.new_key("lifecycle")
    assert new_key != key
    # ...but cannot re-activate the old one on top (one active key per owner)
    r = await api.http.post(f"/admin/keys/{kid}/activate", headers=api.admin)
    assert r.status_code == 409

    kid2 = await api.key_id(new_key)
    r = await api.http.post(f"/admin/keys/{kid2}/rotate", headers=api.admin)
    rotated = r.json()["api_key"]
    assert (await api.http.get("/v1/me", headers=api.h(new_key))).status_code == 401
    assert (await api.http.get("/v1/me", headers=api.h(rotated))).status_code == 200


async def test_admin_requires_token(api, make_api):
    assert (await api.http.get("/admin/keys")).status_code == 401
    assert (await api.http.get("/admin/keys", headers={"X-Admin-Token": "wrong"})).status_code == 401
    assert (await api.http.get("/admin/keys", headers=api.admin)).status_code == 200
    async with make_api(admin_token="") as no_admin:
        r = await no_admin.http.get("/admin/keys", headers={"X-Admin-Token": ""})
        assert r.status_code == 503 and r.json()["error"] == "admin_disabled"


async def test_wrong_admin_tokens_are_rate_limited(api):
    codes = [(await api.http.get("/admin/keys", headers={"X-Admin-Token": f"guess{i}"})).status_code
             for i in range(13)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429] * 3
    # the right token still works for legitimate use
    assert (await api.http.get("/admin/keys", headers=api.admin)).status_code == 200
