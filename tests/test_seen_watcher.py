"""Тесты seen_watcher.py: Telethon НЕ нужен, сеть подменяется фейками."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import seen_watcher as w


def tl(name, **attrs):
    """Имитация TL-объекта Telethon с нужным именем типа."""
    obj = type(name, (), {})()
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


def inbox(user_id=42, max_id=77, **extra):
    peer = tl("PeerUser", user_id=user_id)
    return tl("UpdateReadHistoryInbox", peer=peer, max_id=max_id, **extra)


def base_env(**over):
    env = {
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "abcdef",
        "TELEGRAM_SESSION": "session-string",
        "SECRETARY_SEEN_URL": "https://bot.example.com/internal/seen/",
        "SECRETARY_SEEN_SECRET": "s" * 24,
    }
    env.update(over)
    return env


class TestLoadConfig(unittest.TestCase):
    def test_valid(self):
        cfg = w.load_config(base_env())
        self.assertEqual(cfg["api_id"], 12345)
        self.assertEqual(cfg["url"], "https://bot.example.com/internal/seen")
        self.assertIsNone(cfg["allowed"])

    def test_session_or_phone_required(self):
        env = base_env(TELEGRAM_SESSION="", TELEGRAM_PHONE="")
        with self.assertRaises(SystemExit):
            w.load_config(env)

    def test_phone_instead_of_session(self):
        cfg = w.load_config(base_env(TELEGRAM_SESSION="", TELEGRAM_PHONE="+70000000000"))
        self.assertEqual(cfg["phone"], "+70000000000")

    def test_missing_key(self):
        with self.assertRaises(SystemExit):
            w.load_config({k: v for k, v in base_env().items()
                           if k != "TELEGRAM_API_ID"})

    def test_bad_api_id(self):
        with self.assertRaises(SystemExit):
            w.load_config(base_env(TELEGRAM_API_ID="not-a-number"))

    def test_short_secret_rejected(self):
        with self.assertRaises(SystemExit):
            w.load_config(base_env(SECRETARY_SEEN_SECRET="short"))

    def test_plain_http_rejected(self):
        with self.assertRaises(SystemExit):
            w.load_config(base_env(SECRETARY_SEEN_URL="http://evil.example.com/internal/seen"))

    def test_localhost_http_allowed_for_dev(self):
        cfg = w.load_config(base_env(SECRETARY_SEEN_URL="http://127.0.0.1:10000/internal/seen"))
        self.assertIn("127.0.0.1", cfg["url"])

    def test_allowed_list(self):
        cfg = w.load_config(base_env(SEEN_CHAT_IDS="42, 7, bad, -1"))
        self.assertEqual(cfg["allowed"], {42, 7})


class TestExtractRead(unittest.TestCase):
    def test_private_inbox(self):
        self.assertEqual(w.extract_read(inbox()), {"chat_id": 42, "max_id": 77})

    def test_outbox_ignored(self):
        u = tl("UpdateReadHistoryOutbox", peer=tl("PeerUser", user_id=42), max_id=77)
        self.assertIsNone(w.extract_read(u))

    def test_own_outgoing_read_ignored(self):
        self.assertIsNone(w.extract_read(inbox(out=True)))

    def test_group_and_channel_ignored(self):
        for peer in (tl("PeerChat", chat_id=42), tl("PeerChannel", channel_id=42)):
            with self.subTest(peer=type(peer).__name__):
                u = tl("UpdateReadHistoryInbox", peer=peer, max_id=77)
                self.assertIsNone(w.extract_read(u))

    def test_contents_update_ignored(self):
        u = tl("UpdateReadMessagesContents", messages=[1, 2])
        self.assertIsNone(w.extract_read(u))

    def test_bool_rejected(self):
        self.assertIsNone(w.extract_read(inbox(user_id=True)))
        self.assertIsNone(w.extract_read(inbox(max_id=True)))

    def test_bad_values_rejected(self):
        self.assertIsNone(w.extract_read(inbox(user_id="abc")))
        self.assertIsNone(w.extract_read(inbox(max_id=None)))
        self.assertIsNone(w.extract_read(inbox(user_id=0)))
        self.assertIsNone(w.extract_read(inbox(max_id=-1)))

    def test_plain_namespace_is_not_an_update(self):
        self.assertIsNone(w.extract_read(SimpleNamespace(peer=None, max_id=1)))


class TestHelpers(unittest.TestCase):
    def test_allowed(self):
        self.assertTrue(w.is_allowed(1, None))
        self.assertTrue(w.is_allowed(1, {1, 2}))
        self.assertFalse(w.is_allowed(9, {1, 2}))

    def test_payload_has_both_markers(self):
        payload = w.build_payload({"chat_id": 42, "max_id": 77}, now=1000.0)
        self.assertEqual(payload, {
            "chat_id": 42, "max_read_id": 77, "read_at": 1000.0,
        })


class FakeResp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0
        self.kwargs = None

    def post(self, url, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return FakeResp(self.statuses.pop(0) if self.statuses else 200)


CFG = {
    "url": "https://bot.example.com/internal/seen",
    "secret": "s" * 24,
    "allowed": None,
}


class TestPostSeen(unittest.IsolatedAsyncioTestCase):
    async def test_success_first_try(self):
        s = FakeSession([200])
        self.assertTrue(await w.post_seen(s, CFG, {"chat_id": 1}))
        self.assertEqual(s.calls, 1)
        self.assertEqual(s.kwargs["headers"]["Authorization"], "Bearer " + CFG["secret"])

    async def test_4xx_no_retry(self):
        s = FakeSession([401])
        self.assertFalse(await w.post_seen(s, CFG, {"chat_id": 1}))
        self.assertEqual(s.calls, 1)

    async def test_5xx_retries(self):
        s = FakeSession([500, 502, 200])
        with patch("seen_watcher.asyncio.sleep", new=AsyncMock()):
            self.assertTrue(await w.post_seen(s, CFG, {"chat_id": 1}))
        self.assertEqual(s.calls, 3)

    async def test_gives_up_after_retries(self):
        s = FakeSession([500, 500, 500])
        with patch("seen_watcher.asyncio.sleep", new=AsyncMock()):
            self.assertFalse(await w.post_seen(s, CFG, {"chat_id": 1}))
        self.assertEqual(s.calls, 3)


class TestHandleUpdate(unittest.IsolatedAsyncioTestCase):
    async def test_valid_update_posts(self):
        s = FakeSession([200])
        cfg = dict(CFG)
        with patch("seen_watcher.post_seen", new=AsyncMock(return_value=True)) as m:
            ok = await w.handle_update(inbox(), s, cfg)
        self.assertTrue(ok)
        payload = m.call_args.args[2]
        self.assertEqual(payload["chat_id"], 42)
        self.assertEqual(payload["max_read_id"], 77)

    async def test_irrelevant_update_does_not_post(self):
        u = tl("UpdateReadHistoryOutbox", peer=tl("PeerUser", user_id=42), max_id=77)
        with patch("seen_watcher.post_seen", new=AsyncMock()) as m:
            self.assertFalse(await w.handle_update(u, FakeSession([200]), dict(CFG)))
        m.assert_not_awaited()

    async def test_allowlist_filters(self):
        cfg = dict(CFG, allowed={999})
        with patch("seen_watcher.post_seen", new=AsyncMock()) as m:
            self.assertFalse(await w.handle_update(inbox(), FakeSession([200]), cfg))
        m.assert_not_awaited()
