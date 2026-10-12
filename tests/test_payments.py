import re

import pytest

from app.payments.emailparse import parse_payment, parse_email_bytes
from tests.conftest import build_email, make_settings


# ── parser ────────────────────────────────────────────────────────────────
@pytest.fixture
def cfg(tmp_path):
    return make_settings(tmp_path)


def test_parser_extracts_amount_utr_code_payer(cfg):
    p = parse_payment("Payment received", "You received ₹29.00 from Rahul Kumar. UTR 412345678901. Note: myt8f3k2q", cfg)
    assert p.direction == "in"
    assert p.amount_paise == 2900
    assert p.utr == "412345678901"
    assert p.code == "MYT8F3K2Q"
    assert p.payer == "Rahul Kumar"


@pytest.mark.parametrize("text,paise", [
    ("received Rs. 79", 7900),
    ("received Rs 1,079.50", 107950),
    ("credited INR 44.00 to your account", 4400),
    ("money received. Amount: 39", 3900),
    ("received ₹ 2,000", 200000),
])
def test_parser_amount_formats(cfg, text, paise):
    assert parse_payment("", text, cfg).amount_paise == paise


def test_parser_direction(cfg):
    assert parse_payment("", "You paid ₹29 to Swiggy. UTR 111111111111", cfg).direction == "out"
    assert parse_payment("Refund received", "Refund of ₹29 received", cfg).direction == "out"
    assert parse_payment("", "₹29 debited from your account", cfg).direction == "out"
    assert parse_payment("", "Your KYC is complete ₹29", cfg).direction == "unknown"


def test_parser_utr_variants(cfg):
    assert parse_payment("", "received ₹29 Ref No: 998877665544", cfg).utr == "998877665544"
    assert parse_payment("", "received ₹29. 556677889900 is the id", cfg).utr == "556677889900"
    assert parse_payment("", "received ₹29. phone 9876543210", cfg).utr is None  # 10 digits is not a UTR
    assert parse_payment("", "received ₹29 order 1234567890123456", cfg).utr is None  # 16 digits is not one either


def test_parser_code_is_strict(cfg):
    assert parse_payment("", "received ₹29 note MYT plan29", cfg).code is None
    assert parse_payment("", "received ₹29 note MYTABC", cfg).code is None
    assert parse_payment("", "received ₹29 note MYTAAAAAA9", cfg).code is None  # 7 chars after prefix
    assert parse_payment("", "received ₹29 (MYT2345AB)", cfg).code == "MYT2345AB"


def test_html_only_email_is_parsed(cfg):
    raw = build_email(body="<b>You received</b> ₹39.00<br>UTR 123123123123", html=True)
    info = parse_email_bytes(raw, cfg)
    p = parse_payment(info.subject, info.text, cfg)
    assert p.amount_paise == 3900 and p.utr == "123123123123"


def test_email_auth_detection(cfg):
    good = parse_email_bytes(build_email(), cfg)
    assert good.auth_ok and good.from_addr == "no-reply@fampay.in"
    assert not parse_email_bytes(build_email(auth=None), cfg).auth_ok
    assert not parse_email_bytes(build_email(auth="mx.google.com; dkim=fail; spf=fail; dmarc=fail"), cfg).auth_ok
    # dkim pass for somebody else's domain must not count
    assert not parse_email_bytes(build_email(auth="mx.google.com; dkim=pass header.i=@evil.com"), cfg).auth_ok


# ── end to end: order → email → plan ──────────────────────────────────────
async def make_order(api, owner="buyer", plan="p2000"):
    key = await api.new_key(owner)
    r = await api.http.post("/v1/orders", json={"plan": plan}, headers=api.h(key))
    assert r.status_code == 201, r.text
    return key, r.json()


async def usage(api, key):
    return (await api.http.get("/v1/me", headers=api.h(key))).json()["usage"]


