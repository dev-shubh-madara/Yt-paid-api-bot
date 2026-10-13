import html
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import segno
from pydantic import BaseModel, Field

from ..deps import AuthedKey, client_ip, require_key
from ..errors import ApiError
from ..plans import PLANS, paid_plans
from ..security import safe_equals

router = APIRouter()


class KeyCreate(BaseModel):
    owner: str | None = Field(None, description="Only for server-side issuing with X-Creation-Secret (or when Google login is off)")
    name: str | None = Field(None, max_length=40)


class OrderCreate(BaseModel):
    plan: str = Field(..., description="One of p2000, p3000, p4000, p10000")


class ClaimBody(BaseModel):
    utr: str = Field(..., description="12-digit UPI reference (UTR) of your payment")


def _plan_json(p) -> dict:
    return {"id": p.id, "name": p.name, "daily_limit": p.daily_limit, "price_inr": p.price_inr}


@router.get("/")
async def root(request: Request):
    return {
        "name": "Madara YouTube API",
        "docs": "/docs" if request.app.state.settings.expose_docs else None,
        "plans": [_plan_json(p) for p in PLANS.values()],
        "plan_duration_days": request.app.state.settings.plan_duration_days,
    }


@router.get("/health")
async def health(request: Request):
    import yt_dlp

    poller = getattr(request.app.state, "poller", None)
    body = {"status": "ok", "yt_dlp": yt_dlp.version.__version__}
    if poller is not None:
        body["imap"] = {"ok": poller.last_error is None, "last_error": poller.last_error}
    return body


@router.get("/v1/plans")
async def plans(request: Request):
    s = request.app.state.settings
    return {
        "plan_duration_days": s.plan_duration_days,
        "free": _plan_json(PLANS["free"]),
        "paid": [_plan_json(p) for p in paid_plans()],
        "pay_to": {"upi_id": s.upi_id or None, "payee_name": s.payee_name or None},
        "limit_unit": "songs (plays) per day; resets at midnight " + s.timezone,
    }


def _key_response(raw, rec, st):
    plan = st.keys.effective_plan(rec)
    return {
        "api_key": raw,
        "warning": "Your key is saved to your account. Sign in at /portal any time to see it again.",
        "owner": rec.owner,
        "plan": plan.id,
        "daily_limit": plan.daily_limit,
    }


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer ") or not auth[7:].strip():
        raise ApiError(401, "login_required", "Sign in with Google at /portal to get a key.")
    return auth[7:].strip()


def _ip_gate(st, ip: str) -> None:
    s = st.settings
    ok, retry = st.limiter.allow(f"newkey:{ip}", s.key_creations_per_ip_per_hour, 3600)
    if not ok:
        raise ApiError(
            429, "too_many_keys", "Too many keys created from this address. Try again later.",
            headers={"Retry-After": str(retry)},
        )


@router.post("/v1/keys", status_code=201)
async def create_key(
    request: Request,
    body: KeyCreate | None = None,
    x_creation_secret: str = Header("", alias="X-Creation-Secret"),
):
    st = request.app.state
    s = st.settings
    ip = client_ip(request)
    secret_ok = bool(s.key_creation_secret and x_creation_secret
                     and safe_equals(x_creation_secret, s.key_creation_secret))
    if secret_ok or not st.clerk.enabled:
        # Server-side issuing (your own bot/admin) or Google login not configured.
        if s.key_creation_secret and not secret_ok:
            raise ApiError(403, "creation_forbidden", "Key creation needs the X-Creation-Secret header.")
        if not s.key_creation_secret and not s.allow_public_key_creation:
            raise ApiError(403, "creation_disabled", "Public key creation is turned off on this server.")
        _ip_gate(st, ip)
        raw, rec = await st.keys.create(body.owner if body else "", body.name if body else None, ip)
        return _key_response(raw, rec, st)

    # Normal path: Google login, one key per Google account.
    _ip_gate(st, ip)
    uid = await st.clerk.verify_session(_bearer(request))
    ident = await st.clerk.google_identity(uid)
    raw, rec = await st.keys.create_for_google(ident.email, ip)
    return _key_response(raw, rec, st)


@router.post("/v1/keys/rotate")
async def rotate_my_key(request: Request):
    """Lost your key? Sign in again and get a new one (the old one stops working)."""
    st = request.app.state
    if not st.clerk.enabled:
        raise ApiError(404, "not_available", "Google login is not enabled on this server.")
    ok, retry = st.limiter.allow(f"rotate:{client_ip(request)}", 5, 3600)
    if not ok:
        raise ApiError(429, "rate_limited", "Too many attempts.", headers={"Retry-After": str(retry)})
    uid = await st.clerk.verify_session(_bearer(request))
    ident = await st.clerk.google_identity(uid)
    rec = await st.keys.active_by_owner("g:" + ident.email)
    if rec is None:
        raise ApiError(404, "no_key", "No active key for this account. Create one first.")
    raw, rec = await st.keys.rotate(rec.id)
    return _key_response(raw, rec, st)


# ── Buying a plan from the web page (Google-login protected) ──────────────
_IDENT_TTL = 60.0


async def _portal_identity(request: Request):
    """The signed-in Google account, with a short cache so page refreshes do not hammer Clerk."""
    st = request.app.state
    if not st.clerk.enabled:
        raise ApiError(404, "not_available", "Google login is not enabled on this server.")
    ip = client_ip(request)
    ok, retry = st.limiter.allow(f"portal:{ip}", 60, 60)
    if not ok:
        raise ApiError(429, "rate_limited", "Too many requests. Slow down a little.", headers={"Retry-After": str(retry)})
    uid = await st.clerk.verify_session(_bearer(request))
    cache = st.__dict__.setdefault("_portal_ident", {})
    hit = cache.get(uid)
    now = time.monotonic()
    if hit and now - hit[1] < _IDENT_TTL:
        return hit[0]
    ident = await st.clerk.google_identity(uid)
    if len(cache) > 2000:
        cache.clear()
    cache[uid] = (ident, now)
    return ident


