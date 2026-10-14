import sqlite3

from app.catalog import Catalog


def song(i):
    return {"id": f"v{i:010d}", "title": f"Song {i}", "channel": "Artist", "duration": 100 + i,
            "thumbnail": "https://t/x.jpg", "link": f"https://www.youtube.com/watch?v=v{i:010d}"}


def test_new_file_starts_after_200_and_no_duplicates(tmp_path):
    c = Catalog(str(tmp_path))
    assert c.add([song(i) for i in range(450)]) == 450
    assert c.add([song(5), song(449)]) == 0
    sh = c.shards()
    assert [s["songs"] for s in sh] == [200, 200, 50] and [s["full"] for s in sh] == [True, True, False]
    c2 = Catalog(str(tmp_path))  # a restart keeps filling the last file and still knows every id
    assert c2.add([song(3), song(450)]) == 1 and c2.shards()[-1]["songs"] == 51


def test_snapshot_is_a_readable_sqlite_file(tmp_path):
    c = Catalog(str(tmp_path))
    c.add([song(1), {"id": "x", "title": ""}])  # a song without a title is skipped
    snap = c.snapshot(1)
    row = sqlite3.connect(snap).execute("SELECT title, artist, duration FROM songs").fetchall()
    assert row == [("Song 1", "Artist", 101)] and c.snapshot(9) is None


async def test_search_fills_catalog_and_admin_and_developer_gates(api):
    api.st.catalog.add([song(7)])
    r = await api.http.get("/admin/catalog", headers=api.admin)
    assert r.json()["shards"][0]["songs"] == 1
    d = await api.http.get("/admin/catalog/1", headers=api.admin)
    assert d.status_code == 200 and d.content.startswith(b"SQLite format 3")
    assert (await api.http.get("/admin/catalog/1")).status_code in (401, 403)
    assert (await api.http.get("/admin/catalog/5", headers=api.admin)).status_code == 404


async def test_extractor_reports_search_results_to_the_catalog(tmp_path, monkeypatch):
    from app import ytdlp_service as ys
    from tests.conftest import make_settings

    class YDL:
        def __init__(self, o): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, u, download=False):
            return {"entries": [{"id": "abcdefghijk", "title": "Pal Pal", "duration": 200, "channel": "Afusic"}]}

    monkeypatch.setattr(ys.yt_dlp, "YoutubeDL", YDL)
    e = ys.Extractor(make_settings(tmp_path))
    c = Catalog(str(tmp_path))
    e.on_tracks = c.add
    await e.search("pal pal")
    e.pool.shutdown(wait=True)
    assert c.shards()[0]["songs"] == 1
