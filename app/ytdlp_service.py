"""yt-dlp wrapper: search, track info, stream-URL resolving, playlists.

No YouTube API key is used anywhere. Resolved stream URLs and search results
are cached and identical concurrent requests share one yt-dlp run, which is
what makes repeat plays near-instant.
"""

import asyncio
import base64
import logging
import re
import shutil
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp

from .config import Settings
from .errors import MediaError
from .mirrors import Mirrors
from .utils import fmt_duration

log = logging.getLogger("ytapi.ytdlp")

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LIST_ID_FULL_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_PATH_PREFIXES = ("/shorts/", "/embed/", "/live/", "/v/")


def _youtube_url(value: str):
    """Parse value as a URL, but only if its host is really YouTube."""
    value = (value or "").strip()
    if not value or any(c.isspace() for c in value):
        return None
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host in _YT_HOSTS or host in _SHORT_HOSTS:
        return parsed, host
    return None


def parse_video_ref(value: str) -> str | None:
    value = (value or "").strip()
    if VIDEO_ID_RE.match(value):
        return value
    found = _youtube_url(value)
    if not found:
        return None
    parsed, host = found
    candidate = None
    if host in _SHORT_HOSTS:
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif parsed.path == "/watch":
        candidate = (parse_qs(parsed.query).get("v") or [None])[0]
    else:
        for prefix in _PATH_PREFIXES:
            if parsed.path.startswith(prefix):
                candidate = parsed.path[len(prefix):].split("/")[0]
                break
    return candidate if candidate and VIDEO_ID_RE.match(candidate) else None


def parse_list_ref(value: str) -> str | None:
    value = (value or "").strip()
    if LIST_ID_FULL_RE.match(value):
        return value
    found = _youtube_url(value)
    if not found:
        return None
    candidate = (parse_qs(found[0].query).get("list") or [None])[0]
    return candidate if candidate and LIST_ID_FULL_RE.match(candidate) else None


# With the PO-token generator running (see Dockerfile), the web-style clients work first; the rest are fallbacks.
# YouTube checks each player client differently: when one is told to sign in, the next often is not.
PLAYER_CLIENTS = ("mweb", "web_safari", "tv", "web", "android_vr", "ios")
BLOCKED = ("sign in to confirm", "not a bot", "sign in", "cookies", "po token", "requested format is not available")

AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"
VIDEO_FORMAT = (
    "best[ext=mp4][height<=720][vcodec!=none][acodec!=none]/"
    "best[height<=720][vcodec!=none][acodec!=none]/best"
)


@dataclass
class StreamInfo:
    video_id: str
    kind: str
    url: str
    headers: dict
    ext: str
    title: str
    duration: int
    thumbnail: str
    channel: str
    expires_at: float
    filesize: int | None = None
    extra: dict = field(default_factory=dict)

    def track(self) -> dict:
        return _track(
            self.video_id, self.title, self.duration, self.thumbnail, self.channel
        )


def _track(video_id: str, title: str, duration, thumbnail: str | None, channel: str | None,
           views: int | None = None) -> dict:
    duration = int(duration or 0)
    return {
        "id": video_id,
        "title": title or "Unknown title",
        "duration": duration,
        "duration_min": fmt_duration(duration),
        "thumbnail": thumbnail or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "channel": channel or "",
        "link": f"https://www.youtube.com/watch?v={video_id}",
        "views": views,
    }


def _entry_to_track(entry: dict) -> dict | None:
    vid = entry.get("id")
    if not vid or not VIDEO_ID_RE.match(str(vid)):
        return None
    thumbs = entry.get("thumbnails") or []
    thumb = None
    if thumbs and isinstance(thumbs[-1], dict):
        thumb = thumbs[-1].get("url")
    return _track(
        vid,
        entry.get("title") or "",
        entry.get("duration"),
        (thumb or "").split("?", 1)[0] or None,
        entry.get("channel") or entry.get("uploader"),
        entry.get("view_count"),
    )


