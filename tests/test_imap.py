"""The IMAP poller, exercised against an in-memory fake of imaplib.IMAP4_SSL.

NOTE: this proves our logic (UIDs, restarts, dedupe, error recovery). It does
not prove a real Gmail login works; that depends on your credentials.
"""

import imaplib

import pytest

from tests.conftest import build_email
from app.payments.imap_poller import ImapPoller


class FakeMailbox:
    def __init__(self):
        self.messages: dict[int, bytes] = {}
        self.next_uid = 1
        self.uidvalidity = "777"
        self.logins = 0
        self.fail_login = False
        self.fail_search = False
        self.calls: list[tuple] = []
        self.opened_readonly: list[bool] = []
        self.timeouts: list = []

    def add(self, raw: bytes) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.messages[uid] = raw
        return uid

    def factory(self, host, port, timeout=None):
        self.timeouts.append(timeout)
        return FakeImap(self)


class FakeImap:
    def __init__(self, box: FakeMailbox):
        self.box = box

    def login(self, user, password):
        if self.box.fail_login:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        self.box.logins += 1
        return "OK", [b"logged in"]

    def select(self, folder, readonly=False):
        self.box.opened_readonly.append(readonly)
        return "OK", [str(len(self.box.messages)).encode()]

    def response(self, name):
        assert name == "UIDVALIDITY"
        return name, [self.box.uidvalidity.encode()]

    def uid(self, command, *args):
        self.box.calls.append((command, *args))
        if command == "SEARCH":
            if self.box.fail_search:
                raise imaplib.IMAP4.abort("socket error")
            crit = args[1:]
            uids = sorted(self.box.messages)
            if crit and crit[0] == "UID":
                lo = int(crit[1].split(":")[0])
                uids = [u for u in uids if u >= lo] or uids[-1:]  # real servers return the newest for n:*
            return "OK", [" ".join(map(str, uids)).encode()]
        if command == "FETCH":
            uid = int(args[0])
            assert "PEEK" in args[1], "must not mark mail as read"
            return "OK", [(f"{uid} (BODY[] {{1}}".encode(), self.box.messages[uid]), b")"]
        raise AssertionError(command)

    def logout(self):
        return "BYE", [b""]


def mail_for(code, utr, amount=29):
    return build_email(body=f"You received ₹{amount}.00 from Rahul. UTR {utr}. Note: {code}")


@pytest.fixture
def box():
    return FakeMailbox()


async def test_poller_activates_plan_from_mailbox(make_api, box):
    async with make_api(imap_factory=box.factory, imap_enabled=True, imap_user="u", imap_password="p",
                        imap_poll_seconds=1) as api:
        key = await api.new_key("imapbuyer")
        order = (await api.http.post("/v1/orders", json={"plan": "p2000"}, headers=api.h(key))).json()
        poller: ImapPoller = api.st.poller
        assert poller is not None
        box.add(mail_for(order["code"], "123456789012"))
        import asyncio
        for _ in range(100):  # background task polls every second
            usage = (await api.http.get("/v1/me", headers=api.h(key))).json()["usage"]
            if usage["plan"] == "p2000":
                break
            await asyncio.sleep(0.05)
        assert usage["plan"] == "p2000"
        assert all(box.opened_readonly)  # mailbox opened read-only
        assert box.timeouts and all(t == 30 for t in box.timeouts)  # socket timeout is always set


async def poller_for(api, box):
    return ImapPoller(api.st.settings, api.st.db, api.st.payments, api.st.clock, imap_factory=box.factory)


