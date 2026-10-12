import asyncio
import time

from app.security import sign_token
from tests.conftest import MEDIA_SIZE, VID_A, VID_B, media_bytes


async def get_stream_url(api, key, vid=VID_A, **extra):
    r = await api.http.get("/v1/play", params={"id": vid, **extra}, headers=api.h(key))
    assert r.status_code == 200, r.text
    return r.json()["stream_url"].replace("http://api.test", "")


async def test_full_stream_matches_source_bytes_without_api_key(api):
    key = await api.new_key("s1")
    path = await get_stream_url(api, key)
    r = await api.http.get(path)  # no key header on purpose
    assert r.status_code == 200
    assert r.content == media_bytes(VID_A) and len(r.content) == MEDIA_SIZE
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "audio/mp4"


async def test_range_requests_for_seeking(api, media_server):
    key = await api.new_key("s2")
    path = await get_stream_url(api, key)
    r = await api.http.get(path, headers={"Range": "bytes=1000-1999"})
    assert r.status_code == 206
    assert r.content == media_bytes(VID_A)[1000:2000]
    assert r.headers["content-range"] == f"bytes 1000-1999/{MEDIA_SIZE}"
    r = await api.http.get(path, headers={"Range": "bytes=299000-"})
    assert r.status_code == 206 and r.content == media_bytes(VID_A)[299000:]
    assert media_server.hits[-1]["range"] == "bytes=299000-"


async def test_head_request_returns_headers_only(api):
    key = await api.new_key("s3")
    path = await get_stream_url(api, key)
    r = await api.http.head(path)
    assert r.status_code == 200 and r.content == b""
    assert r.headers["content-length"] == str(MEDIA_SIZE)


async def test_upstream_gets_ytdlp_headers_but_not_stale_range_or_gzip(api, media_server):
    key = await api.new_key("s4")
    path = await get_stream_url(api, key)
    await api.http.get(path)
    assert media_server.hits[-1]["ua"] == "FakeUA/1.0"
    assert media_server.hits[-1]["range"] is None  # the Range from yt-dlp's header set was dropped


async def test_tampered_and_garbage_tokens_are_rejected(api):
    key = await api.new_key("s5")
    path = await get_stream_url(api, key)
    token = path.rsplit("/", 1)[1]
    flipped = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
    for bad in [flipped, "garbage", "a.b", token.split(".")[0]]:
        r = await api.http.get(f"/v1/stream/{bad}")
        assert r.status_code == 401 and r.json()["error"] == "invalid_token", bad


async def test_token_signed_with_other_secret_is_rejected(api):
    key = await api.new_key("s6")
    kid = await api.key_id(key)
    forged = sign_token("wrong-secret", {"v": VID_A, "k": "audio", "e": int(time.time()) + 999, "i": kid})
    assert (await api.http.get(f"/v1/stream/{forged}")).status_code == 401


async def test_token_expires(api):
    key = await api.new_key("s7")
    path = await get_stream_url(api, key)
    assert (await api.http.get(path)).status_code == 200
    api.st.clock.advance(api.st.settings.token_ttl_seconds + 5)
    r = await api.http.get(path)
    assert r.status_code == 410 and r.json()["error"] == "token_expired"


async def test_revoking_the_key_kills_existing_stream_links(api):
    key = await api.new_key("s8")
    kid = await api.key_id(key)
    path = await get_stream_url(api, key)
    await api.http.post(f"/admin/keys/{kid}/revoke", headers=api.admin)
    r = await api.http.get(path)
    assert r.status_code == 401 and r.json()["error"] == "key_revoked"


async def test_streaming_and_seeking_do_not_use_up_the_daily_limit(api):
    key = await api.new_key("s9")
    path = await get_stream_url(api, key)
    for rng in ["bytes=0-99", "bytes=100-199", "bytes=5000-"]:
        await api.http.get(path, headers={"Range": rng})
    assert (await api.http.get("/v1/me", headers=api.h(key))).json()["usage"]["used_today"] == 1


async def test_resolved_url_is_cached_for_repeat_plays(api):
    key = await api.new_key("s10")
    for _ in range(3):
        await get_stream_url(api, key)
    await api.http.get(await get_stream_url(api, key))
    assert api.st.extractor.stream_calls.count((VID_A, "audio")) == 1


async def test_parallel_requests_share_one_ytdlp_run(api):
    ex = api.st.extractor
    ex.delay = 0.3
    keys = [await api.new_key(f"par{i}") for i in range(5)]
    start = time.perf_counter()
    rs = await asyncio.gather(*[
        api.http.get("/v1/play", params={"id": VID_B}, headers=api.h(k)) for k in keys
    ])
    assert all(r.status_code == 200 for r in rs)
    assert ex.stream_calls.count((VID_B, "audio")) == 1
    assert time.perf_counter() - start < 1.0


async def test_stale_cached_url_is_refreshed_transparently(api, media_server):
    key = await api.new_key("s11")
    path = await get_stream_url(api, key)
    assert api.st.extractor.stream_calls.count((VID_A, "audio")) == 1
    media_server.fail_next_with = 403  # googlevideo says the cached URL expired
    r = await api.http.get(path)
    assert r.status_code == 200 and r.content == media_bytes(VID_A)
    assert api.st.extractor.stream_calls.count((VID_A, "audio")) == 2  # re-resolved once


async def test_upstream_that_keeps_failing_gives_a_clean_502(api, media_server):
    key = await api.new_key("s12")
    path = await get_stream_url(api, key)
    media_server.fail_next_with = 500
    r = await api.http.get(path)
    assert r.status_code == 502 and r.json()["error"] == "upstream_status"


async def test_url_cache_honours_the_expire_parameter(api):
    ex = api.st.extractor
    info = await ex.stream_info(VID_A, "audio")
    assert info.expires_at <= time.time() + 21000  # not the full 3300s default only: capped by expire-120
    assert info.expires_at > time.time() + 100
