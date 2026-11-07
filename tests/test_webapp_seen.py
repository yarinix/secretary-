"""Тесты защищённого machine-to-machine приёмника POST /internal/seen."""

import hashlib
import hmac
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app_settings import build_defaults
from storage import Storage
from webapp import register_webapp_routes

BOT_TOKEN = "123456789:AAFakeTokenForTests_xxxxxxxxxxxxxxxxxx"
OWNER_ID = 5084782149
WEB_DIR = Path(__file__).resolve().parents[1] / "web"
SEEN_SECRET = "s" * 24
CANARY = "SEEN_SECRET_LEAK_CANARY"


def make_init_data():
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAANedF6I",
        "user": json.dumps(
            {"id": OWNER_ID, "first_name": "Артём"},
            ensure_ascii=False, separators=(",", ":"),
        ),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode({**pairs, "hash": digest})

CFG_STUB = SimpleNamespace(
    MODELS=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
    IGNORE_IDS=set(), OWNER_NAME="Артём", BOT_TIMEZONE="Asia/Omsk",
    MAX_REPLIES_PER_HOUR=20, HISTORY_LIMIT=12, PERSONA_LIMIT=800,
    SMART_REPLIES="1", REPLY_DELAY_MINUTES=10, AWAY_MINUTES=60,
    AWAY_COLLECT_SECONDS=15, NIGHT_ENABLED="1",
    NIGHT_START="01:00", NIGHT_END="08:00", NIGHT_HINT="",
)


class SeenRouteTestBase(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, secret=SEEN_SECRET, provider=None, no_provider=False):
        self.calls = []
        if provider is None and not no_provider:
            async def default_provider(chat_id, max_read_id, read_at):
                self.calls.append((chat_id, max_read_id, read_at))
                return {"cancelled": True, "reason": "owner_read"}
            provider = default_provider

        self.db = Storage(":memory:")
        self.app = web.Application()
        register_webapp_routes(
            self.app,
            bot_token=BOT_TOKEN,
            owner_id=OWNER_ID,
            storage=self.db,
            defaults=build_defaults(CFG_STUB),
            status_provider=lambda: {
                "pending_chats": 1,
                "seen_watcher_enabled": True,
                CANARY: CANARY,  # постороннее поле не должно пройти
            },
            readonly={"BOT_TOKEN": BOT_TOKEN, "OWNER_ID": OWNER_ID},
            web_dir=WEB_DIR,
            seen_secret=secret,
            seen_provider=provider,
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        return self.client

    async def asyncTearDown(self):
        await self.client.close()

    async def post(self, payload, headers=None):
        return await self.client.post(
            "/internal/seen",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
        )


class TestSeenRouteDisabled(SeenRouteTestBase):
    async def test_no_secret_route_not_registered(self):
        await self.make_client(secret="")
        r = await self.post({"chat_id": 1, "max_read_id": 5})
        self.assertEqual(r.status, 404)
        self.assertEqual(self.calls, [])

    async def test_short_secret_disables_route(self):
        await self.make_client(secret="tiny")
        r = await self.post({"chat_id": 1, "max_read_id": 5})
        self.assertEqual(r.status, 404)
        self.assertEqual(self.calls, [])


class TestSeenRouteAuth(SeenRouteTestBase):
    async def asyncSetUp(self):
        await self.make_client()

    async def test_no_header_unauthorized(self):
        r = await self.post({"chat_id": 1, "max_read_id": 5})
        self.assertEqual(r.status, 401)
        self.assertEqual(self.calls, [])

    async def test_wrong_secret_unauthorized(self):
        r = await self.post(
            {"chat_id": 1, "max_read_id": 5},
            headers={"Authorization": "Bearer wrong-secret-value-xxxx"},
        )
        self.assertEqual(r.status, 401)
        self.assertEqual(self.calls, [])

    async def test_initdata_does_not_authorize_machine_route(self):
        # initData — другой контур авторизации, здесь он не должен работать.
        r = await self.post(
            {"chat_id": 1, "max_read_id": 5},
            headers={"X-Telegram-Init-Data": "query_id=x&user=%7B%7D&hash=abc"},
        )
        self.assertEqual(r.status, 401)

    async def test_bearer_header_works(self):
        r = await self.post(
            {"chat_id": 42, "max_read_id": 77, "read_at": 1234.5},
            headers={"Authorization": "Bearer " + SEEN_SECRET},
        )
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["cancelled"])
        self.assertEqual(self.calls, [(42, 77, 1234.5)])
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    async def test_alternative_header_works(self):
        r = await self.post(
            {"chat_id": 7, "read_at": 99.0},
            headers={"X-Seen-Secret": SEEN_SECRET},
        )
        self.assertEqual(r.status, 200)
        self.assertEqual(self.calls, [(7, None, 99.0)])