def map_error(exc: Exception) -> MediaError:
    text = str(exc)
    low = text.lower()
    if "confirm your age" in low or ("age" in low and "restricted" in low):
        return MediaError(422, "video_age_restricted", "This video is age restricted.")
    if "sign in to confirm" in low or "not a bot" in low:
        return MediaError(
            503,
            "youtube_bot_check",
            "YouTube asked this server to prove it is not a bot. Set YTDLP_COOKIES_FILE "
            "or YTDLP_PROXY on the API server.",
        )
    if "private video" in low:
        return MediaError(404, "video_private", "This video is private.")
    if any(s in low for s in ("video unavailable", "not available", "has been removed", "does not exist")):
        return MediaError(404, "video_unavailable", "This video is not available.")
    if "429" in low or "too many requests" in low:
        return MediaError(503, "youtube_rate_limited", "YouTube is rate limiting this server.")
    return MediaError(502, "extraction_failed", "Could not get this video from YouTube.")


class _TTLCache:
    def __init__(self, max_items: int = 5000):
        self.max_items = max_items
        self._d: OrderedDict[object, tuple[float, object]] = OrderedDict()

    def get(self, key):
        item = self._d.get(key)
        if not item:
            return None
        if item[0] <= time.time():
            self._d.pop(key, None)
            return None
        return item[1]

    def set(self, key, value, expires_at: float) -> None:
        self._d[key] = (expires_at, value)
        self._d.move_to_end(key)
        while len(self._d) > self.max_items:
            self._d.popitem(last=False)

    def pop(self, key) -> None:
        self._d.pop(key, None)


