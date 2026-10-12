"""Checks of the yt-dlp layer that need no network."""

import pytest
import yt_dlp

from app.errors import MediaError
from app.ytdlp_service import (
    Extractor,
    map_error,
    parse_list_ref,
    parse_video_ref,
)
from tests.conftest import make_settings


@pytest.mark.parametrize("value,expected", [
    ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ&t=10", "dQw4w9WgXcQ"),
    ("https://music.youtube.com/watch?v=dQw4w9WgXcQ&si=abc", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    ("youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("  https://youtu.be/dQw4w9WgXcQ  ", "dQw4w9WgXcQ"),
])
def test_video_refs_accepted(value, expected):
    assert parse_video_ref(value) == expected


@pytest.mark.parametrize("value", [
    "", "never gonna give you up", "https://evil.com/watch?v=dQw4w9WgXcQ",
    "https://notyoutube.com/watch?v=dQw4w9WgXcQ", "https://youtube.com.evil.io/watch?v=dQw4w9WgXcQ",
    "dQw4w9WgXc", "dQw4w9WgXcQQ", "http://169.254.169.254/latest/meta-data",
])
def test_video_refs_rejected(value):
    assert parse_video_ref(value) is None


def test_playlist_refs():
    assert parse_list_ref("https://www.youtube.com/playlist?list=PLabcdefghijk") == "PLabcdefghijk"
    assert parse_list_ref("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabcdefghijk&index=2") == "PLabcdefghijk"
    assert parse_list_ref("PLabcdefghijk") == "PLabcdefghijk"
    assert parse_list_ref("https://evil.com/?list=../../etc") is None


def test_options_are_accepted_by_the_real_yt_dlp(tmp_path):
    ex = Extractor(make_settings(tmp_path, ytdlp_js_runtimes="deno,node", ytdlp_player_clients="tv,android"))
    with yt_dlp.YoutubeDL(ex._opts(format="bestaudio")) as ydl:
        assert set(ydl.params["js_runtimes"]) == {"deno", "node"}
        assert ydl.params["extractor_args"] == {"youtube": {"player_client": ["tv", "android"]}}
        assert ydl.params["skip_download"] is True


def test_cookie_file_is_copied_not_modified(tmp_path):
    src = tmp_path / "c.txt"
    src.write_text("# Netscape HTTP Cookie File\n")
    ex = Extractor(make_settings(tmp_path / "data", ytdlp_cookies_file=str(src)))
    opts = ex._opts()
    assert opts["cookiefile"] != str(src) and opts["cookiefile"].endswith("cookies.txt")


@pytest.mark.parametrize("message,status,code", [
    ("ERROR: [youtube] x: Sign in to confirm you’re not a bot", 503, "youtube_bot_check"),
    ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", 404, "video_private"),
    ("ERROR: [youtube] x: Video unavailable", 404, "video_unavailable"),
    ("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate", 422, "video_age_restricted"),
    ("HTTP Error 429: Too Many Requests", 503, "youtube_rate_limited"),
    ("something totally unexpected", 502, "extraction_failed"),
])
def test_error_mapping(message, status, code):
    err = map_error(RuntimeError(message))
    assert isinstance(err, MediaError) and (err.status, err.code) == (status, code)


def test_stream_info_from_realistic_ytdlp_dicts(tmp_path):
    ex = Extractor(make_settings(tmp_path))
    # single selected format (the normal case)
    info = ex._to_stream_info("dQw4w9WgXcQ", "audio", {
        "title": "T", "duration": 61, "url": "https://rr1.googlevideo.com/videoplayback?expire=9999999999&x=1",
        "ext": "m4a", "http_headers": {"User-Agent": "UA", "Range": "bytes=0-1"}, "channel": "C",
        "thumbnails": [{"url": "https://i.ytimg.com/a.jpg?s=1"}, {"url": "https://i.ytimg.com/b.jpg?s=2"}],
    })
    assert info.url.startswith("https://rr1.googlevideo.com") and info.headers == {"User-Agent": "UA"}
    assert info.thumbnail == "https://i.ytimg.com/b.jpg" and info.track()["duration_min"] == "1:01"
    # merged formats: no top-level url, pick the right stream
    merged = ex._to_stream_info("dQw4w9WgXcQ", "audio", {
        "title": "T", "duration": 5,
        "requested_formats": [
            {"url": "https://v/video", "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
            {"url": "https://v/audio", "vcodec": "none", "acodec": "mp4a", "ext": "m4a"},
        ],
    })
    assert merged.url == "https://v/audio"
    with pytest.raises(MediaError) as e:
        ex._to_stream_info("dQw4w9WgXcQ", "audio", {"title": "T", "formats": []})
    assert e.value.code == "no_stream_url"
    with pytest.raises(MediaError) as e:
        ex._to_stream_info("dQw4w9WgXcQ", "audio", {"is_live": True})
    assert e.value.code == "live_not_supported"


def test_max_duration_limit(tmp_path):
    ex = Extractor(make_settings(tmp_path, max_duration_seconds=600))
    with pytest.raises(MediaError) as e:
        ex._to_stream_info("dQw4w9WgXcQ", "audio", {"url": "https://x/y", "duration": 601, "title": "t"})
    assert e.value.code == "too_long"
