import time

from fastapi import APIRouter, Depends, Query, Request

from ..deps import AuthedKey, require_key
from ..errors import ApiError
from ..security import sign_token, verify_token
from ..streaming import proxy_stream
from ..ytdlp_service import LIST_ID_FULL_RE, parse_list_ref, parse_video_ref

router = APIRouter()


def _pick_video_id(id: str | None, url: str | None) -> str | None:
    for candidate in (id, url):
        if candidate:
            vid = parse_video_ref(candidate)
            if vid:
                return vid
    return None


@router.get("/v1/search")
async def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(10, ge=1, le=25),
    auth: AuthedKey = Depends(require_key),
):
    results = await request.app.state.extractor.search(q, limit)
    return {"query": q, "count": len(results), "results": results}


@router.get("/v1/track")
async def track(
    request: Request,
    q: str | None = Query(None, max_length=200, description="free-text search, takes the top result"),
    id: str | None = Query(None, description="YouTube video id"),
    url: str | None = Query(None, description="YouTube link"),
    auth: AuthedKey = Depends(require_key),
):
    ex = request.app.state.extractor
    vid = _pick_video_id(id, url)
    if vid is None:
        if not q:
            raise ApiError(422, "missing_query", "Provide one of: id, url or q.")
        vid = await ex.resolve(q)
        results = await ex.search(q, 1)
        if results and results[0]["id"] == vid:
            return results[0]  # search results already carry full metadata
    return await ex.track(vid)


@router.get("/v1/playlist")
async def playlist(
    request: Request,
    url: str | None = Query(None, description="YouTube playlist link"),
    id: str | None = Query(None, description="YouTube playlist id"),
    limit: int = Query(25, ge=1, le=200),
    auth: AuthedKey = Depends(require_key),
):
    list_id = None
    if id and LIST_ID_FULL_RE.match(id):
        list_id = id
    elif url:
        list_id = parse_list_ref(url)
    if not list_id:
        raise ApiError(422, "invalid_playlist", "Provide a valid YouTube playlist id or link.")
    return await request.app.state.extractor.playlist(list_id, limit)


@router.get("/v1/play")
async def play(
    request: Request,
    q: str | None = Query(None, max_length=200, description="free-text search, plays the top result"),
    id: str | None = Query(None, description="YouTube video id"),
    url: str | None = Query(None, description="YouTube link"),
    type: str = Query("audio", pattern="^(audio|video)$"),
    auth: AuthedKey = Depends(require_key),
):
    """Resolve a song and return a stream link. This is the call that counts
    against the daily limit (once per song, not per byte streamed)."""
    st = request.app.state
    plan = auth.plan
    used = await st.keys.used_today(auth.rec.id)
    if used >= plan.daily_limit:
        raise _limit_error(st, auth, used)

    vid = _pick_video_id(id, url)
    if vid is None:
        if not q:
            raise ApiError(422, "missing_query", "Provide one of: id, url or q.")
        vid = await st.extractor.resolve(q)
    info = await st.extractor.stream_info(vid, type)  # failures here are NOT counted

    dedupe_key = (auth.rec.id, vid, type)
    now = time.monotonic()
    last = st.recent_plays.get(dedupe_key)
    if last is not None and now - last < st.settings.play_dedupe_seconds:
        counted = False  # a quick retry of the same song is free
    else:
        ok, used = await st.keys.consume(auth.rec.id, plan.daily_limit)
        if not ok:
            raise _limit_error(st, auth, used)
        st.recent_plays[dedupe_key] = now
        if len(st.recent_plays) > 20000:
            cutoff = now - st.settings.play_dedupe_seconds
            st.recent_plays = {k: v for k, v in st.recent_plays.items() if v >= cutoff}
        counted = True

    used = await st.keys.used_today(auth.rec.id)
    exp = st.clock.now_ts() + st.settings.token_ttl_seconds
    token = sign_token(st.token_secret, {"v": vid, "k": type, "e": exp, "i": auth.rec.id})
    base = (st.settings.public_base_url or str(request.base_url)).rstrip("/")
    return {
        **info.track(),
        "type": type,
        "stream_url": f"{base}/v1/stream/{token}",
        "expires_at": st.clock.iso(exp),
        "counted": counted,
        "usage": {
            "plan": plan.id,
            "daily_limit": plan.daily_limit,
            "used_today": used,
            "remaining_today": max(0, plan.daily_limit - used),
        },
    }


def _limit_error(st, auth: AuthedKey, used: int) -> ApiError:
    retry = max(1, st.clock.next_reset_ts() - st.clock.now_ts())
    return ApiError(
        429,
        "daily_limit_reached",
        f"Daily limit of {auth.plan.daily_limit} songs reached for the {auth.plan.name} plan. "
        "It resets at midnight, or upgrade via POST /v1/orders.",
        headers={"Retry-After": str(retry)},
        plan=auth.plan.id,
        daily_limit=auth.plan.daily_limit,
        used_today=used,
        resets_at=st.clock.iso(st.clock.next_reset_ts()),
    )


@router.head("/v1/stream/{token}", include_in_schema=False)
@router.get("/v1/stream/{token}")
async def stream(token: str, request: Request):
    """Signed link returned by /v1/play. No API key needed, and no extra
    count, so players can seek and reconnect freely."""
    st = request.app.state
    payload = verify_token(st.token_secret, token)
    if not payload:
        raise ApiError(401, "invalid_token", "This stream link is not valid.")
    if int(payload.get("e", 0)) < st.clock.now_ts():
        raise ApiError(410, "token_expired", "This stream link has expired. Call /v1/play again.")
    if not await st.keys.is_active(int(payload.get("i", 0))):
        raise ApiError(401, "key_revoked", "The API key behind this link was revoked.")
    info = await st.extractor.stream_info(str(payload["v"]), str(payload["k"]))
    return await proxy_stream(request, st.http, st.extractor, info)
