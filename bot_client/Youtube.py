"""
Drop-in replacement for MADARAMUSIC/platforms/Youtube.py.

Talks to YOUR Madara YouTube API (this repo's server). No Shruti API, no
third-party API and no YouTube API key.

Environment variables (add them to your bot's env / config):
    API_URL            Base URL of your API, e.g. https://yt-api.example.com
    API_KEY            Your API key (from POST /v1/keys)
    STREAM_MODE        "url" (default): hand the stream link straight to the
                       voice chat, playback starts instantly.
                       "file": download the song first, then play the file.
    DOWNLOAD_DIR       Where files go in "file" mode / for /song (default: downloads)

Public interface is the same as the old YouTubeAPI class, plus the module
level download_song() / download_video() that plugins/tools/song.py imports.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import urlparse

import aiohttp
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message

log = logging.getLogger("MADARAMUSIC.youtube")

try:  # the bot's config.py (optional: plain environment variables also work)
    import config as _config
except Exception:  # pragma: no cover
    _config = None


def _setting(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None and _config is not None:
        value = getattr(_config, name, None)
    return str(value if value is not None else default).strip()


DOWNLOAD_DIR = Path(_setting("DOWNLOAD_DIR", "downloads"))
STREAM_MODE = _setting("STREAM_MODE", "url").lower()


def _api_url() -> str:
    return _setting("API_URL").rstrip("/")


def _api_key() -> str:
    return _setting("API_KEY")


# ── errors ────────────────────────────────────────────────────────────────
class YouTubeAPIError(Exception):
    """The API said no (or could not be reached)."""

    def __init__(self, status: int, code: str, message: str, payload: dict | None = None):
        super().__init__(f"[{status} {code}] {message}")
        self.status = status
        self.code = code
        self.message = message
        self.payload = payload or {}


class DailyLimitReached(YouTubeAPIError):
    """The API key used up its songs for today."""


class InvalidAPIKey(YouTubeAPIError):
    """API_KEY is missing, wrong or revoked."""


# ── helpers kept from the old module (other files may import them) ────────
def time_to_seconds(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        parts = str(value).strip().split(":")
        return sum(int(p) * 60**i for i, p in enumerate(reversed(parts)))
    except (TypeError, ValueError):
        return 0


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}


def normalise_youtube_url(link: str) -> str:
    value = str(link or "").strip()
    if _ID_RE.match(value):
        return f"https://www.youtube.com/watch?v={value}"
    if value.startswith(("youtu.be/", "youtube.com/", "www.youtube.com/", "m.youtube.com/", "music.youtube.com/")):
        return "https://" + value
    return value


def youtube_video_id(link: str) -> str:
    value = normalise_youtube_url(link)
    if _ID_RE.match(value):
        return value
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    if (parsed.hostname or "").lower() not in _YT_HOSTS:
        return ""
    from urllib.parse import parse_qs

    host = (parsed.hostname or "").lower()
    candidate = ""
    if host.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif parsed.path == "/watch":
        candidate = (parse_qs(parsed.query).get("v") or [""])[0]
    else:
        for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
            if parsed.path.startswith(prefix):
                candidate = parsed.path[len(prefix):].split("/")[0]
                break
    return candidate if _ID_RE.match(candidate) else ""


# ── HTTP plumbing ─────────────────────────────────────────────────────────
_session: aiohttp.ClientSession | None = None
_session_loop: asyncio.AbstractEventLoop | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session, _session_loop
    loop = asyncio.get_running_loop()
    if _session is None or _session.closed or _session_loop is not loop:
        _session = aiohttp.ClientSession(headers={"User-Agent": "MadaraMusicBot/1.0"})
        _session_loop = loop
    return _session


async def _api_get(path: str, params: dict[str, Any] | None = None, *, timeout: float = 60) -> dict:
    base, key = _api_url(), _api_key()
    if not base:
        raise YouTubeAPIError(0, "not_configured", "API_URL is not set")
    if not key:
        raise InvalidAPIKey(0, "not_configured", "API_KEY is not set")
    clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    last: Exception | None = None
    for attempt in range(3):
        try:
            async with _get_session().get(
                f"{base}{path}",
                params=clean,
                headers={"X-API-Key": key},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {}
                if resp.status == 200:
                    return data
                code = (data or {}).get("error", "error")
                message = (data or {}).get("message", f"HTTP {resp.status}")
                if resp.status == 401:
                    raise InvalidAPIKey(resp.status, code, message, data)
                if resp.status == 429 and code == "daily_limit_reached":
                    log.warning("Madara API: daily song limit reached (%s)", message)
                    raise DailyLimitReached(resp.status, code, message, data)
                err = YouTubeAPIError(resp.status, code, message, data)
                if code in ("rate_limited", "extraction_timeout", "upstream_error") and attempt < 2:
                    last = err
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise err
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last = exc
            if attempt < 2:
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
    if isinstance(last, YouTubeAPIError):
        raise last
    raise YouTubeAPIError(0, "unreachable", f"Could not reach the API: {last!r}")


async def _save_stream(stream_url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    async with _get_session().get(
        stream_url, timeout=aiohttp.ClientTimeout(total=1800, sock_read=90)
    ) as resp:
        if resp.status != 200:
            raise YouTubeAPIError(resp.status, "stream_failed", f"stream answered HTTP {resp.status}")
        with tmp.open("wb") as out:
            async for chunk in resp.content.iter_chunked(131072):
                out.write(chunk)
    if not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise YouTubeAPIError(0, "empty_download", "downloaded file is empty")
    tmp.replace(destination)
    return destination


def _extension_for(kind: str, stream_url: str) -> str:
    return "mp4" if kind == "video" else "m4a"


async def _play(video_id: str, kind: str) -> dict:
    """Ask the API for a stream link (this is the call that counts a song)."""
    return await _api_get("/v1/play", {"id": video_id, "type": kind})


async def _download_file(video_id: str, kind: str) -> Optional[str]:
    destination = DOWNLOAD_DIR / f"{video_id}.{_extension_for(kind, '')}"
    if destination.exists() and destination.stat().st_size > 0:
        return str(destination)
    info = await _play(video_id, kind)
    return str(await _save_stream(info["stream_url"], destination))


async def download_song(link: str) -> Optional[str]:
    """Audio file for /song. Returns the path, or None on failure."""
    vid = youtube_video_id(link)
    if not vid:
        return None
    try:
        return await _download_file(vid, "audio")
    except Exception as exc:
        log.warning("download_song failed for %s: %s", vid, exc)
        return None


async def download_video(link: str) -> Optional[str]:
    vid = youtube_video_id(link)
    if not vid:
        return None
    try:
        return await _download_file(vid, "video")
    except Exception as exc:
        log.warning("download_video failed for %s: %s", vid, exc)
        return None


# ── the class the bot uses ────────────────────────────────────────────────
class YouTubeAPI:
    def __init__(self) -> None:
        self.base = "https://www.youtube.com/watch?v="
        self.regex = r"(?:youtube\.com|youtu\.be)"
        self.listbase = "https://youtube.com/playlist?list="
        self._meta: dict[str, dict] = {}

    # metadata cache: playlists and searches already return full details, so
    # the bot's follow-up details() calls cost nothing
    def _remember(self, track: dict) -> dict:
        if len(self._meta) > 3000:
            for key in list(self._meta)[:500]:
                self._meta.pop(key, None)
        self._meta[track["id"]] = track
        return track

    @staticmethod
    def _as_details(track: dict) -> dict:
        return {
            "title": track["title"],
            "link": track.get("link") or f"https://www.youtube.com/watch?v={track['id']}",
            "vidid": track["id"],
            "duration_min": track["duration_min"],
            "duration_sec": track["duration"],
            "thumb": (track.get("thumbnail") or "").split("?", 1)[0],
            "uploader": track.get("channel") or "",
        }

    async def _lookup(self, link: str, videoid: Union[bool, str, None]) -> dict:
        """Metadata for a video id, a YouTube link or a free-text search."""
        if videoid:
            ref_id, query = str(link), None
        else:
            ref_id = youtube_video_id(str(link))
            query = None if ref_id else str(link or "").strip()
        if ref_id and ref_id in self._meta:
            return self._meta[ref_id]
        if not ref_id and not query:
            raise ValueError("empty search")
        track = await _api_get("/v1/track", {"id": ref_id or None, "q": query})
        return self._remember(track)

    async def exists(self, link: str, videoid: Union[bool, str, None] = None) -> bool:
        value = self.base + str(link) if videoid else str(link or "")
        return bool(re.search(self.regex, value, re.IGNORECASE))

    async def url(self, message_1: Message) -> Optional[str]:
        messages = [message_1]
        if message_1.reply_to_message:
            messages.append(message_1.reply_to_message)
        for message in messages:
            text = message.text or message.caption or ""
            for entity in message.entities or message.caption_entities or []:
                if entity.type == MessageEntityType.TEXT_LINK and entity.url:
                    return entity.url
                if entity.type == MessageEntityType.URL:
                    return text[entity.offset : entity.offset + entity.length]
            match = re.search(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/\S+", text, re.IGNORECASE)
            if match:
                return match.group(0).rstrip(").,")
        return None

    async def details(self, link: str, videoid: Union[bool, str, None] = None):
        t = await self._lookup(link, videoid)
        return t["title"], t["duration_min"], t["duration"], (t.get("thumbnail") or "").split("?", 1)[0], t["id"]

    async def title(self, link: str, videoid: Union[bool, str, None] = None):
        return (await self.details(link, videoid))[0]

    async def duration(self, link: str, videoid: Union[bool, str, None] = None):
        return (await self.details(link, videoid))[1]

    async def thumbnail(self, link: str, videoid: Union[bool, str, None] = None):
        return (await self.details(link, videoid))[3]

    async def track(self, link: str, videoid: Union[bool, str, None] = None):
        t = await self._lookup(link, videoid)
        return self._as_details(t), t["id"]

    async def video(self, link: str, videoid: Union[bool, str, None] = None):
        """A playable video link. (0, reason) when it is not possible, e.g. live streams."""
        vid = str(link) if videoid else youtube_video_id(str(link))
        if not vid:
            return 0, "Not a YouTube video"
        try:
            info = await _play(vid, "video")
        except DailyLimitReached as exc:
            return 0, exc.message
        except YouTubeAPIError as exc:
            return 0, exc.message
        return 1, info["stream_url"]

    async def playlist(self, link: str, limit: int, user_id: Any, videoid: Union[bool, str, None] = None):
        ref = {"id": str(link)} if videoid else {"url": str(link)}
        data = await _api_get("/v1/playlist", {**ref, "limit": limit})
        ids = []
        for t in data.get("tracks", []):
            self._remember(t)
            ids.append(t["id"])
        return ids

    async def formats(self, link: str, videoid: Union[bool, str, None] = None):
        # The API serves one best-quality stream per type; there is no format menu.
        return [], (self.base + str(link) if videoid else str(link))

    async def slider(self, link: str, query_type: int, videoid: Union[bool, str, None] = None):
        query = self.base + str(link) if videoid else str(link)
        data = await _api_get("/v1/search", {"q": query, "limit": 10})
        results = data.get("results") or []
        if query_type < 0 or query_type >= len(results):
            raise IndexError("search result index out of range")
        for t in results:
            self._remember(t)
        t = results[query_type]
        return t["title"], t["duration_min"], (t.get("thumbnail") or "").split("?", 1)[0], t["id"]

    async def download(
        self,
        link: str,
        mystic: Any,
        video: Union[bool, str, None] = None,
        videoid: Union[bool, str, None] = None,
        songaudio: Union[bool, str, None] = None,
        songvideo: Union[bool, str, None] = None,
        format_id: Union[bool, str, None] = None,
        title: Union[bool, str, None] = None,
    ):
        """Returns (path_or_url, True). Raises when the song can not be played,
        so the bot's existing try/except shows its normal error message."""
        vid = str(link) if videoid else youtube_video_id(str(link))
        if not vid:
            raise YouTubeAPIError(422, "invalid_video_id", "Not a YouTube video")
        kind = "video" if (video or songvideo) else "audio"
        if songaudio or songvideo or STREAM_MODE == "file":
            return await _download_file(vid, kind), True
        info = await _play(vid, kind)
        return info["stream_url"], True


YouTube = YouTubeAPI()
