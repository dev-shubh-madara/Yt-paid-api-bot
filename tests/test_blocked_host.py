"""YouTube blocks the host: client rotation, public mirrors and cookies from an environment variable."""

import base64

import httpx
import pytest

from app import ytdlp_service as ys
from app.errors import MediaError
from app.mirrors import Mirrors
from tests.conftest import make_settings

VID = "dQw4w9WgXcQ"
BOT = "ERROR: Sign in to confirm you're not a bot"


class FakeYDL:
    clients_seen: list = []
    answers: dict = {}

    def __init__(self, opts):
        self.client = (opts.get("extractor_args", {}).get("youtube", {}).get("player_client") or ["default"])[0]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        FakeYDL.clients_seen.append(self.client)
        ans = FakeYDL.answers.get(self.client, BOT)
        if isinstance(ans, str):
            raise Exception(ans)
        return ans


@pytest.fixture
def ex(tmp_path, monkeypatch):
    FakeYDL.clients_seen, FakeYDL.answers = [], {}
    monkeypatch.setattr(ys.yt_dlp, "YoutubeDL", FakeYDL)
    e = ys.Extractor(make_settings(tmp_path))
    yield e
    e.pool.shutdown(wait=False)


def piped_ok(req):
    return httpx.Response(200, json={"title": "Pal Pal", "duration": 200, "uploader": "A",
                                     "audioStreams": [{"url": "https://px/a", "bitrate": 1, "mimeType": "audio/mp4"}]})


async def test_next_player_client_is_tried_when_one_is_blocked(ex):
    FakeYDL.answers = {"web_safari": {"url": "https://g/x", "title": "T", "duration": 10, "ext": "m4a"}}
    si = await ex.stream_info(VID, "audio")
    assert si.url == "https://g/x" and FakeYDL.clients_seen == ["mweb", "web_safari"]


async def test_mirror_answers_when_every_client_is_blocked(ex):
    ex.mirrors = Mirrors(piped=["https://p1.test"], invidious=[], client=httpx.AsyncClient(transport=httpx.MockTransport(piped_ok)),
                         shuffle=lambda x: None)
    si = await ex.stream_info(VID, "audio")
    assert si.url == "https://px/a" and si.title == "Pal Pal" and len(FakeYDL.clients_seen) == 6


async def test_original_error_is_kept_when_mirrors_are_down_too(ex):
    ex.mirrors = Mirrors(piped=["https://p1.test"], invidious=[], client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
                         shuffle=lambda x: None)
    with pytest.raises(MediaError) as e:
        await ex.stream_info(VID, "audio")
    assert e.value.code == "youtube_bot_check"


async def test_private_video_does_not_try_other_clients_or_mirrors(ex):
    FakeYDL.answers = {"mweb": "ERROR: Private video"}
    with pytest.raises(MediaError) as e:
        await ex.stream_info(VID, "audio")
    assert e.value.code == "video_private" and FakeYDL.clients_seen == ["mweb"]


async def test_mirror_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setattr(ys.yt_dlp, "YoutubeDL", FakeYDL)
    e = ys.Extractor(make_settings(tmp_path, mirror_fallback=False))
    assert e.mirrors is None
    with pytest.raises(MediaError):
        await e.stream_info(VID, "audio")
    e.pool.shutdown(wait=False)


def test_cookies_from_base64_and_text_env(tmp_path):
    text = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"
    e = ys.Extractor(make_settings(tmp_path, ytdlp_cookies_b64=base64.b64encode(text.encode()).decode()))
    assert open(e._cookie_copy).read() == text
    e.pool.shutdown(wait=False)
    e = ys.Extractor(make_settings(tmp_path / "b", ytdlp_cookies_text=text.replace("\n", "\\n")))
    assert open(e._cookie_copy).read() == text
    e.pool.shutdown(wait=False)
    e = ys.Extractor(make_settings(tmp_path / "c", ytdlp_cookies_b64="%%%not base64%%%"))
    assert e._cookie_copy is None  # bad input never stops the server from starting
    e.pool.shutdown(wait=False)


def test_docker_image_ships_the_po_token_generator():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    docker = (root / "Dockerfile").read_text()
    assert "bgutil-ytdlp-pot-provider" in docker and "npx tsc" in docker and "start.sh" in docker
    start = (root / "start.sh").read_text()
    assert "build/main.js" in start and "--port 4416" in start and "exec uvicorn" in start
    assert "bgutil-ytdlp-pot-provider==" in (root / "requirements.txt").read_text()


async def test_breaker_skips_ytdlp_after_block_and_probes_again(ex, monkeypatch):
    ex.mirrors = Mirrors(piped=["https://p1.test"], invidious=[], client=httpx.AsyncClient(transport=httpx.MockTransport(piped_ok)),
                         shuffle=lambda x: None)
    await ex.stream_info(VID, "audio")
    first = len(FakeYDL.clients_seen)
    assert first == 6 and ex.blocked_for > 0
    await ex.stream_info("aaaaaaaaaaa", "audio")
    assert len(FakeYDL.clients_seen) == first  # yt-dlp not touched during the cool-down
    ex._blocked_until = 0
    FakeYDL.answers = {"mweb": {"url": "https://g/x", "title": "T", "duration": 10, "ext": "m4a"}}
    await ex.stream_info("bbbbbbbbbbb", "audio")
    assert ex.blocked_for == 0


async def test_user_message_has_no_operator_instructions(ex):
    ex.mirrors = None
    with pytest.raises(MediaError) as e:
        await ex.stream_info(VID, "audio")
    assert "YTDLP" not in e.value.message and e.value.code == "youtube_bot_check"


def test_cookies_and_proxy_persist_without_env(tmp_path):
    e = ys.Extractor(make_settings(tmp_path))
    with pytest.raises(MediaError):
        e.set_cookies("garbage that is not cookies at all")
    assert e.set_cookies("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n") == 1
    e.set_proxy("http://u:p@1.2.3.4:8080")
    with pytest.raises(MediaError):
        e.set_proxy("ftp://nope")
    e2 = ys.Extractor(make_settings(tmp_path))  # a restart
    o = e2._opts()
    assert o["cookiefile"].endswith("cookies.txt") and o["proxy"] == "http://u:p@1.2.3.4:8080"
    assert e2.status()["proxy"] == "http://***@1.2.3.4:8080" and "p@" not in str(e2.status())
    e2.set_proxy("")
    e2.clear_cookies()
    assert "proxy" not in e2._opts() and "cookiefile" not in e2._opts()
    e.pool.shutdown(wait=False); e2.pool.shutdown(wait=False)
