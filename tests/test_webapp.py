"""Тесты HTTP-роутов Mini App: авторизация, валидация, отсутствие утечек."""

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
SECRET_HEX = "0123456789abcdef0123456789abcdef"


def make_init_data(bot_token=BOT_TOKEN, user_id=OWNER_ID, auth_date=None):
    """Подписанная строка с ТЕКУЩИМ временем (webapp проверяет реальный now)."""
    pairs = {
        "auth_date": str(int(time.time() if auth_date is None else auth_date)),
        "query_id": "AAHdF6IQAAAAANedF6I",
        "user": json.dumps(
            {"id": user_id, "first_name": "Артём"}, ensure_ascii=False, separators=(",", ":")
        ),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode({**pairs, "hash": digest})


CFG_STUB = SimpleNamespace(
    MODELS=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
    IGNORE_IDS=set(), OWNER_NAME="Артём", BOT_TIMEZONE="Asia/Omsk",
    TAKEOVER_MINUTES=60, MAX_REPLIES_PER_HOUR=20,
    HISTORY_LIMIT=12, PERSONA_LIMIT=800,
)

READONLY = {
    "BOT_TOKEN": BOT_TOKEN,
    "OWNER_ID": OWNER_ID,
    "WEBHOOK_SECRET": SECRET_HEX,
    "PORT": 10000,
    "DB_PATH": "secretary.db",
    "BASE_URL": "https://example.onrender.com",
    "APP_URL": "https://example.onrender.com/app",
    "api_key_set": True,
}


class TestWebAppRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Storage(":memory:")
        self.defaults = build_defaults(CFG_STUB)
        self.app = web.Application()
        register_webapp_routes(
            self.app,
            bot_token=BOT_TOKEN,
            owner_id=OWNER_ID,
            storage=self.db,
            defaults=self.defaults,
            status_provider=lambda: {
                "connected": True, "can_reply": True,
                "paused_chats": 0, "replies_last_hour": 3,
            },
            readonly=READONLY,
            web_dir=WEB_DIR,
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.init = make_init_data()

    async def asyncTearDown(self):
        await self.client.close()

    async def state(self, init_data=None):
        return await self.client.post("/api/state", json={"initData": init_data or self.init})

    # ---------- статика ----------
    async def test_app_html(self):
        r = await self.client.get("/app")
        self.assertEqual(r.status, 200)
        text = await r.text()
        self.assertIn("Секретарь", text)
        self.assertIn("/static/app.js", text)
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    async def test_static_allowed(self):
        r = await self.client.get("/static/app.css")
        self.assertEqual(r.status, 200)
        self.assertIn("--tg-theme", await r.text())

    async def test_static_traversal_blocked(self):
        for name in ("config.py", "..%2Fconfig.py", "app.py", "secretary.db"):
            with self.subTest(name=name):
                r = await self.client.get("/static/" + name)
                self.assertEqual(r.status, 404)

    # ---------- авторизация ----------
    async def test_state_without_init_data(self):
        r = await self.client.post("/api/state", json={})
        self.assertEqual(r.status, 401)

    async def test_state_forged_signature(self):
        r = await self.state("auth_date=1&hash=deadbeef&user=%7B%7D")
        self.assertEqual(r.status, 401)

    async def test_state_wrong_owner(self):
        r = await self.state(make_init_data(user_id=123456, auth_date=time.time()))
        self.assertEqual(r.status, 403)

    async def test_state_expired(self):
        r = await self.state(make_init_data(auth_date=time.time() - 7200))
        self.assertEqual(r.status, 401)

    async def test_state_via_header(self):
        r = await self.client.post(
            "/api/state", json={}, headers={"X-Telegram-Init-Data": self.init}
        )
        self.assertEqual(r.status, 200)

    # ---------- форма ответа и утечки ----------
    async def test_state_shape_and_no_secrets(self):
        r = await self.state()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")
        data = await r.json()
        self.assertEqual(data["settings"]["takeover_minutes"], 60)
        self.assertEqual(data["status"]["replies_last_hour"], 3)
        self.assertIn("fields", data["schema"])
        self.assertTrue(data["readonly"]["api_key_set"])
        raw = json.dumps(data, ensure_ascii=False)
        self.assertNotIn(BOT_TOKEN, raw)
        self.assertNotIn(SECRET_HEX, raw)
        self.assertNotIn(self.init, raw)
        self.assertNotIn("hash", data["readonly"])

    # ---------- сохранение ----------
    async def test_settings_valid_changes_runtime(self):
        r = await self.client.post(
            "/api/settings", json={"initData": self.init, "settings": {"takeover_minutes": "10"}}
        )
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertEqual(data["settings"]["takeover_minutes"], 10)
        state = await (await self.state()).json()
        self.assertEqual(state["settings"]["takeover_minutes"], 10)
        self.assertEqual(self.db.get_setting("takeover_minutes"), "10")

    async def test_settings_full_patch(self):
        patch = {
            "enabled": "0", "inbox": "0", "notify": "1",
            "persona": "Отвечай коротко", "takeover_minutes": "10",
            "max_replies_per_hour": "50", "owner_name": "Иван",
            "timezone": "Europe/Moscow", "ignore_ids": [111, 222],
            "models": ["m1", "m2"], "history_limit": "20", "persona_limit": "900",
        }
        r = await self.client.post(
            "/api/settings", json={"initData": self.init, "settings": patch}
        )
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertEqual(data["settings"]["models"], ["m1", "m2"])
        self.assertEqual(data["settings"]["ignore_ids"], [111, 222])
        self.assertEqual(data["settings"]["enabled"], "0")

    async def test_settings_invalid_is_all_or_nothing(self):
        r = await self.client.post(
            "/api/settings",
            json={"initData": self.init, "settings": {
                "takeover_minutes": "10", "max_replies_per_hour": "999",
            }},
        )
        self.assertEqual(r.status, 400)
        data = await r.json()
        self.assertIn("max_replies_per_hour", data["errors"])
        self.assertEqual(self.db.get_all_settings(), {})

    async def test_settings_rejects_readonly_keys(self):
        for key in ("BOT_TOKEN", "OWNER_ID", "WEBHOOK_SECRET", "api_key_set"):
            with self.subTest(key=key):
                r = await self.client.post(
                    "/api/settings", json={"initData": self.init, "settings": {key: "hack"}}
                )
                self.assertEqual(r.status, 400)
                self.assertIn(key, (await r.json())["errors"])
        self.assertEqual(self.db.get_all_settings(), {})

    async def test_settings_requires_auth_before_db_write(self):
        r = await self.client.post(
            "/api/settings", json={"settings": {"takeover_minutes": "10"}}
        )
        self.assertEqual(r.status, 401)
        self.assertEqual(self.db.get_all_settings(), {})

    async def test_settings_wrong_content_type(self):
        r = await self.client.post(
            "/api/state", data="привет", headers={"Content-Type": "text/plain"}
        )
        self.assertEqual(r.status, 415)

    async def test_settings_body_too_large(self):
        r = await self.client.post(
            "/api/settings",
            json={"initData": self.init, "settings": {"persona": "x" * 70000}},
        )
        self.assertEqual(r.status, 413)

    async def test_test_ai_without_provider(self):
        r = await self.client.post("/api/test-ai", json={"initData": self.init})
        self.assertEqual(r.status, 503)

    async def test_test_ai_requires_auth(self):
        r = await self.client.post("/api/test-ai", json={})
        self.assertEqual(r.status, 401)


if __name__ == "__main__":
    unittest.main()