class TestSeenRouteValidation(SeenRouteTestBase):
    async def asyncSetUp(self):
        await self.make_client()
        self.h = {"Authorization": "Bearer " + SEEN_SECRET}

    async def test_bad_chat_id(self):
        for bad in (0, -1, "abc", None, True, {}, [1]):
            with self.subTest(bad=bad):
                r = await self.post({"chat_id": bad, "max_read_id": 5}, self.h)
                self.assertEqual(r.status, 400)
        self.assertEqual(self.calls, [])

    async def test_needs_at_least_one_marker(self):
        r = await self.post({"chat_id": 1}, self.h)
        self.assertEqual(r.status, 400)
        self.assertEqual(self.calls, [])

    async def test_bool_is_not_an_int(self):
        r = await self.post({"chat_id": 1, "max_read_id": True}, self.h)
        self.assertEqual(r.status, 400)

    async def test_wrong_content_type(self):
        r = await self.client.post(
            "/internal/seen", data=b"chat_id=1",
            headers={**self.h, "Content-Type": "text/plain"},
        )
        self.assertEqual(r.status, 415)

    async def test_bad_json(self):
        r = await self.client.post(
            "/internal/seen", data=b"{not json",
            headers={**self.h, "Content-Type": "application/json"},
        )
        self.assertEqual(r.status, 400)


class TestSeenRouteProvider(SeenRouteTestBase):
    async def test_sync_provider(self):
        calls = []
        await self.make_client(provider=lambda c, m, t: calls.append((c, m, t)) or
                               {"cancelled": False, "reason": "new_messages_after_read"})
        r = await self.post(
            {"chat_id": 3, "max_read_id": 9},
            {"Authorization": "Bearer " + SEEN_SECRET},
        )
        self.assertEqual(r.status, 200)
        self.assertFalse((await r.json())["cancelled"])
        self.assertEqual(calls, [(3, 9, None)])

    async def test_provider_exception_returns_502_without_leak(self):
        def boom(c, m, t):
            raise RuntimeError(CANARY + " / " + SEEN_SECRET)
        await self.make_client(provider=boom)
        r = await self.post(
            {"chat_id": 3, "max_read_id": 9},
            {"Authorization": "Bearer " + SEEN_SECRET},
        )
        self.assertEqual(r.status, 502)
        text = await r.text()
        self.assertNotIn(CANARY, text)
        self.assertNotIn(SEEN_SECRET, text)

    async def test_no_provider_returns_503(self):
        await self.make_client(no_provider=True)
        r = await self.post(
            {"chat_id": 1, "max_read_id": 5},
            {"Authorization": "Bearer " + SEEN_SECRET},
        )
        self.assertEqual(r.status, 503)


class TestSeenSecretNotLeaked(SeenRouteTestBase):
    async def test_secret_absent_from_state_and_status(self):
        await self.make_client(secret=CANARY + "_" + "x" * 20)
        r = await self.client.post(
            "/api/state", json={"initData": make_init_data()}
        )
        self.assertEqual(r.status, 200)
        text = await r.text()
        self.assertNotIn(CANARY, text)
        self.assertNotIn(SEEN_SECRET, text)
        data = await r.json()
        self.assertIn("seen_watcher_enabled", data["status"])
        self.assertTrue(data["status"]["seen_watcher_enabled"])
        self.assertNotIn(CANARY, data["status"])
