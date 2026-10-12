"""The bot's Youtube.py against a REAL running API server over real HTTP.

A uvicorn server runs in a thread; the client talks to it with aiohttp exactly
as the Telegram bot would. Streams are also decoded by the real ffmpeg, which
is what the voice-chat player uses.
"""

import asyncio
import importlib.util
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
import uvicorn

from app.main import create_app
from tests.conftest import (
    VID_A, VID_B, VID_BAD, VID_C, VID_LIVE, FakeExtractor, free_port, make_settings, media_bytes,
)

REAL_ID = "REALAUDIO_1"
CLIENT_PATH = Path(__file__).resolve().parents[1] / "bot_client" / "Youtube.py"


def load_client(api_url: str, api_key: str, tmp_path, mode="url"):
    os.environ["API_URL"] = api_url
    os.environ["API_KEY"] = api_key
    os.environ["DOWNLOAD_DIR"] = str(tmp_path / "downloads")
    os.environ["STREAM_MODE"] = mode
    spec = importlib.util.spec_from_file_location("bot_youtube_client", CLIENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bot_youtube_client"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def real_m4a(tmp_path_factory):
    out = tmp_path_factory.mktemp("audio") / "tone.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
         "-c:a", "aac", "-movflags", "+faststart", str(out)],
        check=True, timeout=60,
    )
    return out.read_bytes()


class LiveServer:
    def __init__(self, tmp_path, media_server, real_m4a):
        self.settings = make_settings(tmp_path / "srv")
        self.app = create_app(self.settings)
        self.port = free_port()
        media_server.real[REAL_ID] = real_m4a
        self.media_server = media_server
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started and hasattr(self.app.state, "extractor"):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("server did not start")
        self.app.state.extractor.shutdown()
        self.app.state.extractor = FakeExtractor(self.settings, self.media_server.port)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def live(tmp_path, media_server, real_m4a):
    srv = LiveServer(tmp_path, media_server, real_m4a)
    srv.start()
    yield srv
    srv.stop()


async def issue_key(live: LiveServer, owner="botowner") -> str:
    import aiohttp

    async with aiohttp.ClientSession() as s:
        async with s.post(f"{live.url}/v1/keys", json={"owner": owner}) as r:
            assert r.status == 201
            return (await r.json())["api_key"]


@pytest_asyncio.fixture
async def client(live, tmp_path):
    key = await issue_key(live)
    mod = load_client(live.url, key, tmp_path)
    yield mod
    if mod._session and not mod._session.closed:
        await mod._session.close()


async def usage(live: LiveServer, mod) -> dict:
    return (await mod._api_get("/v1/me"))["usage"]


# ── metadata calls ────────────────────────────────────────────────────────
async def test_details_track_title_duration_thumbnail(client, live):
    yt = client.YouTubeAPI()
    title, dur_min, dur_sec, thumb, vidid = await yt.details(VID_A, True)
    assert (title, vidid) == (f"Song {VID_A}", VID_A) and dur_sec == 215 and dur_min == "3:35"
    assert "?" not in thumb
    assert await yt.title(VID_A, True) == f"Song {VID_A}"
    assert await yt.duration(VID_A, True) == "3:35"
    assert (await yt.thumbnail(VID_A, True)) == thumb
    d, tid = await yt.track(f"https://youtu.be/{VID_B}?si=x")
    assert tid == VID_B and set(d) >= {"title", "link", "vidid", "duration_min", "thumb"}
    d, tid = await yt.track("some song name")  # free text
    assert tid == VID_A and d["title"].startswith("some song name")
    assert d["link"] == f"https://www.youtube.com/watch?v={VID_A}"
    assert (await usage(live, client))["used_today"] == 0  # metadata never costs a song