async def test_order_shape_and_idempotence(api):
    key, order = await make_order(api, plan="p3000")
    assert re.fullmatch(r"MYT[A-HJ-NP-Z2-9]{6}", order["code"])
    assert order["amount_inr"] == 39 and order["status"] == "pending"
    assert order["pay"]["upi_id"] == "madara@fam" and order["pay"]["note"] == order["code"]
    again = (await api.http.post("/v1/orders", json={"plan": "p3000"}, headers=api.h(key))).json()
    assert again["order_id"] == order["order_id"]  # same pending order, no code spam
    for bad in ["free", "p9999", ""]:
        r = await api.http.post("/v1/orders", json={"plan": bad}, headers=api.h(key))
        assert r.status_code == 422 and r.json()["error"] == "invalid_plan"


async def test_payment_email_with_code_upgrades_the_plan(api):
    key, order = await make_order(api, plan="p2000")
    mail = build_email(body=f"You received ₹29.00 from Rahul. UTR 412345678901. Note: {order['code']}")
    res = await api.st.payments.handle_email(mail)
    assert res["result"] == "matched" and res["plan"] == "p2000"
    u = await usage(api, key)
    assert u["plan"] == "p2000" and u["daily_limit"] == 2000 and u["plan_expires_at"]
    o = (await api.http.get(f"/v1/orders/{order['order_id']}", headers=api.h(key))).json()
    assert o["status"] == "paid" and o["paid_at"] and "pay" not in o


@pytest.mark.parametrize("plan,price", [("p2000", 29), ("p3000", 39), ("p4000", 44), ("p10000", 79)])
async def test_every_paid_plan_activates_at_its_price(api, plan, price):
    key, order = await make_order(api, owner=f"o-{plan}", plan=plan)
    utr = {"p2000": "100000000001", "p3000": "100000000002", "p4000": "100000000003", "p10000": "100000000004"}[plan]
    mail = build_email(body=f"received ₹{price}.00 UTR {utr} note {order['code']}")
    assert (await api.st.payments.handle_email(mail))["result"] == "matched"
    assert (await usage(api, key))["plan"] == plan


async def test_same_email_twice_never_double_credits(api):
    key, order = await make_order(api)
    mail = build_email(body=f"received ₹29 UTR 412345678901 {order['code']}")
    assert (await api.st.payments.handle_email(mail))["result"] == "matched"
    expiry1 = (await usage(api, key))["plan_expires_at"]
    assert (await api.st.payments.handle_email(mail))["result"] == "already_processed"
    # the bank sends a second notification (new Message-ID, same UTR)
    mail2 = build_email(subject="Money received", body=f"received ₹29 UTR 412345678901 {order['code']}")
    assert (await api.st.payments.handle_email(mail2))["result"] == "duplicate_payment"
    assert (await usage(api, key))["plan_expires_at"] == expiry1


async def test_underpayment_does_not_activate(api):
    key, order = await make_order(api, plan="p4000")
    mail = build_email(body=f"received ₹29 UTR 200000000001 {order['code']}")
    res = await api.st.payments.handle_email(mail)
    assert res["result"] == "underpaid"
    assert (await usage(api, key))["plan"] == "free"
    pays = (await api.http.get("/admin/payments", params={"status": "underpaid"}, headers=api.admin)).json()
    assert pays[0]["amount_inr"] == 29 and "needed 44" in pays[0]["note"]


async def test_overpayment_is_accepted(api):
    key, order = await make_order(api, plan="p2000")
    mail = build_email(body=f"received ₹100 UTR 200000000002 {order['code']}")
    assert (await api.st.payments.handle_email(mail))["result"] == "matched"
    assert (await usage(api, key))["plan"] == "p2000"


async def test_outgoing_money_emails_are_ignored(api):
    key, order = await make_order(api)
    mail = build_email(subject="Payment sent", body=f"You paid ₹29 to Someone. UTR 300000000001 {order['code']}")
    assert (await api.st.payments.handle_email(mail))["result"] == "ignored_outgoing"
    assert (await usage(api, key))["plan"] == "free"


