"""Orders, payment matching and plan activation."""

import logging
import re
import secrets
import sqlite3

from ..config import Settings
from ..db import Conn, Database
from ..errors import ApiError
from ..keys import KeyRecord, KeyService
from ..plans import PLANS, paid_plans
from ..utils import Clock
from .emailparse import CODE_ALPHABET, ParsedPayment, parse_email_bytes, parse_payment, sender_allowed

log = logging.getLogger("ytapi.payments")

MAX_PENDING_ORDERS = 5


def _rupees(paise: int | None) -> float | None:
    return None if paise is None else paise / 100


class PaymentService:
    def __init__(self, db: Database, keys: KeyService, clock: Clock, settings: Settings):
        self.db = db
        self.keys = keys
        self.clock = clock
        self.s = settings
        self.poller = None  # set by main.py when IMAP is on; lets a UTR claim check the mailbox right away

    # ── presentation ──────────────────────────────────────────────────────
    def order_view(self, row) -> dict:
        now = self.clock.now_ts()
        status = row["status"]
        if status == "pending" and now > row["expires_at"] + self.s.order_grace_hours * 3600:
            status = "expired"
        view = {
            "order_id": row["id"],
            "plan": row["plan"],
            "amount_inr": _rupees(row["amount_paise"]),
            "status": status,
            "code": row["code"],
            "created_at": self.clock.iso(row["created_at"]),
            "expires_at": self.clock.iso(row["expires_at"]),
            "paid_at": self.clock.iso(row["paid_at"]),
        }
        if status == "pending":
            view["pay"] = {
                "upi_id": self.s.upi_id or None,
                "payee_name": self.s.payee_name or None,
                "amount_inr": _rupees(row["amount_paise"]),
                "note": row["code"],
                "instructions": (
                    f"Pay exactly ₹{row['amount_paise'] / 100:g} to the UPI ID above. FamPay's payment "
                    "e-mail does not carry a note, so after paying enter the 12-digit UTR / reference "
                    f"number from your UPI app (POST /v1/orders/{row['id']}/claim). Your plan activates "
                    "as soon as that payment is found in the mailbox."
                ),
            }
        return view

    # ── orders ────────────────────────────────────────────────────────────
    def _new_code(self) -> str:
        return self.s.order_prefix.upper() + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))

    async def create_order(self, key: KeyRecord, plan_id: str) -> dict:
        plan = PLANS.get(plan_id)
        if plan is None or plan.price_inr <= 0:
            raise ApiError(
                422,
                "invalid_plan",
                "plan must be one of: " + ", ".join(p.id for p in paid_plans()),
            )
        now = self.clock.now_ts()
        async with self.db.tx() as t:
            existing = await t.fetchone(
                "SELECT * FROM orders WHERE key_id=? AND plan=? AND status='pending' AND expires_at>?",
                (key.id, plan.id, now),
            )
            if existing:
                return self.order_view(existing)
            pending = await t.fetchone(
                "SELECT COUNT(*) AS n FROM orders WHERE key_id=? AND status='pending' AND expires_at>?",
                (key.id, now),
            )
            if pending["n"] >= MAX_PENDING_ORDERS:
                raise ApiError(429, "too_many_orders", "Too many unpaid orders. Pay or wait for them to expire.")
            for _ in range(10):
                code = self._new_code()
                try:
                    order_id = secrets.token_hex(8)
                    await t.execute(
                        "INSERT INTO orders(id, code, key_id, plan, amount_paise, status, created_at, expires_at)"
                        " VALUES(?,?,?,?,?, 'pending', ?, ?)",
                        (order_id, code, key.id, plan.id, plan.price_paise, now, now + self.s.order_ttl_hours * 3600),
                    )
                    break
                except sqlite3.IntegrityError:
                    continue
            else:  # pragma: no cover
                raise ApiError(500, "order_failed", "Could not create an order, try again.")
            row = await t.fetchone("SELECT * FROM orders WHERE id=?", (order_id,))
        return self.order_view(row)

    async def get_order(self, key: KeyRecord, order_id: str) -> dict:
        row = await self.db.fetchone("SELECT * FROM orders WHERE id=? AND key_id=?", (order_id, key.id))
        if not row:
            raise ApiError(404, "order_not_found", "No such order for this key.")
        return self.order_view(row)

    # ── activation ────────────────────────────────────────────────────────
    async def _activate(self, t: Conn, key_id: int, plan_id: str) -> None:
        now = self.clock.now_ts()
        days = self.s.plan_duration_days * 86400
        row = await t.fetchone("SELECT plan, plan_expires_at FROM api_keys WHERE id=?", (key_id,))
        start = now
        if row and row["plan"] == plan_id and row["plan_expires_at"] and row["plan_expires_at"] > now:
            start = row["plan_expires_at"]  # renewing the same plan stacks time
        await t.execute(
            "UPDATE api_keys SET plan=?, plan_expires_at=? WHERE id=?", (plan_id, start + days, key_id)
        )

    async def _settle(self, t: Conn, order, payment_id: int, status_note: str) -> None:
        now = self.clock.now_ts()
        await t.execute(
            "UPDATE orders SET status='paid', payment_id=?, paid_at=? WHERE id=?",
            (payment_id, now, order["id"]),
        )
        await t.execute(
            "UPDATE payments SET status='matched', order_id=?, note=? WHERE id=?",
            (order["id"], status_note, payment_id),
        )
        await self._activate(t, order["key_id"], order["plan"])

    def _matchable(self, order) -> bool:
        if order["status"] != "pending":
            return False
        return self.clock.now_ts() <= order["expires_at"] + self.s.order_grace_hours * 3600

    # ── e-mail → payment → order ──────────────────────────────────────────
    async def handle_email(self, raw: bytes) -> dict:
        info = parse_email_bytes(raw, self.s)
        now = self.clock.now_ts()

        async def finish(result: str) -> dict:
            await self.db.execute(
                "INSERT OR REPLACE INTO processed_emails(message_id, from_addr, subject, result, processed_at)"
                " VALUES(?,?,?,?,?)",
                (info.message_id, info.from_addr, info.subject[:200], result, now),
            )
            log.info("email %s from %s -> %s", info.message_id[:24], info.from_addr, result)
            return {"result": result, "message_id": info.message_id}

        seen = await self.db.fetchone("SELECT 1 FROM processed_emails WHERE message_id=?", (info.message_id,))
        if seen:
            return {"result": "already_processed", "message_id": info.message_id}
        if not sender_allowed(info.from_addr, self.s.payment_sender_list):
            return await finish("ignored_sender")
        if self.s.require_email_auth and not info.auth_ok:
            return await finish("rejected_unauthenticated")

        parsed = parse_payment(info.subject, info.text, self.s)
        if parsed.direction == "out":
            return await finish("ignored_outgoing")
        if parsed.direction != "in" or parsed.amount_paise is None:
            return await finish("unparsed")

        outcome = await self._match_payment(info.message_id, parsed, info.date_ts or now)
        return await finish(outcome["result"]) | {k: v for k, v in outcome.items() if k != "result"}

    async def _match_payment(self, message_id: str, p: ParsedPayment, received_at: int) -> dict:
        async with self.db.tx() as t:
            if p.utr:
                dup = await t.fetchone("SELECT id FROM payments WHERE utr=?", (p.utr,))
                if dup:
                    return {"result": "duplicate_payment"}
            try:
                res = await t.execute(
                    "INSERT INTO payments(message_id, utr, amount_paise, code, payer, received_at, status)"
                    " VALUES(?,?,?,?,?,?, 'unmatched')",
                    (message_id, p.utr, p.amount_paise, p.code, p.payer, received_at),
                )
            except sqlite3.IntegrityError:
                return {"result": "duplicate_payment"}
            pid = res.lastrowid

            order = None
            if p.code:
                order = await t.fetchone("SELECT * FROM orders WHERE code=?", (p.code,))
            if order is None and p.utr:
                order = await t.fetchone(
                    "SELECT * FROM orders WHERE claimed_utr=? AND status='pending'", (p.utr,)
                )
            if order is None:
                return {"result": "unmatched", "payment_id": pid}
            if not self._matchable(order):
                await t.execute(
                    "UPDATE payments SET status='unmatched', order_id=?, note=? WHERE id=?",
                    (order["id"], f"order is {order['status']}", pid),
                )
                return {"result": "order_not_open", "payment_id": pid, "order_id": order["id"]}
            if p.amount_paise < order["amount_paise"]:
                await t.execute(
                    "UPDATE payments SET status='underpaid', order_id=?, note=? WHERE id=?",
                    (order["id"], f"paid {p.amount_paise / 100:g}, needed {order['amount_paise'] / 100:g}", pid),
                )
                return {"result": "underpaid", "payment_id": pid, "order_id": order["id"]}
            note = "overpaid" if p.amount_paise > order["amount_paise"] else "ok"
            await self._settle(t, order, pid, note)
            return {"result": "matched", "payment_id": pid, "order_id": order["id"], "plan": order["plan"]}

    # ── user claims a payment by UTR ──────────────────────────────────────
    async def claim(self, key: KeyRecord, order_id: str, utr: str) -> dict:
        utr = (utr or "").strip()
        if not re.fullmatch(self.s.utr_pattern, utr):
            raise ApiError(422, "invalid_utr", "That does not look like a valid UPI reference (UTR) number.")
        async with self.db.tx() as t:
            order = await t.fetchone("SELECT * FROM orders WHERE id=? AND key_id=?", (order_id, key.id))
            if not order:
                raise ApiError(404, "order_not_found", "No such order for this key.")
            if order["status"] == "paid":
                return self.order_view(order)
            if not self._matchable(order):
                raise ApiError(410, "order_expired", "This order has expired. Create a new one.")

            payment = await t.fetchone("SELECT * FROM payments WHERE utr=?", (utr,))
            if payment is not None:
                if payment["status"] == "matched":
                    raise ApiError(409, "utr_used", "That payment was already used for an order.")
                if payment["amount_paise"] < order["amount_paise"]:
                    raise ApiError(
                        402,
                        "underpaid",
                        f"That payment was ₹{payment['amount_paise'] / 100:g} but this plan costs "
                        f"₹{order['amount_paise'] / 100:g}.",
                    )
                await self._settle(t, order, payment["id"], "claimed by utr")
            else:
                clash = await t.fetchone(
                    "SELECT id FROM orders WHERE claimed_utr=? AND status='pending' AND id<>?", (utr, order_id)
                )
                if clash:
                    raise ApiError(409, "utr_in_use", "That UTR is already attached to another order.")
                await t.execute("UPDATE orders SET claimed_utr=? WHERE id=?", (utr, order_id))
            row = await t.fetchone("SELECT * FROM orders WHERE id=?", (order_id,))
        view = self.order_view(row)
        if view["status"] == "pending" and self.poller is not None:
            # Look in the mailbox right now instead of waiting for the next poll.
            try:
                await self.poller.poll_soon()
            except Exception:
                log.warning("immediate mailbox check failed", exc_info=True)
            row = await self.db.fetchone("SELECT * FROM orders WHERE id=?", (order_id,))
            view = self.order_view(row)
        if view["status"] == "pending":
            view["message"] = (
                "UTR saved. We are checking for your payment; the plan activates automatically "
                "as soon as the payment e-mail shows up (usually within a minute)."
            )
        return view

    # ── admin ─────────────────────────────────────────────────────────────
    async def admin_approve(self, order_id: str, note: str = "manual approval") -> dict:
        async with self.db.tx() as t:
            order = await t.fetchone("SELECT * FROM orders WHERE id=?", (order_id,))
            if not order:
                raise ApiError(404, "order_not_found", "No such order.")
            if order["status"] == "paid":
                return self.order_view(order)
            res = await t.execute(
                "INSERT INTO payments(message_id, utr, amount_paise, code, payer, received_at, status, note)"
                " VALUES(?,?,?,?,?,?, 'unmatched', ?)",
                (f"manual-{secrets.token_hex(6)}", None, order["amount_paise"], order["code"], "admin",
                 self.clock.now_ts(), note),
            )
            await self._settle(t, order, res.lastrowid, note)
            row = await t.fetchone("SELECT * FROM orders WHERE id=?", (order_id,))
        return self.order_view(row)

    async def list_orders(self, status: str | None, limit: int) -> list[dict]:
        if status:
            rows = await self.db.fetchall(
                "SELECT * FROM orders WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)
            )
        else:
            rows = await self.db.fetchall("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self.order_view(r) | {"key_id": r["key_id"], "claimed_utr": r["claimed_utr"]} for r in rows]

    async def list_payments(self, status: str | None, limit: int) -> list[dict]:
        if status:
            rows = await self.db.fetchall(
                "SELECT * FROM payments WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
            )
        else:
            rows = await self.db.fetchall("SELECT * FROM payments ORDER BY id DESC LIMIT ?", (limit,))
        return [
            {
                "id": r["id"],
                "amount_inr": _rupees(r["amount_paise"]),
                "utr": r["utr"],
                "code": r["code"],
                "payer": r["payer"],
                "status": r["status"],
                "order_id": r["order_id"],
                "note": r["note"],
                "received_at": self.clock.iso(r["received_at"]),
            }
            for r in rows
        ]

    async def list_emails(self, result: str | None, limit: int) -> list[dict]:
        if result:
            rows = await self.db.fetchall(
                "SELECT * FROM processed_emails WHERE result=? ORDER BY processed_at DESC LIMIT ?", (result, limit)
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM processed_emails ORDER BY processed_at DESC LIMIT ?", (limit,)
            )
        return [
            {
                "from": r["from_addr"],
                "subject": r["subject"],
                "result": r["result"],
                "at": self.clock.iso(r["processed_at"]),
            }
            for r in rows
        ]
