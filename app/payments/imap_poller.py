"""Reads new mail over IMAP and feeds it to PaymentService.

Read-only: the mailbox is opened with EXAMINE and messages are fetched with
BODY.PEEK, so nothing is marked read, moved or deleted. Progress is tracked by
IMAP UID (stored in the database) and every e-mail is also de-duplicated by
Message-ID, so a restart never double-credits a payment.
"""

import asyncio
import imaplib
import logging
from datetime import timedelta

from ..config import Settings
from ..db import Database
from ..utils import Clock
from .service import PaymentService

log = logging.getLogger("ytapi.imap")


class ImapPoller:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        service: PaymentService,
        clock: Clock,
        imap_factory=imaplib.IMAP4_SSL,
    ):
        self.s = settings
        self.db = db
        self.service = service
        self.clock = clock
        self._factory = imap_factory
        self._conn = None
        self._uidvalidity: str | None = None
        self._last_uid: int | None = None
        self.last_ok_at: int | None = None
        self.last_error: str | None = None

    # ── blocking IMAP work (runs in a thread) ─────────────────────────────
    def _connect(self):
        conn = self._factory(self.s.imap_host, self.s.imap_port, timeout=30)  # never hang forever
        conn.login(self.s.imap_user, self.s.imap_password)
        return conn

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def _fetch_new(self, last_uid: int | None, uidvalidity_known: str | None):
        """Returns (uidvalidity, [(uid, raw_bytes)], initial, high_uid)."""
        if self._conn is None:
            self._conn = self._connect()
        conn = self._conn
        typ, _ = conn.select(self.s.imap_folder, readonly=True)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"cannot open folder {self.s.imap_folder}")
        validity = None
        typ, data = conn.response("UIDVALIDITY")
        if data and data[0]:
            validity = data[0].decode() if isinstance(data[0], bytes) else str(data[0])

        initial = last_uid is None or bool(
            validity and uidvalidity_known and validity != uidvalidity_known
        )
        high = 0
        if initial:
            since = (self.clock.now() - timedelta(days=self.s.imap_lookback_days)).strftime("%d-%b-%Y")
            typ, data = conn.uid("SEARCH", None, "SINCE", since)
            floor = 0
            typ_all, data_all = conn.uid("SEARCH", None, "ALL")
            if typ_all == "OK" and data_all and data_all[0]:
                high = max(int(u) for u in data_all[0].split())
        else:
            typ, data = conn.uid("SEARCH", None, "UID", f"{last_uid + 1}:*")
            floor = last_uid
        if typ != "OK":
            raise imaplib.IMAP4.error("search failed")
        uids = sorted(int(u) for u in (data[0] or b"").split())
        uids = [u for u in uids if u > floor]  # "n:*" always returns the newest mail, even if old

        out = []
        for uid in uids[-200:]:
            typ, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not parts:
                continue
            for part in parts:
                if isinstance(part, tuple) and len(part) > 1:
                    out.append((uid, part[1]))
                    break
        return validity, out, initial, high

    # ── async side ────────────────────────────────────────────────────────
    async def _load_state(self) -> None:
        row = await self.db.fetchone("SELECT v FROM settings WHERE k='imap_state'")
        if row:
            try:
                validity, uid = row["v"].split(":", 1)
                self._uidvalidity, self._last_uid = validity or None, int(uid)
            except ValueError:
                pass

    async def _save_state(self) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO settings(k, v) VALUES('imap_state', ?)",
            (f"{self._uidvalidity or ''}:{self._last_uid or 0}",),
        )

    async def poll_once(self) -> int:
        """Fetch and process new mail. Returns how many e-mails were handled."""
        try:
            validity, batch, initial, high = await asyncio.to_thread(
                self._fetch_new, self._last_uid, self._uidvalidity
            )
        except Exception:
            self._drop()
            raise
        if validity and validity != self._uidvalidity:
            if self._uidvalidity is not None:
                self._last_uid = None  # mailbox was rebuilt: old UIDs mean nothing
            self._uidvalidity = validity
        handled, failed = 0, False
        for uid, raw in batch:
            try:
                await self.service.handle_email(raw)
            except Exception:
                log.exception("failed to process e-mail uid=%s", uid)
                failed = True  # keep _last_uid behind it so it is retried next poll
                continue
            handled += 1
            if not failed:
                self._last_uid = max(self._last_uid or 0, uid)
        if initial and not failed:
            # First run: skip everything older than the lookback window for good.
            self._last_uid = max(self._last_uid or 0, high)
        await self._save_state()
        self.last_ok_at = self.clock.now_ts()
        self.last_error = None
        return handled

    async def run_forever(self) -> None:
        await self._load_state()
        await self.db.execute(
            "DELETE FROM processed_emails WHERE processed_at < ?", (self.clock.now_ts() - 90 * 86400,)
        )
        delay = self.s.imap_poll_seconds
        log.info("IMAP poller started for %s", self.s.imap_user)
        while True:
            try:
                n = await self.poll_once()
                if n:
                    log.info("processed %d new e-mail(s)", n)
                delay = self.s.imap_poll_seconds
            except asyncio.CancelledError:
                self._drop()
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                delay = min(max(delay * 2, 30), 300)
                log.warning("IMAP poll failed (%s); retrying in %ss", self.last_error, delay)
            await asyncio.sleep(delay)