async def _portal_key(request: Request):
    ident = await _portal_identity(request)
    rec = await request.app.state.keys.active_by_owner("g:" + ident.email)
    if rec is None:
        raise ApiError(404, "no_key", "Create your API key first, then you can buy a plan.")
    return ident, rec


def _upi_uri(upi_id: str, payee: str, amount: float, note: str) -> str:
    q = {"pa": upi_id, "pn": payee or "API", "am": f"{amount:.2f}", "cu": "INR", "tn": note}
    return "upi://pay?" + urlencode(q, quote_via=quote, safe="@")


def _portal_order(order: dict) -> dict:
    """Add the QR code and a one-tap UPI link to a pending order."""
    pay = order.get("pay")
    if pay and pay.get("upi_id"):
        uri = _upi_uri(pay["upi_id"], pay.get("payee_name") or "", float(pay["amount_inr"]), pay["note"])
        pay["upi_uri"] = uri
        pay["qr"] = segno.make(uri, error="m").svg_data_uri(scale=6, border=2, dark="#000000", light="#ffffff")
    return order


@router.get("/v1/portal/me")
async def portal_me(request: Request):
    st = request.app.state
    ident = await _portal_identity(request)
    rec = await st.keys.active_by_owner("g:" + ident.email)
    out = {"email": ident.email, "has_key": rec is not None, "payments_enabled": bool(st.settings.upi_id)}
    if rec is not None:
        out["key_recoverable"] = await st.keys.reveal(rec.id) is not None
        plan = st.keys.effective_plan(rec)
        out["key"] = rec.public(st.clock, plan)
        out["usage"] = await st.keys.usage_info(rec)
        orders = await st.db.fetchall(
            "SELECT * FROM orders WHERE key_id=? ORDER BY created_at DESC LIMIT 5", (rec.id,))
        out["orders"] = [st.payments.order_view(o) for o in orders]
    return out


@router.get("/v1/portal/key")
async def portal_show_key(request: Request):
    """Show the signed-in account's own key again (one key per Google account, so there is nothing new to hand out)."""
    st = request.app.state
    _, rec = await _portal_key(request)
    raw = await st.keys.reveal(rec.id)
    if raw is None:
        raise ApiError(409, "key_not_recoverable",
                       "This key was made before key recovery existed. Use 'Rotate key' once and it will always be shown from then on.")
    return JSONResponse({"api_key": raw}, headers={"Cache-Control": "no-store"})


@router.post("/v1/portal/orders", status_code=201)
async def portal_create_order(body: OrderCreate, request: Request):
    st = request.app.state
    if not st.settings.upi_id:
        raise ApiError(503, "payments_disabled", "Buying plans is not switched on yet. Contact the owner.")
    _, rec = await _portal_key(request)
    return _portal_order(await st.payments.create_order(rec, body.plan))


@router.get("/v1/portal/orders/{order_id}")
async def portal_get_order(order_id: str, request: Request):
    _, rec = await _portal_key(request)
    return _portal_order(await request.app.state.payments.get_order(rec, order_id))


@router.post("/v1/portal/orders/{order_id}/claim")
async def portal_claim_order(order_id: str, body: ClaimBody, request: Request):
    _, rec = await _portal_key(request)
    return _portal_order(await request.app.state.payments.claim(rec, order_id, body.utr))


_STATIC = Path(__file__).resolve().parent.parent / "static"


@router.get("/portal", include_in_schema=False)
async def portal(request: Request):
    st = request.app.state
    if not st.clerk.enabled:
        raise ApiError(404, "not_available", "Google login is not enabled on this server.")
    page = (_STATIC / "portal.html").read_text(encoding="utf-8")
    page = page.replace("__PK__", html.escape(st.settings.clerk_publishable_key, quote=True))
    page = page.replace("__DOMAIN__", html.escape(st.clerk.domain, quote=True))
    return HTMLResponse(page)


@router.get("/portal/portal.js", include_in_schema=False)
async def portal_js():
    return FileResponse(_STATIC / "portal.js", media_type="application/javascript")


@router.get("/portal/portal.css", include_in_schema=False)
async def portal_css():
    return FileResponse(_STATIC / "portal.css", media_type="text/css")


@router.get("/v1/me")
async def me(request: Request, auth: AuthedKey = Depends(require_key)):
    st = request.app.state
    return {
        "key": auth.rec.public(st.clock, auth.plan),
        "usage": await st.keys.usage_info(auth.rec),
    }


@router.post("/v1/orders", status_code=201)
async def create_order(body: OrderCreate, request: Request, auth: AuthedKey = Depends(require_key)):
    return await request.app.state.payments.create_order(auth.rec, body.plan)


@router.get("/v1/orders/{order_id}")
async def get_order(order_id: str, request: Request, auth: AuthedKey = Depends(require_key)):
    return await request.app.state.payments.get_order(auth.rec, order_id)


@router.post("/v1/orders/{order_id}/claim")
async def claim_order(
    order_id: str, body: ClaimBody, request: Request, auth: AuthedKey = Depends(require_key)
):
    return await request.app.state.payments.claim(auth.rec, order_id, body.utr)