async def test_spoofed_emails_cannot_buy_a_plan(api):
    key, order = await make_order(api)
    body = f"received ₹29 UTR 400000000001 {order['code']}"
    # wrong sender domain
    r = await api.st.payments.handle_email(build_email(body=body, sender="scammer@gmail.com", message_id="<s1@x>"))
    assert r["result"] == "ignored_sender"
    # right sender address but the mail server could not verify it
    r = await api.st.payments.handle_email(build_email(body=body, auth=None, message_id="<s2@x>"))
    assert r["result"] == "rejected_unauthenticated"
    r = await api.st.payments.handle_email(
        build_email(body=body, auth="mx.google.com; dkim=fail; dmarc=fail", message_id="<s3@x>"))
    assert r["result"] == "rejected_unauthenticated"
    # a forged header lower down must not beat the genuine failing one on top
    raw = build_email(body=body, auth="mx.google.com; dkim=fail; dmarc=fail", message_id="<s4@x>")
    raw = raw.replace(b"Authentication-Results:", b"X-Forged: 1\r\nAuthentication-Results:", 1)
    raw += b""
    assert (await api.st.payments.handle_email(raw))["result"] == "rejected_unauthenticated"
    assert (await usage(api, key))["plan"] == "free"
    ignored = (await api.http.get("/admin/emails", params={"result": "ignored_sender"}, headers=api.admin)).json()
    assert ignored[0]["from"] == "scammer@gmail.com"


async def test_auth_check_can_be_switched_off(make_api):
    async with make_api(require_email_auth=False) as api:
        key, order = await make_order(api)
        mail = build_email(body=f"received ₹29 UTR 500000000001 {order['code']}", auth=None)
        assert (await api.st.payments.handle_email(mail))["result"] == "matched"


async def test_email_without_code_stays_unmatched_then_claim_by_utr(api):
    key, order = await make_order(api, plan="p3000")
    # payment arrives first, user forgot the note
    mail = build_email(body="received ₹39.00 from Rahul. UTR 612345678901")
    assert (await api.st.payments.handle_email(mail))["result"] == "unmatched"
    assert (await usage(api, key))["plan"] == "free"
    r = await api.http.post(f"/v1/orders/{order['order_id']}/claim", json={"utr": "612345678901"}, headers=api.h(key))
    assert r.status_code == 200 and r.json()["status"] == "paid"
    assert (await usage(api, key))["plan"] == "p3000"


async def test_claim_first_then_email_arrives(api):
    key, order = await make_order(api, plan="p2000")
    r = await api.http.post(f"/v1/orders/{order['order_id']}/claim", json={"utr": "712345678901"}, headers=api.h(key))
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert "payment e-mail arrives" in r.json()["message"]
    mail = build_email(body="received ₹29 UTR 712345678901 no note")
    res = await api.st.payments.handle_email(mail)
    assert res["result"] == "matched"
    assert (await usage(api, key))["plan"] == "p2000"


async def test_claim_rules(api):
    key, order = await make_order(api, plan="p4000")
    other_key, other_order = await make_order(api, owner="someone-else", plan="p2000")
    # bad UTR format
    r = await api.http.post(f"/v1/orders/{order['order_id']}/claim", json={"utr": "12345"}, headers=api.h(key))
    assert r.status_code == 422 and r.json()["error"] == "invalid_utr"
    # claiming somebody else's order
    r = await api.http.post(f"/v1/orders/{other_order['order_id']}/claim", json={"utr": "812345678901"}, headers=api.h(key))
    assert r.status_code == 404
    # real payment too small for the plan
    await api.st.payments.handle_email(build_email(body="received ₹29 UTR 912345678901"))
    r = await api.http.post(f"/v1/orders/{order['order_id']}/claim", json={"utr": "912345678901"}, headers=api.h(key))
    assert r.status_code == 402 and r.json()["error"] == "underpaid"
    # the same payment used by one buyer cannot be reused by another
    r = await api.http.post(f"/v1/orders/{other_order['order_id']}/claim", json={"utr": "912345678901"}, headers=api.h(other_key))
    assert r.status_code == 200 and r.json()["status"] == "paid"
    third_key, third_order = await make_order(api, owner="third", plan="p2000")
    r = await api.http.post(f"/v1/orders/{third_order['order_id']}/claim", json={"utr": "912345678901"}, headers=api.h(third_key))
    assert r.status_code == 409 and r.json()["error"] == "utr_used"


async def test_two_orders_cannot_claim_the_same_pending_utr(api):
    k1, o1 = await make_order(api, owner="c1", plan="p2000")
    k2, o2 = await make_order(api, owner="c2", plan="p2000")
    assert (await api.http.post(f"/v1/orders/{o1['order_id']}/claim", json={"utr": "111222333444"}, headers=api.h(k1))).status_code == 200
    r = await api.http.post(f"/v1/orders/{o2['order_id']}/claim", json={"utr": "111222333444"}, headers=api.h(k2))
    assert r.status_code == 409 and r.json()["error"] == "utr_in_use"


