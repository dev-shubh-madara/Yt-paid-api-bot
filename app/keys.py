import re
import sqlite3
import time
from dataclasses import dataclass

from .config import Settings
from .db import Database
from .errors import ApiError
from .plans import FREE, PLANS, Plan
from .security import hash_key, new_api_key, seal_key, unseal_key
from .utils import Clock

OWNER_RE = re.compile(r"^[\w.@:+\-]{3,64}$")


@dataclass
class KeyRecord:
    id: int
    owner: str
    name: str
    plan: str
    plan_expires_at: int | None
    active: bool
    created_at: int
    last_used_at: int | None
    key_prefix: str
    total_plays: int

    def public(self, clock: Clock, plan: Plan) -> dict:
        return {
            "id": self.id,
            "owner": self.owner,
            "name": self.name,
            "key_prefix": self.key_prefix,
            "plan": plan.id,
            "stored_plan": self.plan,
            "plan_expires_at": clock.iso(self.plan_expires_at),
            "active": self.active,
            "created_at": clock.iso(self.created_at),
            "last_used_at": clock.iso(self.last_used_at),
            "total_plays": self.total_plays,
        }


def _rec(row) -> KeyRecord:
    return KeyRecord(
        id=row["id"],
        owner=row["owner"],
        name=row["name"],
        plan=row["plan"],
        plan_expires_at=row["plan_expires_at"],
        active=bool(row["active"]),
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        key_prefix=row["key_prefix"],
        total_plays=row["total_plays"],
    )


