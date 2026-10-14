"""Song catalog: metadata only (title, artist, YouTube id, duration, thumbnail).

Every song the API sees is remembered in SQLite files of SHARD_SIZE songs each; when one is full the next file
starts by itself. It makes searching faster and gives the owner a library of what people play. No audio is stored.
"""

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("ytapi.catalog")

SHARD_SIZE = 200
_NAME = re.compile(r"^catalog_(\d{4})\.db$")
_SCHEMA = """CREATE TABLE IF NOT EXISTS songs(
  video_id TEXT PRIMARY KEY, title TEXT NOT NULL, artist TEXT, duration INTEGER,
  thumbnail TEXT, link TEXT, added_at INTEGER NOT NULL)"""


class Catalog:
    def __init__(self, data_dir: str, shard_size: int = SHARD_SIZE):
        self.dir = Path(data_dir) / "catalog"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.size = shard_size
        self._lock = threading.Lock()
        self._known: set[str] = set()
        self._current = 1
        self._count = 0  # songs in the current shard
        for n in self.shard_numbers():
            with self._open(n) as db:
                self._known.update(r[0] for r in db.execute("SELECT video_id FROM songs"))
                self._current, self._count = n, db.execute("SELECT COUNT(*) FROM songs").fetchone()[0]

    def path(self, n: int) -> Path:
        return self.dir / f"catalog_{n:04d}.db"

    def shard_numbers(self) -> list[int]:
        return sorted(int(m.group(1)) for p in self.dir.iterdir() if (m := _NAME.match(p.name)))

    def _open(self, n: int) -> sqlite3.Connection:
        db = sqlite3.connect(self.path(n))
        db.execute(_SCHEMA)
        return db

    def add(self, tracks: list[dict]) -> int:
        """Remember songs not seen before. Returns how many were new."""
        fresh = []
        for t in tracks or []:
            vid = t.get("id")
            if vid and t.get("title") and vid not in self._known:
                fresh.append(t)
        if not fresh:
            return 0
        added = 0
        with self._lock:
            db = self._open(self._current)
            try:
                for t in fresh:
                    if t["id"] in self._known:
                        continue
                    if self._count >= self.size:  # shard is full: start the next file
                        db.commit(); db.close()
                        self._current += 1
                        self._count = 0
                        db = self._open(self._current)
                    db.execute("INSERT OR IGNORE INTO songs VALUES(?,?,?,?,?,?,?)", (
                        t["id"], t["title"], t.get("channel") or "", int(t.get("duration") or 0),
                        t.get("thumbnail") or "", t.get("link") or "", int(time.time())))
                    self._known.add(t["id"])
                    self._count += 1
                    added += 1
                db.commit()
            finally:
                db.close()
        return added

    def shards(self) -> list[dict]:
        out = []
        for n in self.shard_numbers():
            with self._open(n) as db:
                songs = db.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
            out.append({"shard": n, "file": self.path(n).name, "songs": songs,
                        "size_bytes": self.path(n).stat().st_size, "full": songs >= self.size})
        return out

    def snapshot(self, n: int) -> Path | None:
        """A consistent copy of one shard for download (None if it does not exist)."""
        if n not in self.shard_numbers():
            return None
        dst = self.dir / f".dl_{n:04d}.tmp"
        with self._lock:
            src = self._open(n)
            out = sqlite3.connect(dst)
            try:
                src.backup(out)
            finally:
                out.close(); src.close()
        return dst
