"""Last-resort stream source: public Piped and Invidious servers.

YouTube blocks many hosting IPs ("sign in to confirm you're not a bot"). These servers read YouTube from their own
addresses and hand back a playable link, so the API can still answer. Nothing to configure; edit the lists to taste.
A failing server rests for a few minutes and the next one is tried inside the same request.
"""

import logging
import random
import time

import httpx

log = logging.getLogger("ytapi.mirrors")

PIPED = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.private.coffee",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi.nosebs.ru",
    "https://pipedapi.reallyaweso.me",
]
INVIDIOUS = [
    "https://inv.nadeko.net",
    "https://yewtu.be",
    "https://invidious.nerdvpn.de",
    "https://invidious.privacyredirect.com",
    "https://iv.ggtyler.dev",
]
COOLDOWN = 300.0
TRIES = 5


class Mirrors:
    def __init__(self, *, piped=None, invidious=None, client: httpx.AsyncClient | None = None,
                 clock=time.monotonic, shuffle=random.shuffle):
        self.servers = [("piped", u.rstrip("/")) for u in (PIPED if piped is None else piped)] + \
                       [("invidious", u.rstrip("/")) for u in (INVIDIOUS if invidious is None else invidious)]
        self.http = client or httpx.AsyncClient(timeout=httpx.Timeout(9.0, connect=5.0), follow_redirects=True,
                                                headers={"User-Agent": "Mozilla/5.0 (compatible; music-api)"})
        self._rest: dict[str, float] = {}
        self._good: str | None = None
        self.clock, self._shuffle = clock, shuffle

    def _order(self):
        now = self.clock()
        up = [s for s in self.servers if self._rest.get(s[1], 0) <= now]
        self._shuffle(up)
        up.sort(key=lambda s: s[1] != self._good)
        return (up or sorted(self.servers, key=lambda s: self._rest.get(s[1], 0)))[:TRIES]

    @staticmethod
    def _abs(base: str, url: str) -> str:
        return url if url.startswith("http") else base + url

    async def stream(self, video_id: str, kind: str) -> dict | None:
        """A dict shaped like yt-dlp's answer (url, title, duration, ...), or None when no server could help."""
        for server, base in self._order():
            try:
                if server == "piped":
                    r = await self.http.get(f"{base}/streams/{video_id}")
                else:
                    r = await self.http.get(f"{base}/api/v1/videos/{video_id}", params={"local": "true"})
                r.raise_for_status()
                d = r.json()
                if not d.get("title") or d.get("error"):
                    raise ValueError("empty answer")
                if d.get("livestream") or d.get("liveNow"):
                    return None
                url, ext = self._pick(server, base, d, kind)
                if not url:
                    raise ValueError("no stream")
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as e:
                self._rest[base] = self.clock() + COOLDOWN
                log.info("mirror %s failed: %s", base, str(e)[:80])
                continue
            self._good = base
            self._rest.pop(base, None)
            if server == "piped":
                return {"url": url, "ext": ext, "title": d.get("title"), "duration": d.get("duration"),
                        "thumbnail": d.get("thumbnailUrl"), "uploader": d.get("uploader")}
            return {"url": url, "ext": ext, "title": d.get("title"), "duration": d.get("lengthSeconds"),
                    "uploader": d.get("author")}
        return None

    def _pick(self, server: str, base: str, d: dict, kind: str):
        if server == "piped":
            if kind == "video":
                muxed = [s for s in d.get("videoStreams") or [] if not s.get("videoOnly") and s.get("url")]
                muxed = [s for s in muxed if 0 < int(s.get("height") or 0) <= 720] or muxed
                if muxed:
                    return max(muxed, key=lambda s: int(s.get("height") or 0))["url"], "mp4"
            audio = [s for s in d.get("audioStreams") or [] if s.get("url")]
            audio.sort(key=lambda s: ("mp4" in str(s.get("mimeType")), int(s.get("bitrate") or 0)), reverse=True)
            return (audio[0]["url"], "m4a" if "mp4" in str(audio[0].get("mimeType")) else "webm") if audio else (None, "")
        if kind == "video":
            muxed = [f for f in d.get("formatStreams") or [] if f.get("url")]
            if muxed:
                return self._abs(base, muxed[-1]["url"]), "mp4"
        audio = [f for f in d.get("adaptiveFormats") or [] if str(f.get("type", "")).startswith("audio") and f.get("url")]
        audio.sort(key=lambda f: ("mp4" in str(f.get("type")), int(f.get("bitrate") or 0)), reverse=True)
        return (self._abs(base, audio[0]["url"]), "m4a" if "mp4" in str(audio[0].get("type")) else "webm") if audio else (None, "")

    async def close(self) -> None:
        await self.http.aclose()
