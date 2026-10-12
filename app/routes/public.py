import html
from pathlib import Path

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import FileResponse, HTMLResponse
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
        "warning": "Save this key now. It is shown only once and cannot be recovered.",
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
