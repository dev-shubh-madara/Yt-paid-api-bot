import asyncio

import pytest

from tests.conftest import VID_A, VID_B, VID_BAD, VID_C, VID_LIVE


async def play(api, key, **params):
    params.setdefault("id", VID_A)
    return await api.http.get("/v1/play", params=params, headers=api.h(key))


async def test_play_returns_stream_url_and_counts_once(api):
    key = await api.new_key("p1")
    r = await play(api, key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == VID_A and body["title"] == f"Song {VID_A}"
    assert body["duration"] == 215 and body["duration_min"] == "3:35"
    assert body["stream_url"].startswith("http://api.test/v1/stream/")
    assert body["counted"] is True
    assert body["usage"] == {"plan": "free", "daily_limit": 1000, "used_today": 1, "remaining_today": 999}


async def test_search_track_playlist_do_not_count(api):
    key = await api.new_key("p2")
    s = (await api.http.get("/v1/search", params={"q": "lofi", "limit": 3}, headers=api.h(key))).json()
    assert s["count"] == 3 and s["results"][0]["id"] == VID_A
    assert s["results"][0]["thumbnail"].endswith("/x.jpg")  # query string stripped
    assert (await api.http.get("/v1/track", params={"q": "lofi"}, headers=api.h(key))).json()["id"] == VID_A
    t = (await api.http.get("/v1/track", params={"id": VID_B}, headers=api.h(key))).json()
    assert t["id"] == VID_B and t["title"] == f"Song {VID_B}"
    pl = (await api.http.get("/v1/playlist", params={"url": "https://www.youtube.com/playlist?list=PLabcdefghijk"},
                             headers=api.h(key))).json()
    assert [x["id"] for x in pl["tracks"]] == [VID_A, VID_B, VID_C]
    me = (await api.http.get("/v1/me", headers=api.h(key))).json()
    assert me["usage"]["used_today"] == 0


async def test_play_by_url_and_by_text_query(api):
    key = await api.new_key("p3")
    r = await play(api, key, id=None, url=f"https://youtu.be/{VID_B}?si=xyz")
    assert r.json()["id"] == VID_B
    r = await api.http.get("/v1/play", params={"q": "some song"}, headers=api.h(key))
    assert r.status_code == 200 and r.json()["id"] == VID_A  # top search hit


async def test_only_youtube_links_are_accepted(api):
    key = await api.new_key("p4")
    for bad in ["https://evil.example.com/watch?v=aaaaaaaaaaa", "http://127.0.0.1:8080/x", "file:///etc/passwd"]:
        r = await api.http.get("/v1/play", params={"url": bad}, headers=api.h(key))
        assert r.status_code == 422, bad
    assert not api.st.extractor.stream_calls  # nothing was ever fetched


async def test_video_type(api):
    key = await api.new_key("p5")
    r = await play(api, key, type="video")
    assert r.status_code == 200 and r.json()["type"] == "video"
    assert (VID_A, "video") in api.st.extractor.stream_calls
    assert (await play(api, key, type="movie")).status_code == 422


async def test_failed_extraction_is_not_counted(api):
    key = await api.new_key("p6")
    r = await play(api, key, id=VID_BAD)
    assert r.status_code == 404 and r.json()["error"] == "video_unavailable"
    r = await play(api, key, id=VID_LIVE)
    assert r.status_code == 422 and r.json()["error"] == "live_not_supported"
    assert (await api.http.get("/v1/me", headers=api.h(key))).json()["usage"]["used_today"] == 0


async def test_quick_retry_of_same_song_is_free_but_other_songs_count(api):
    key = await api.new_key("p7")
    assert (await play(api, key, id=VID_A)).json()["counted"] is True
    assert (await play(api, key, id=VID_A)).json()["counted"] is False
    assert (await play(api, key, id=VID_B)).json()["counted"] is True
    assert (await api.http.get("/v1/me", headers=api.h(key))).json()["usage"]["used_today"] == 2


async def test_free_plan_blocks_the_1001st_song(api):
    key = await api.new_key("limit-free")
    kid = await api.key_id(key)
    day = api.st.clock.today()
    await api.st.db.execute("INSERT INTO usage(key_id, day, count) VALUES(?,?,?)", (kid, day, 999))
    r = await play(api, key, id=VID_A)  # 1000th song: allowed
    assert r.status_code == 200 and r.json()["usage"]["remaining_today"] == 0
    r = await play(api, key, id=VID_B)  # 1001st: blocked
    assert r.status_code == 429
    body = r.json()
    assert body["error"] == "daily_limit_reached"
    assert body["plan"] == "free" and body["daily_limit"] == 1000 and body["used_today"] == 1000
    assert int(r.headers["retry-after"]) > 0
    # the blocked call must not have wasted a yt-dlp run
    assert (VID_B, "audio") not in api.st.extractor.stream_calls


@pytest.mark.parametrize("plan,limit", [("p2000", 2000), ("p3000", 3000), ("p4000", 4000), ("p10000", 10000)])
async def test_each_paid_plan_has_its_own_daily_limit(api, plan, limit):
    key = await api.new_key(f"owner-{plan}")
    kid = await api.key_id(key)
    r = await api.http.post(f"/admin/keys/{kid}/plan", json={"plan": plan}, headers=api.admin)
    assert r.status_code == 200
    usage = (await api.http.get("/v1/me", headers=api.h(key))).json()["usage"]
    assert usage["daily_limit"] == limit and usage["plan"] == plan
    await api.st.db.execute("INSERT INTO usage(key_id, day, count) VALUES(?,?,?)",
                            (kid, api.st.clock.today(), limit - 1))
    assert (await play(api, key, id=VID_A)).status_code == 200
    r = await play(api, key, id=VID_B)
    assert r.status_code == 429 and r.json()["daily_limit"] == limit


async def test_limit_resets_at_midnight_ist(api):
    key = await api.new_key("reset")
    kid = await api.key_id(key)
    await api.st.db.execute("INSERT INTO usage(key_id, day, count) VALUES(?,?,?)",
                            (kid, api.st.clock.today(), 1000))
    assert (await play(api, key, id=VID_A)).status_code == 429
    api.st.clock.advance(86400)
    r = await play(api, key, id=VID_A)
    assert r.status_code == 200 and r.json()["usage"]["used_today"] == 1


async def test_next_reset_is_midnight_in_configured_zone(api):
    iso = (await api.http.get("/v1/me", headers=api.h(await api.new_key("tz")))).json()["usage"]["resets_at"]
    assert iso.endswith("+05:30") and "T00:00:00" in iso


async def test_expired_paid_plan_falls_back_to_free(api):
    key = await api.new_key("expiry")
    kid = await api.key_id(key)
    await api.http.post(f"/admin/keys/{kid}/plan", json={"plan": "p4000", "days": 30}, headers=api.admin)
    assert (await api.http.get("/v1/me", headers=api.h(key))).json()["usage"]["daily_limit"] == 4000
    api.st.clock.advance(31 * 86400)
    usage = (await api.http.get("/v1/me", headers=api.h(key))).json()["usage"]
    assert usage["plan"] == "free" and usage["daily_limit"] == 1000


async def test_concurrent_plays_never_exceed_the_limit(api):
    key = await api.new_key("race")
    kid = await api.key_id(key)
    await api.st.db.execute("INSERT INTO usage(key_id, day, count) VALUES(?,?,?)",
                            (kid, api.st.clock.today(), 990))
    # 40 different songs at the same moment, only 10 slots left
    ids = [f"song{i:07d}" for i in range(40)]
    results = await asyncio.gather(*[play(api, key, id=i) for i in ids])
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 10 and codes.count(429) == 30
    assert await api.st.keys.used_today(kid) == 1000


async def test_admin_can_reset_usage_and_see_stats(api):
    key = await api.new_key("admin-reset")
    kid = await api.key_id(key)
    await play(api, key)
    stats = (await api.http.get("/admin/stats", headers=api.admin)).json()
    assert stats["plays_today"] == 1 and stats["keys_total"] == 1
    r = await api.http.post(f"/admin/keys/{kid}/reset-usage", headers=api.admin)
    assert r.json()["usage"]["used_today"] == 0