class Extractor:
    def __init__(self, settings: Settings):
        self.s = settings
        self.pool = ThreadPoolExecutor(max_workers=settings.ytdlp_workers, thread_name_prefix="ytdlp")
        self._streams = _TTLCache()
        self._searches = _TTLCache(2000)
        self._inflight: dict[object, asyncio.Future] = {}
        self._cookie_copy: str | None = None
        self.mirrors = Mirrors() if settings.mirror_fallback else None
        # yt-dlp writes cookies back on exit; work on a private copy.
        dst = Path(settings.data_dir) / "cookies.txt"
        if settings.ytdlp_cookies_file and Path(settings.ytdlp_cookies_file).is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(settings.ytdlp_cookies_file, dst)
            self._cookie_copy = str(dst)
        elif settings.ytdlp_cookies_b64.strip() or settings.ytdlp_cookies_text.strip():
            try:
                text = (base64.b64decode(settings.ytdlp_cookies_b64.strip()).decode("utf8")
                        if settings.ytdlp_cookies_b64.strip() else settings.ytdlp_cookies_text.replace("\\n", "\n"))
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(text, encoding="utf8")
                self._cookie_copy = str(dst)
            except Exception:
                log.exception("YTDLP_COOKIES_B64 / YTDLP_COOKIES_TEXT could not be read; continuing without cookies")

    # ── yt-dlp options ────────────────────────────────────────────────────
    def _opts(self, **extra) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 15,
            "retries": 2,
            "extractor_retries": 2,
            "geo_bypass": True,
            "js_runtimes": {name: {} for name in self.s.js_runtime_list} or {"deno": {}},
        }
        if self._cookie_copy:
            opts["cookiefile"] = self._cookie_copy
        if self.s.ytdlp_proxy:
            opts["proxy"] = self.s.ytdlp_proxy
        if self.s.player_client_list:
            opts["extractor_args"] = {"youtube": {"player_client": self.s.player_client_list}}
        opts.update(extra)
        return opts

    # ── plumbing ──────────────────────────────────────────────────────────
    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self.pool, fn, *args), self.s.extract_timeout_seconds
            )
        except asyncio.TimeoutError:
            raise MediaError(504, "extraction_timeout", "YouTube took too long to respond.")
        except MediaError:
            raise
        except Exception as exc:  # yt-dlp raises DownloadError / ExtractorError
            log.warning("yt-dlp failure: %s", str(exc)[:300])
            raise map_error(exc)

    async def _once(self, key, factory):
        """Run factory() once for identical concurrent requests."""
        fut = self._inflight.get(key)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = asyncio.ensure_future(factory())
        self._inflight[key] = fut

        def _done(f: asyncio.Future) -> None:
            self._inflight.pop(key, None)
            if not f.cancelled():
                f.exception()  # mark retrieved so asyncio does not warn

        fut.add_done_callback(_done)
        return await asyncio.shield(fut)

    # ── search ────────────────────────────────────────────────────────────
    def _search_blocking(self, query: str, limit: int) -> list[dict]:  # raw yt-dlp entries
        with yt_dlp.YoutubeDL(self._opts(extract_flat=True)) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        return list((info or {}).get("entries") or [])

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        query = " ".join((query or "").split())
        if not query:
            raise MediaError(422, "empty_query", "q is required")
        limit = max(1, min(int(limit), 25))
        key = ("search", query.lower(), limit)
        cached = self._searches.get(key)
        if cached is not None:
            return cached

        async def work():
            entries = await self._run(self._search_blocking, query, limit)
            tracks = [t for t in (_entry_to_track(e or {}) for e in entries) if t]
            self._searches.set(key, tracks, time.time() + self.s.search_cache_seconds)
            return tracks

        return await self._once(key, work)

    # ── single video ──────────────────────────────────────────────────────
    def _stream_blocking(self, video_id: str, kind: str) -> dict:
        fmt = AUDIO_FORMAT if kind == "audio" else VIDEO_FORMAT
        url = f"https://www.youtube.com/watch?v={video_id}"
        # a client list chosen by the owner is used as given; otherwise try them one by one
        clients = [[c] for c in PLAYER_CLIENTS] if not self.s.player_client_list else [self.s.player_client_list]
        last: Exception | None = None
        for client in clients:
            args = {"extractor_args": {"youtube": {"player_client": client}}}
            try:
                with yt_dlp.YoutubeDL(self._opts(format=fmt, **args)) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info and (info.get("url") or info.get("formats") or info.get("requested_formats")):
                    return info
            except Exception as e:
                if not any(k in str(e).lower() for k in BLOCKED):
                    raise  # private, removed, age-restricted...: another client will not change that
                last = e
        if last is not None:
            raise last
        raise MediaError(502, "extraction_failed", "Could not get this video from YouTube.")

    def _to_stream_info(self, video_id: str, kind: str, info: dict) -> StreamInfo:
        if not info:
            raise MediaError(502, "extraction_failed", "Could not get this video from YouTube.")
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise MediaError(422, "live_not_supported", "Live streams are not supported.")
        url = info.get("url")
        headers = info.get("http_headers") or {}
        ext = info.get("ext") or ""
        size = info.get("filesize") or info.get("filesize_approx")
        if not url:
            fmts = info.get("requested_formats") or info.get("formats") or []
            want_audio = kind == "audio"
            pick = None
            for f in fmts:
                if want_audio and f.get("vcodec") in (None, "none") and f.get("url"):
                    pick = f
                if not want_audio and f.get("acodec") not in (None, "none") and f.get("url"):
                    pick = f
            pick = pick or next((f for f in fmts if f.get("url")), None)
            if not pick:
                raise MediaError(502, "no_stream_url", "YouTube returned no playable stream.")
            url = pick["url"]
            headers = pick.get("http_headers") or headers
            ext = pick.get("ext") or ext
            size = pick.get("filesize") or pick.get("filesize_approx") or size
        duration = int(info.get("duration") or 0)
        if self.s.max_duration_seconds and duration > self.s.max_duration_seconds:
            raise MediaError(
                422, "too_long", f"Video is longer than {self.s.max_duration_seconds} seconds."
            )
        now = time.time()
        expires = now + self.s.url_cache_seconds
        try:
            exp_param = parse_qs(urlparse(url).query).get("expire")
            if exp_param:
                expires = min(expires, float(exp_param[0]) - 120)
        except (ValueError, TypeError):
            pass
        thumbs = info.get("thumbnails") or []
        thumb = info.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None)
        return StreamInfo(
            video_id=video_id,
            kind=kind,
            url=url,
            headers={k: v for k, v in headers.items() if k.lower() not in ("range", "accept-encoding")},
            ext=ext,
            title=info.get("title") or "",
            duration=duration,
            thumbnail=(thumb or "").split("?", 1)[0] or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            channel=info.get("channel") or info.get("uploader") or "",
            expires_at=expires,
            filesize=size,
        )

    async def stream_info(self, video_id: str, kind: str = "audio") -> StreamInfo:
        if not VIDEO_ID_RE.match(video_id):
            raise MediaError(422, "invalid_video_id", "Not a valid YouTube video id.")
        key = ("stream", video_id, kind)
        cached = self._streams.get(key)
        if cached is not None:
            return cached

        async def work():
            try:
                raw = await self._run(self._stream_blocking, video_id, kind)
            except MediaError as e:
                # YouTube is blocking this host: let the public mirrors answer instead of failing the song
                if e.code not in ("youtube_bot_check", "extraction_failed", "youtube_rate_limited", "extraction_timeout") or self.mirrors is None:
                    raise
                raw = await self.mirrors.stream(video_id, kind)
                if raw is None:
                    raise e
                log.warning("served %s from a public mirror (%s)", video_id, e.code)
            si = self._to_stream_info(video_id, kind, raw)
            self._streams.set(key, si, si.expires_at)
            return si

        return await self._once(key, work)

    def invalidate(self, video_id: str, kind: str) -> None:
        self._streams.pop(("stream", video_id, kind))

    async def track(self, video_id: str) -> dict:
        cached = self._streams.get(("stream", video_id, "audio"))
        if cached is not None:
            return cached.track()
        return (await self.stream_info(video_id, "audio")).track()

    async def resolve(self, ref: str) -> str:
        """Video id / URL / free-text query -> video id."""
        vid = parse_video_ref(ref)
        if vid:
            return vid
        results = await self.search(ref, 1)
        if not results:
            raise MediaError(404, "no_results", "No results for that search.")
        return results[0]["id"]

    # ── playlists ─────────────────────────────────────────────────────────
    def _playlist_blocking(self, list_id: str, limit: int) -> dict:
        opts = self._opts(extract_flat=True, noplaylist=False, playlistend=limit)
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"https://www.youtube.com/playlist?list={list_id}", download=False)

    async def playlist(self, list_id: str, limit: int) -> dict:
        limit = max(1, min(int(limit), 200))
        key = ("playlist", list_id, limit)
        cached = self._searches.get(key)
        if cached is not None:
            return cached
        info = await self._run(self._playlist_blocking, list_id, limit)
        tracks = [t for t in (_entry_to_track(e or {}) for e in (info or {}).get("entries") or []) if t]
        result = {"id": list_id, "title": (info or {}).get("title") or "", "tracks": tracks[:limit]}
        self._searches.set(key, result, time.time() + self.s.search_cache_seconds)
        return result

    # ── related songs (YouTube Mix / radio) ───────────────────────────────
    def _related_blocking(self, video_id: str, limit: int) -> list[dict]:
        opts = self._opts(extract_flat=True, noplaylist=False, playlistend=limit + 1)
        url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return list((info or {}).get("entries") or [])

    async def related(self, video_id: str, limit: int = 25) -> list[dict]:
        """Songs YouTube's own radio plays after this one. Metadata only, never counted."""
        limit = max(1, min(int(limit), 50))
        key = ("related", video_id, limit)
        cached = self._searches.get(key)
        if cached is not None:
            return cached

        async def work():
            entries = await self._run(self._related_blocking, video_id, limit)
            seen, tracks = {video_id}, []
            for e in entries:
                t = _entry_to_track(e or {})
                if t and t["id"] not in seen:
                    seen.add(t["id"])
                    tracks.append(t)
            tracks = tracks[:limit]
            self._searches.set(key, tracks, time.time() + self.s.search_cache_seconds)
            return tracks

        return await self._once(key, work)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self.mirrors is not None:
            try:
                asyncio.get_running_loop().create_task(self.mirrors.close())
            except RuntimeError:
                pass