class KeyService:
    def __init__(self, db: Database, clock: Clock, settings: Settings):
        self.db = db
        self.clock = clock
        self.settings = settings
        self._touched: dict[int, float] = {}
        self.secret = ""  # set at start-up; encrypts the stored copy of each key

    # ── creation / lookup ─────────────────────────────────────────────────
    async def create(self, owner: str, name: str | None, ip: str | None) -> tuple[str, KeyRecord]:
        owner = (owner or "").strip().lower()
        if not OWNER_RE.match(owner):
            raise ApiError(
                422,
                "invalid_owner",
                "owner must be 3-64 characters: letters, digits and . _ @ : + - "
                "(use your Telegram ID, @username or email).",
            )
        name = (name or "default").strip()[:40] or "default"
        raw = new_api_key()
        try:
            res = await self.db.execute(
                "INSERT INTO api_keys(key_hash, key_prefix, owner, name, created_at, created_ip, key_enc)"
                " VALUES(?,?,?,?,?,?,?)",
                (hash_key(raw), raw[:8], owner, name, self.clock.now_ts(), ip, self._seal(raw)),
            )
        except sqlite3.IntegrityError:
            raise ApiError(
                409,
                "key_exists",
                "This owner already has an active API key. Ask the admin to rotate it if you lost it.",
            )
        return raw, await self.get(res.lastrowid)  # type: ignore[arg-type]

    async def create_for_google(self, canon_email: str, ip: str | None) -> tuple[str, KeyRecord]:
        """One key per Google account, for ever. Revoked accounts stay blocked."""
        owner = "g:" + canon_email
        row = await self.db.fetchone(
            "SELECT active FROM api_keys WHERE owner=? ORDER BY id DESC LIMIT 1", (owner,)
        )
        if row and row["active"]:
            raise ApiError(
                409, "key_exists",
                "This Google account already has an API key. Use 'Rotate key' if you lost it.",
            )
        if row:
            raise ApiError(403, "key_revoked", "The key for this account was revoked. Contact the admin.")
        return await self.create(owner, "google", ip)

    async def active_by_owner(self, owner: str) -> KeyRecord | None:
        row = await self.db.fetchone("SELECT * FROM api_keys WHERE owner=? AND active=1", (owner,))
        return _rec(row) if row else None

    async def lookup(self, raw: str) -> KeyRecord | None:
        row = await self.db.fetchone("SELECT * FROM api_keys WHERE key_hash=?", (hash_key(raw),))
        return _rec(row) if row else None

    async def get(self, key_id: int) -> KeyRecord | None:
        row = await self.db.fetchone("SELECT * FROM api_keys WHERE id=?", (key_id,))
        return _rec(row) if row else None

    async def is_active(self, key_id: int) -> bool:
        row = await self.db.fetchone("SELECT active FROM api_keys WHERE id=?", (key_id,))
        return bool(row and row["active"])

    async def touch(self, key_id: int) -> None:
        """Remember last use, but write at most once a minute per key."""
        now = time.monotonic()
        if now - self._touched.get(key_id, -1e9) < 60:
            return
        self._touched[key_id] = now
        await self.db.execute(
            "UPDATE api_keys SET last_used_at=? WHERE id=?", (self.clock.now_ts(), key_id)
        )

    # ── plans ─────────────────────────────────────────────────────────────
    def effective_plan(self, rec: KeyRecord) -> Plan:
        plan = PLANS.get(rec.plan, FREE)
        if plan.id != "free" and rec.plan_expires_at is not None:
            if rec.plan_expires_at <= self.clock.now_ts():
                return FREE
        return plan

    async def set_plan(self, key_id: int, plan_id: str, days: int | None) -> KeyRecord:
        if plan_id not in PLANS:
            raise ApiError(422, "unknown_plan", f"plan must be one of {sorted(PLANS)}")
        expires = None
        if plan_id != "free":
            expires = self.clock.now_ts() + (days or self.settings.plan_duration_days) * 86400
        res = await self.db.execute(
            "UPDATE api_keys SET plan=?, plan_expires_at=? WHERE id=?", (plan_id, expires, key_id)
        )
        if res.rowcount == 0:
            raise ApiError(404, "key_not_found", "no such key")
        return await self.get(key_id)  # type: ignore[return-value]

    async def set_active(self, key_id: int, active: bool) -> KeyRecord:
        try:
            res = await self.db.execute(
                "UPDATE api_keys SET active=? WHERE id=?", (1 if active else 0, key_id)
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, "key_exists", "another active key already exists for this owner")
        if res.rowcount == 0:
            raise ApiError(404, "key_not_found", "no such key")
        return await self.get(key_id)  # type: ignore[return-value]

    def _seal(self, raw: str) -> str | None:
        return seal_key(self.secret, raw) if self.secret else None

    async def reveal(self, key_id: int) -> str | None:
        """The owner's current key, or None for keys made before recovery existed (rotate once to enable it)."""
        row = await self.db.fetchone("SELECT key_enc FROM api_keys WHERE id=? AND active=1", (key_id,))
        return unseal_key(self.secret, row["key_enc"]) if row else None

    async def rotate(self, key_id: int) -> tuple[str, KeyRecord]:
        raw = new_api_key()
        res = await self.db.execute(
            "UPDATE api_keys SET key_hash=?, key_prefix=?, key_enc=? WHERE id=?",
            (hash_key(raw), raw[:8], self._seal(raw), key_id),
        )
        if res.rowcount == 0:
            raise ApiError(404, "key_not_found", "no such key")
        return raw, await self.get(key_id)  # type: ignore[return-value]

    async def list(self, owner: str | None, limit: int) -> list[KeyRecord]:
        if owner:
            rows = await self.db.fetchall(
                "SELECT * FROM api_keys WHERE owner LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{owner.lower()}%", limit),
            )
        else:
            rows = await self.db.fetchall("SELECT * FROM api_keys ORDER BY id DESC LIMIT ?", (limit,))
        return [_rec(r) for r in rows]

    # ── daily usage ───────────────────────────────────────────────────────
    async def used_today(self, key_id: int) -> int:
        row = await self.db.fetchone(
            "SELECT count FROM usage WHERE key_id=? AND day=?", (key_id, self.clock.today())
        )
        return int(row["count"]) if row else 0

    async def consume(self, key_id: int, limit: int) -> tuple[bool, int]:
        """Atomically add one play. Returns (ok, used_after). Never exceeds limit."""
        day = self.clock.today()
        async with self.db.tx() as t:
            await t.execute("INSERT OR IGNORE INTO usage(key_id, day, count) VALUES(?,?,0)", (key_id, day))
            res = await t.execute(
                "UPDATE usage SET count=count+1 WHERE key_id=? AND day=? AND count<?",
                (key_id, day, limit),
            )
            row = await t.fetchone("SELECT count FROM usage WHERE key_id=? AND day=?", (key_id, day))
            used = int(row["count"])
            if res.rowcount == 1:
                await t.execute(
                    "UPDATE api_keys SET total_plays=total_plays+1, last_used_at=? WHERE id=?",
                    (self.clock.now_ts(), key_id),
                )
                return True, used
            return False, used

    async def reset_usage(self, key_id: int) -> None:
        await self.db.execute(
            "DELETE FROM usage WHERE key_id=? AND day=?", (key_id, self.clock.today())
        )

    async def usage_info(self, rec: KeyRecord) -> dict:
        plan = self.effective_plan(rec)
        used = await self.used_today(rec.id)
        return {
            "plan": plan.id,
            "plan_name": plan.name,
            "daily_limit": plan.daily_limit,
            "used_today": used,
            "remaining_today": max(0, plan.daily_limit - used),
            "resets_at": self.clock.iso(self.clock.next_reset_ts()),
            "plan_expires_at": self.clock.iso(rec.plan_expires_at) if plan.id != "free" else None,
        }