async def test_slider_and_playlist_fill_the_cache(client, live):
    yt = client.YouTubeAPI()
    title, dur_min, thumb, vid = await yt.slider("lofi", 1)
    assert vid == VID_B and title == "lofi #1"
    with pytest.raises(IndexError):
        await yt.slider("lofi", 99)
    ids = await yt.playlist("PLabcdefghijk", 25, 123, True)
    assert ids == [VID_A, VID_B, VID_C]
    ex = live.app.state.extractor
    ex.stream_calls.clear()
    # details of playlist items now come from the cache: zero API / yt-dlp work
    for vid in ids:
        await yt.details(vid, True)
    assert ex.stream_calls == []


async def test_exists_and_url_helpers(client):
    yt = client.YouTubeAPI()
    assert await yt.exists("https://youtu.be/dQw4w9WgXcQ") and not await yt.exists("hello world")
    from pyrogram.enums import MessageEntityType

    entity = SimpleNamespace(type=MessageEntityType.URL, offset=5, length=28, url=None)
    msg = SimpleNamespace(text="play https://youtu.be/dQw4w9WgXcQ", caption=None, entities=[entity],
                          caption_entities=None, reply_to_message=None)
    assert await yt.url(msg) == "https://youtu.be/dQw4w9WgXcQ"
    msg2 = SimpleNamespace(text="no link", caption=None, entities=None, caption_entities=None,
                           reply_to_message=SimpleNamespace(text="listen: https://youtube.com/watch?v=dQw4w9WgXcQ).",
                                                            caption=None, entities=None, caption_entities=None,
                                                            reply_to_message=None))
    assert await yt.url(msg2) == "https://youtube.com/watch?v=dQw4w9WgXcQ"


def test_video_id_helpers(client):
    assert client.youtube_video_id("https://music.youtube.com/watch?v=dQw4w9WgXcQ&x=1") == "dQw4w9WgXcQ"
    assert client.youtube_video_id("https://evil.com/watch?v=dQw4w9WgXcQ") == ""
    assert client.youtube_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert client.time_to_seconds("1:02:03") == 3723 and client.time_to_seconds(None) == 0


# ── playing ───────────────────────────────────────────────────────────────
async def test_download_returns_a_stream_url_that_streams(client, live):
    import aiohttp

    yt = client.YouTubeAPI()
    path, direct = await yt.download(VID_A, None, videoid=True)
    assert direct is True and path.startswith(live.url + "/v1/stream/")
    # the bot stores this in its queue and decides how to play it with substring checks
    assert not any(w in path for w in ("vid_", "live_", "index_"))
    async with aiohttp.ClientSession() as s:
        async with s.get(path) as r:
            assert r.status == 200 and await r.read() == media_bytes(VID_A)
        async with s.get(path, headers={"Range": "bytes=10-19"}) as r:
            assert r.status == 206 and await r.read() == media_bytes(VID_A)[10:20]
    assert (await usage(live, client))["used_today"] == 1


async def test_download_by_link_and_video_type(client, live):
    yt = client.YouTubeAPI()
    path, _ = await yt.download(f"https://youtu.be/{VID_B}", None, video=True)
    assert "/v1/stream/" in path
    assert (VID_B, "video") in live.app.state.extractor.stream_calls
    n, link = await yt.video(VID_C, True)
    assert n == 1 and "/v1/stream/" in link


async def test_file_mode_downloads_once(live, tmp_path):
    key = await issue_key(live, "filemode")
    mod = load_client(live.url, key, tmp_path, mode="file")
    yt = mod.YouTubeAPI()
    path, direct = await yt.download(VID_A, None, videoid=True)
    assert direct is True and Path(path).read_bytes() == media_bytes(VID_A) and path.endswith(".m4a")
    path2, _ = await yt.download(VID_A, None, videoid=True)
    assert path2 == path
    assert (await usage(live, mod))["used_today"] == 1  # second call reused the file
    await mod._session.close()


async def test_song_command_helpers(client, live):
    audio = await client.download_song(f"https://www.youtube.com/watch?v={VID_A}")
    assert audio and Path(audio).read_bytes() == media_bytes(VID_A) and audio.endswith(".m4a")
    video = await client.download_video(f"https://www.youtube.com/watch?v={VID_B}")
    assert video and video.endswith(".mp4")
    assert await client.download_song("not a link") is None
    assert await client.download_song(f"https://youtu.be/{VID_BAD}") is None  # unavailable video -> None, no crash


