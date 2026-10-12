"""Test harness.

* FakeMediaServer: a real HTTP server (uvicorn in a thread) that serves bytes
  with Range support, so the streaming proxy is tested over real sockets.
* FakeExtractor: the real Extractor with only the three network calls into
  yt-dlp replaced by canned yt-dlp-shaped data. Everything else (caching,
  de-duplication, stream-info building, error mapping) is the production code.
"""

import hashlib
import os
import socket
import threading
import time
from email.message import EmailMessage
from email.utils import format_datetime, formatdate
from datetime import datetime, timezone

os.environ["YTAPI_NO_AUTOAPP"] = "1"

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI, Request, Response

from app.config import Settings
from app.main import create_app
from app.ytdlp_service import Extractor

MEDIA_SIZE = 300_000


def media_bytes(video_id: str, size: int = MEDIA_SIZE) -> bytes:
    seed = hashlib.sha256(video_id.encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeMediaServer:
    def __init__(self):
        self.hits: list[dict] = []
        self.fail_next_with: int | None = None  # make the next request fail (e.g. 403)
        self.real: dict[str, bytes] = {}  # video id -> real media bytes (instead of filler)
        self.port = free_port()
        api = FastAPI()

        @api.get("/media/{vid}")
        async def media(vid: str, request: Request):
            self.hits.append({"vid": vid, "range": request.headers.get("range"), "ua": request.headers.get("user-agent")})
            if self.fail_next_with:
                code, self.fail_next_with = self.fail_next_with, None
                return Response(status_code=code)
            data = self.real.get(vid) or media_bytes(vid)
            rng = request.headers.get("range")
            headers = {"accept-ranges": "bytes", "content-type": "audio/mp4"}
            if rng and rng.startswith("bytes="):
                start_s, _, end_s = rng[6:].partition("-")
                start = int(start_s or 0)
                end = int(end_s) if end_s else len(data) - 1
                end = min(end, len(data) - 1)
                chunk = data[start : end + 1]
                headers["content-range"] = f"bytes {start}-{end}/{len(data)}"
                headers["content-length"] = str(len(chunk))
                return Response(chunk, status_code=206, headers=headers)
            headers["content-length"] = str(len(data))
            return Response(data, status_code=200, headers=headers)

        config = uvicorn.Config(api, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("fake media server did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture(scope="session")
def media_server():
    srv = FakeMediaServer()
    srv.start()
    yield srv
    srv.stop()


# Valid-shaped 11 character video ids
VID_A = "aaaaaaaaaaa"
VID_B = "bbbbbbbbbbb"
VID_C = "ccccccccccc"
VID_LIVE = "LIVE_LIVE_1"
VID_BAD = "BADBADBAD_1"


class FakeExtractor(Extractor):
    """Real Extractor, fake yt-dlp network calls."""

    def __init__(self, settings, media_port: int):
        super().__init__(settings)
        self.media_port = media_port
        self.stream_calls: list[tuple[str, str]] = []
        self.search_calls: list[str] = []
        self.delay = 0.0
        self.expire_param = True

    def _stream_blocking(self, video_id: str, kind: str) -> dict:
        self.stream_calls.append((video_id, kind))
        if self.delay:
            time.sleep(self.delay)
        if video_id == VID_BAD:
            raise RuntimeError("ERROR: [youtube] BADBADBAD_1: Video unavailable")
        if video_id == VID_LIVE:
            return {"id": video_id, "title": "Live", "is_live": True, "live_status": "is_live"}
        expire = f"?expire={int(time.time()) + 21000}" if self.expire_param else ""
        return {
            "id": video_id,
            "title": f"Song {video_id}",
            "duration": 215,
            "channel": "Test Channel",
            "thumbnails": [{"url": f"https://i.ytimg.com/vi/{video_id}/hq.jpg?x=1"}],
            "url": f"http://127.0.0.1:{self.media_port}/media/{video_id}{expire}",
            "ext": "m4a",
            "filesize": MEDIA_SIZE,
            "http_headers": {"User-Agent": "FakeUA/1.0", "Accept-Encoding": "gzip", "Range": "bytes=0-1"},
        }

    def _search_blocking(self, query: str, limit: int) -> list[dict]:
        self.search_calls.append(query)
        ids = [VID_A, VID_B, VID_C][:limit]
        return [
            {
                "id": vid,
                "title": f"{query} #{i}",
                "duration": 200 + i,
                "channel": "Chan",
                "thumbnails": [{"url": f"https://i.ytimg.com/vi/{vid}/x.jpg?sqp=1"}],
                "view_count": 1000 + i,
            }
            for i, vid in enumerate(ids)
        ] + ([{"id": None, "title": "junk entry"}] if limit > 3 else [])

    def _playlist_blocking(self, list_id: str, limit: int) -> dict:
        return {
            "title": "Test Playlist",
            "entries": [{"id": v, "title": f"P-{v}", "duration": 100} for v in (VID_A, VID_B, VID_C)][:limit],
        }


def make_settings(tmp_path, **over) -> Settings:
    base = dict(
        data_dir=str(tmp_path),
        admin_token="admintoken",
        token_secret="test-secret",
        timezone="Asia/Kolkata",
        upi_id="madara@fam",
        payee_name="Madara",
        payment_senders="fampay.in",
        key_creations_per_ip_per_hour=1000,
        rate_limit_per_minute=100000,
        play_dedupe_seconds=60,
        _env_file=None,
    )
    base.update(over)
    return Settings(**base)


class Api:
    """Small helper around the in-process app."""

    def __init__(self, app, client: httpx.AsyncClient):
        self.app = app
        self.http = client
        self.st = app.state
        self.admin = {"X-Admin-Token": "admintoken"}

    async def new_key(self, owner="user1", **kw):
        if len(owner) < 3:
            owner += "-own"
        r = await self.http.post("/v1/keys", json={"owner": owner}, **kw)
        assert r.status_code == 201, r.text
        return r.json()["api_key"]

    @staticmethod
    def h(key):
        return {"X-API-Key": key}

    async def key_id(self, key):
        return (await self.st.keys.lookup(key)).id


@pytest_asyncio.fixture
async def api(tmp_path, media_server):
    settings = make_settings(tmp_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        app.state.extractor.shutdown()
        app.state.extractor = FakeExtractor(settings, media_server.port)
        media_server.hits.clear()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield Api(app, client)


@pytest.fixture
def make_api(tmp_path, media_server):
    """Factory for tests that need custom settings / IMAP double."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _make(imap_factory=None, **over):
        settings = make_settings(tmp_path, **over)
        app = create_app(settings, imap_factory=imap_factory)
        async with app.router.lifespan_context(app):
            app.state.extractor.shutdown()
            app.state.extractor = FakeExtractor(settings, media_server.port)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
                yield Api(app, client)

    return _make


# ── e-mail helpers ────────────────────────────────────────────────────────
def build_email(
    *,
    subject="Payment received",
    body="You received ₹29.00 from Rahul Kumar. UTR 412345678901. Note: MYT8F3K2Q",
    sender="no-reply@fampay.in",
    message_id=None,
    auth="mx.google.com; dkim=pass header.i=@fampay.in header.s=s1; spf=pass; dmarc=pass (p=NONE) header.from=fampay.in",
    html=False,
    date=None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = f"FamPay <{sender}>"
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id or f"<{hashlib.md5((subject + body).encode()).hexdigest()}@mail>"
    msg["Date"] = format_datetime(date or datetime.now(timezone.utc))
    if auth is not None:
        msg["Authentication-Results"] = auth
    if html:
        msg.set_content("fallback")
        msg.add_alternative(f"<html><body><p>{body}</p></body></html>", subtype="html")
    else:
        msg.set_content(body)
    return bytes(msg)