async def test_renewing_same_plan_stacks_time_and_other_plan_replaces(api):
    key, o1 = await make_order(api, plan="p2000")
    await api.st.payments.handle_email(build_email(body=f"received ₹29 UTR 130000000001 {o1['code']}"))
    kid = await api.key_id(key)
    exp1 = (await api.st.keys.get(kid)).plan_expires_at
    # buy the same plan again while it is still active
    api.st.clock.advance(5 * 86400)
    o2 = (await api.http.post("/v1/orders", json={"plan": "p2000"}, headers=api.h(key))).json()
    await api.st.payments.handle_email(build_email(body=f"received ₹29 UTR 130000000002 {o2['code']}"))
    exp2 = (await api.st.keys.get(kid)).plan_expires_at
    assert exp2 == exp1 + 30 * 86400
    # switching to another plan starts a fresh period
    o3 = (await api.http.post("/v1/orders", json={"plan": "p10000"}, headers=api.h(key))).json()
    await api.st.payments.handle_email(build_email(body=f"received ₹79 UTR 130000000003 {o3['code']}"))
    rec = await api.st.keys.get(kid)
    assert rec.plan == "p10000"
    assert rec.plan_expires_at == api.st.clock.now_ts() + 30 * 86400


async def test_old_unpaid_order_expires_and_late_payment_is_not_applied(api):
    key, order = await make_order(api, plan="p2000")
    api.st.clock.advance((24 + 7) * 3600)  # past order lifetime + grace
    o = (await api.http.get(f"/v1/orders/{order['order_id']}", headers=api.h(key))).json()
    assert o["status"] == "expired"
    res = await api.st.payments.handle_email(build_email(body=f"received ₹29 UTR 140000000001 {order['code']}"))
    assert res["result"] == "order_not_open"
    assert (await usage(api, key))["plan"] == "free"
    r = await api.http.post(f"/v1/orders/{order['order_id']}/claim", json={"utr": "140000000002"}, headers=api.h(key))
    assert r.status_code == 410


async def test_payment_inside_grace_window_still_counts(api):
    key, order = await make_order(api, plan="p2000")
    api.st.clock.advance((24 + 3) * 3600)  # order expired, but within the 6h grace
    res = await api.st.payments.handle_email(build_email(body=f"received ₹29 UTR 150000000001 {order['code']}"))
    assert res["result"] == "matched"


async def test_admin_manual_approve(api):
    key, order = await make_order(api, plan="p4000")
    r = await api.http.post(f"/admin/orders/{order['order_id']}/approve", headers=api.admin)
    assert r.status_code == 200 and r.json()["status"] == "paid"
    assert (await usage(api, key))["plan"] == "p4000"
    again = await api.http.post(f"/admin/orders/{order['order_id']}/approve", headers=api.admin)
    assert again.json()["status"] == "paid"  # idempotent


async def test_unparseable_email_is_recorded_not_crashed(api):
    r = await api.st.payments.handle_email(build_email(subject="Welcome to FamPay", body="Thanks for joining!"))
    assert r["result"] == "unparsed"
    r = await api.st.payments.handle_email(b"this is not even an email")
    assert r["result"] == "ignored_sender"


async def test_admin_parse_email_endpoint(api):
    r = await api.http.post("/admin/parse-email", headers=api.admin,
                            json={"subject": "Payment received", "text": "received ₹44 UTR 160000000001 MYTAB23CD"})
    j = r.json()
    assert j["would_count_as_payment"] and j["amount_inr"] == 44 and j["order_code"] == "MYTAB23CD"


async def test_many_simultaneous_orders_get_unique_codes(api):
    import asyncio

    keys = [await api.new_key(f"bulk{i}") for i in range(20)]
    rs = await asyncio.gather(*[api.http.post("/v1/orders", json={"plan": "p2000"}, headers=api.h(k)) for k in keys])
    codes = {r.json()["code"] for r in rs}
    assert len(codes) == 20