# ── failure modes the bot must survive ────────────────────────────────────
async def test_daily_limit_surfaces_as_a_clear_error(client, live):
    yt = client.YouTubeAPI()
    kid = (await live.app.state.keys.lookup(os.environ["API_KEY"])).id
    await live.app.state.db.execute(
        "INSERT INTO usage(key_id, day, count) VALUES(?,?,?)", (kid, live.app.state.clock.today(), 1000))
    with pytest.raises(client.DailyLimitReached) as e:
        await yt.download(VID_A, None, videoid=True)
    assert e.value.status == 429 and e.value.payload["daily_limit"] == 1000
    n, msg = await yt.video(VID_A, True)
    assert n == 0 and "limit" in msg.lower()
    assert await client.download_song(f"https://youtu.be/{VID_A}") is None


async def test_unavailable_and_live_videos_raise_or_return_zero(client):
    yt = client.YouTubeAPI()
    with pytest.raises(client.YouTubeAPIError) as e:
        await yt.download(VID_BAD, None, videoid=True)
    assert e.value.code == "video_unavailable"
    n, msg = await yt.video(VID_LIVE, True)
    assert n == 0 and "live" in msg.lower()
    with pytest.raises(client.YouTubeAPIError):
        await yt.download("not a video", None)


async def test_bad_or_missing_key(live, tmp_path):
    mod = load_client(live.url, "myk_wrong", tmp_path)
    with pytest.raises(mod.InvalidAPIKey):
        await mod.YouTubeAPI().details(VID_A, True)
    mod = load_client(live.url, "", tmp_path)
    with pytest.raises(mod.InvalidAPIKey):
        await mod.YouTubeAPI().details(VID_A, True)
    await mod._session.close() if mod._session else None


async def test_unreachable_api_gives_a_clean_error(tmp_path):
    mod = load_client(f"http://127.0.0.1:{free_port()}", "myk_x", tmp_path)
    start = time.perf_counter()
    with pytest.raises(mod.YouTubeAPIError) as e:
        await mod.YouTubeAPI().details(VID_A, True)
    assert e.value.code == "unreachable" and time.perf_counter() - start < 15
    n, _ = await mod.YouTubeAPI().video(VID_A, True)
    assert n == 0
    if mod._session:
        await mod._session.close()


# ── the real player: ffmpeg decodes our stream and can seek ───────────────
def _clean_env():
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    env["no_proxy"] = "127.0.0.1,localhost"
    return env


async def test_ffmpeg_decodes_the_stream_and_seeks(client, live):
    yt = client.YouTubeAPI()
    url, _ = await yt.download(REAL_ID, None, videoid=True)

    def probe():
        return subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name",
             "-of", "default=nw=1", url],
            capture_output=True, text=True, timeout=60, env=_clean_env())

    out = await asyncio.to_thread(probe)
    assert out.returncode == 0, out.stderr
    assert "codec_name=aac" in out.stdout
    duration = float([l for l in out.stdout.splitlines() if l.startswith("duration=")][0].split("=")[1])
    assert 5.5 < duration < 6.5

    def decode(extra):
        return subprocess.run(
            ["ffmpeg", "-v", "error", *extra, "-i", url, "-f", "null", "-"],
            capture_output=True, text=True, timeout=60, env=_clean_env())

    full = await asyncio.to_thread(decode, [])
    assert full.returncode == 0 and full.stderr.strip() == ""
    # the same options the bot's seek feature uses: -ss <t> -to <end>
    seek = await asyncio.to_thread(decode, ["-ss", "3", "-to", "5"])
    assert seek.returncode == 0 and seek.stderr.strip() == ""
    hits = [h for h in live.media_server.hits if h["vid"] == REAL_ID]
    assert any(h["range"] for h in hits), "ffmpeg should have used HTTP Range requests"
    assert (await usage(live, client))["used_today"] == 1  # probing + decoding + seeking = still one song
