from dataclasses import dataclass

from fastapi import Request

from .errors import ApiError
from .keys import KeyRecord
from .plans import Plan
from .security import safe_equals


@dataclass
class AuthedKey:
    rec: KeyRecord
    plan: Plan


def _extract_key(request: Request) -> str | None:
    header = request.headers.get("x-api-key", "").strip()
    if header:
        return header
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


async def require_key(request: Request) -> AuthedKey:
    st = request.app.state
    raw = _extract_key(request)
    if not raw:
        raise ApiError(
            401,
            "api_key_required",
            "Send your API key in the X-API-Key header (or Authorization: Bearer <key>). "
            "Get one with POST /v1/keys.",
        )
    rec = await st.keys.lookup(raw)
    if rec is None or not rec.active:
        raise ApiError(401, "invalid_api_key", "This API key is invalid or has been revoked.")
    ok, retry = st.limiter.allow(f"key:{rec.id}", st.settings.rate_limit_per_minute, 60)
    if not ok:
        raise ApiError(
            429,
            "rate_limited",
            "Too many requests. Slow down.",
            headers={"Retry-After": str(retry)},
            retry_after=retry,
        )
    await st.keys.touch(rec.id)
    return AuthedKey(rec=rec, plan=st.keys.effective_plan(rec))


async def require_admin(request: Request) -> None:
    configured = request.app.state.settings.admin_token
    if not configured:
        raise ApiError(503, "admin_disabled", "Set ADMIN_TOKEN on the server to enable admin endpoints.")
    given = request.headers.get("x-admin-token", "")
    if not given or not safe_equals(given, configured):
        ok, retry = request.app.state.limiter.allow(f"adminfail:{client_ip(request)}", 10, 60)
        if not ok:
            raise ApiError(429, "too_many_attempts", "Too many wrong admin tokens.",
                           headers={"Retry-After": str(retry)})
        raise ApiError(401, "bad_admin_token", "Missing or wrong X-Admin-Token.")


def client_ip(request: Request) -> str:
    from .netguard import real_ip

    return real_ip(request)