async def test_poll_once_only_fetches_new_mail_and_skips_old_history(make_api, box):
    async with make_api(imap_user="u", imap_password="p") as api:
        key = await api.new_key("hist")
        order = (await api.http.post("/v1/orders", json={"plan": "p3000"}, headers=api.h(key))).json()
        for i in range(5):  # old mail that is already in the inbox
            box.add(build_email(subject=f"old {i}", body="Welcome", message_id=f"<old{i}@x>"))
        p = await poller_for(api, box)
        await p.poll_once()
        fetched_first = [c for c in box.calls if c[0] == "FETCH"]
        assert len(fetched_first) == 5  # first run looks back over the window once

        box.calls.clear()
        uid = box.add(mail_for(order["code"], "223456789012", 39))
        await p.poll_once()
        fetched = [c for c in box.calls if c[0] == "FETCH"]
        assert [int(c[1]) for c in fetched] == [uid]  # ONLY the new message
        assert (await api.st.keys.get(await api.key_id(key))).plan == "p3000"

        box.calls.clear()
        await p.poll_once()
        assert not [c for c in box.calls if c[0] == "FETCH"]  # nothing new, nothing fetched


async def test_first_run_with_empty_window_does_not_replay_whole_mailbox(make_api, box):
    async with make_api(imap_user="u", imap_password="p") as api:
        p = await poller_for(api, box)
        await p.poll_once()  # empty mailbox
        for i in range(3):
            box.add(build_email(subject=f"n{i}", body="hello", message_id=f"<n{i}@x>"))
        box.calls.clear()
        await p.poll_once()
        assert len([c for c in box.calls if c[0] == "FETCH"]) == 3
        assert p._last_uid == 3


async def test_restart_does_not_double_credit(make_api, box):
    async with make_api(imap_user="u", imap_password="p") as api:
        key = await api.new_key("restart")
        order = (await api.http.post("/v1/orders", json={"plan": "p2000"}, headers=api.h(key))).json()
        box.add(mail_for(order["code"], "323456789012"))
        p1 = await poller_for(api, box)
        await p1.poll_once()
        kid = await api.key_id(key)
        expiry = (await api.st.keys.get(kid)).plan_expires_at

        # a brand new poller (process restart) with the saved UID state
        p2 = await poller_for(api, box)
        await p2._load_state()
        await p2.poll_once()
        assert (await api.st.keys.get(kid)).plan_expires_at == expiry

        # even with ALL state lost, Message-ID dedupe protects the customer
        await api.st.db.execute("DELETE FROM settings WHERE k='imap_state'")
        p3 = await poller_for(api, box)
        await p3.poll_once()
        assert (await api.st.keys.get(kid)).plan_expires_at == expiry


async def test_poller_survives_errors_and_recovers(make_api, box):
    async with make_api(imap_user="u", imap_password="p") as api:
        p = await poller_for(api, box)
        box.fail_login = True
        with pytest.raises(imaplib.IMAP4.error):
            await p.poll_once()
        box.fail_login = False
        box.fail_search = True
        with pytest.raises(imaplib.IMAP4.abort):
            await p.poll_once()
        box.fail_search = False
        await p.poll_once()
        assert p.last_error is None and p.last_ok_at is not None


async def test_uidvalidity_change_triggers_a_safe_rescan(make_api, box):
    async with make_api(imap_user="u", imap_password="p") as api:
        key = await api.new_key("validity")
        order = (await api.http.post("/v1/orders", json={"plan": "p2000"}, headers=api.h(key))).json()
        p = await poller_for(api, box)
        box.add(build_email(subject="x", body="hi", message_id="<a@x>"))
        await p.poll_once()
        # mailbox rebuilt by the provider: UIDs restart and validity changes
        box.messages.clear()
        box.next_uid = 1
        box.uidvalidity = "888"
        box.add(mail_for(order["code"], "423456789012"))
        await p.poll_once()
        assert (await api.st.keys.get(await api.key_id(key))).plan == "p2000"


async def test_health_reports_imap_status(make_api, box):
    async with make_api(imap_factory=box.factory, imap_enabled=True, imap_user="u", imap_password="p",
                        imap_poll_seconds=3600) as api:
        import asyncio
        await asyncio.sleep(0.2)
        h = (await api.http.get("/health")).json()
        assert h["imap"]["ok"] is True
