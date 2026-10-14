from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..deps import require_admin
from ..errors import ApiError
from ..payments.emailparse import parse_payment

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


class PlanBody(BaseModel):
    plan: str
    days: int | None = Field(None, ge=1, le=3650)


class CookiesBody(BaseModel):
    cookies: str = Field(..., min_length=20, max_length=2_000_000)


class ProxyBody(BaseModel):
    proxy: str = Field("", max_length=500)  # empty clears it


class ParseBody(BaseModel):
    subject: str = ""
    text: str


async def _key_view(st, rec) -> dict:
    return {
        **rec.public(st.clock, st.keys.effective_plan(rec)),
        "usage": await st.keys.usage_info(rec),
    }


@router.get("/stats")
async def stats(request: Request):
    st = request.app.state
    day = st.clock.today()
    keys = await st.db.fetchone("SELECT COUNT(*) AS n, SUM(active) AS a FROM api_keys")
    plays = await st.db.fetchone("SELECT COALESCE(SUM(count),0) AS n FROM usage WHERE day=?", (day,))
    paid = await st.db.fetchone("SELECT COUNT(*) AS n FROM api_keys WHERE plan<>'free' AND active=1")
    orders = await st.db.fetchall("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")
    return {
        "keys_total": keys["n"],
        "keys_active": keys["a"] or 0,
        "keys_on_paid_plan": paid["n"],
        "plays_today": plays["n"],
        "orders_by_status": {r["status"]: r["n"] for r in orders},
    }


@router.get("/keys")
async def list_keys(request: Request, owner: str | None = None, limit: int = Query(50, ge=1, le=500)):
    st = request.app.state
    return [await _key_view(st, r) for r in await st.keys.list(owner, limit)]


@router.get("/keys/{key_id}")
async def get_key(key_id: int, request: Request):
    st = request.app.state
    rec = await st.keys.get(key_id)
    if rec is None:
        raise ApiError(404, "key_not_found", "no such key")
    return await _key_view(st, rec)


@router.post("/keys/{key_id}/plan")
async def set_plan(key_id: int, body: PlanBody, request: Request):
    st = request.app.state
    return await _key_view(st, await st.keys.set_plan(key_id, body.plan, body.days))


@router.post("/keys/{key_id}/revoke")
async def revoke(key_id: int, request: Request):
    st = request.app.state
    return await _key_view(st, await st.keys.set_active(key_id, False))


@router.post("/keys/{key_id}/activate")
async def activate(key_id: int, request: Request):
    st = request.app.state
    return await _key_view(st, await st.keys.set_active(key_id, True))


@router.post("/keys/{key_id}/rotate")
async def rotate(key_id: int, request: Request):
    st = request.app.state
    raw, rec = await st.keys.rotate(key_id)
    return {"api_key": raw, "warning": "Shown once. The old key stopped working.", **await _key_view(st, rec)}


@router.post("/keys/{key_id}/reset-usage")
async def reset_usage(key_id: int, request: Request):
    st = request.app.state
    rec = await st.keys.get(key_id)
    if rec is None:
        raise ApiError(404, "key_not_found", "no such key")
    await st.keys.reset_usage(key_id)
    return await _key_view(st, rec)


@router.get("/orders")
async def orders(request: Request, status: str | None = None, limit: int = Query(50, ge=1, le=500)):
    return await request.app.state.payments.list_orders(status, limit)


@router.post("/orders/{order_id}/approve")
async def approve(order_id: str, request: Request):
    return await request.app.state.payments.admin_approve(order_id)


@router.get("/payments")
async def payments(request: Request, status: str | None = None, limit: int = Query(50, ge=1, le=500)):
    return await request.app.state.payments.list_payments(status, limit)


@router.get("/emails")
async def emails(request: Request, result: str | None = None, limit: int = Query(50, ge=1, le=500)):
    """What the IMAP poller has seen (sender, subject, outcome; never the body).
    Look at 'ignored_sender' rows to find the real From address of payment mails."""
    return await request.app.state.payments.list_emails(result, limit)


@router.post("/parse-email")
async def parse_email(body: ParseBody, request: Request):
    """Paste a payment e-mail's subject/body to see what the parser extracts."""
    p = parse_payment(body.subject, body.text, request.app.state.settings)
    return {
        "direction": p.direction,
        "amount_inr": None if p.amount_paise is None else p.amount_paise / 100,
        "utr": p.utr,
        "order_code": p.code,
        "payer": p.payer,
        "would_count_as_payment": p.direction == "in" and p.amount_paise is not None,
    }


@router.get("/youtube")
async def youtube_status(request: Request):
    """Is the host blocked, are cookies / a proxy set. Secrets are never shown."""
    return request.app.state.extractor.status()


@router.post("/youtube/cookies")
async def youtube_set_cookies(body: CookiesBody, request: Request):
    """Paste a Netscape cookies.txt from a throwaway Google account. Saved on the data volume, applied at once."""
    n = request.app.state.extractor.set_cookies(body.cookies)
    return {"ok": True, "cookies": n}


@router.delete("/youtube/cookies")
async def youtube_clear_cookies(request: Request):
    request.app.state.extractor.clear_cookies()
    return {"ok": True}


@router.post("/youtube/proxy")
async def youtube_set_proxy(body: ProxyBody, request: Request):
    """Set (or clear, with an empty string) the proxy used for both extraction and streaming."""
    st = request.app.state
    st.extractor.set_proxy(body.proxy)
    await st.swap_stream_client()
    return {"ok": True, **st.extractor.status()}


@router.post("/youtube/unblock")
async def youtube_unblock(request: Request):
    """Skip the cool-down and try YouTube again right now."""
    request.app.state.extractor.reset_block()
    return {"ok": True}
