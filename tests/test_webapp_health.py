"""Тесты /health и отображения активного хранилища в Mini App."""

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
DSN = "postgresql://user:supersecret@db.example.com:5432/secretary"

CFG_STUB = SimpleNamespace(
    MODELS=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
    IGNORE_IDS=set(), OWNER_NAME="Артём", BOT_TIMEZONE="Asia/Omsk",
    TAKEOVER_MINUTES=60, MAX_REPLIES_PER_HOUR=20,
    HISTORY_LIMIT=12, PERSONA_LIMIT=800,
)

READONLY = {
    "BOT_TOKEN": BOT_TOKEN,
    "OWNER_ID": OWNER_ID,
    "WEBHOOK_SECRET": "0123456789abcdef0123456789abcdef",
    "PORT": 10000,
    "DB_PATH": "secretary.db",
    "BASE_URL": "https://example.onrender.com",
    "APP_URL": "https://example.onrender.com/app",
    "STORAGE_BACKEND": "Postgres (устойчиво)",
    # Не в белом списке: DSN не должен уйти в клиент ни при каких условиях.
    "DATABASE_URL": DSN,
    "api_key_set": True,
}


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


class _ExplodingStorage:
    """Если /health вдруг полезет в БД — тест сразу упадёт."""

    def get_all_settings(self):
        raise AssertionError("/health не должен читать БД")

    def set_settings_many(self, pairs):
        raise AssertionError("/health не должен писать в БД")


def make_app(storage, readonly=READONLY):
    app = web.Application()
    register_webapp_routes(
        app,
        bot_token=BOT_TOKEN,
        owner_id=OWNER_ID,
        storage=storage,
        defaults=build_defaults(CFG_STUB),
        readonly=readonly,
        web_dir=WEB_DIR,
    )
    return app


class TestHealthRoute(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(make_app(Storage(":memory:"))))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_health_returns_ok_without_auth(self):
        r = await self.client.get("/health")
        self.assertEqual(r.status, 200)
        self.assertEqual(await r.text(), "ok")
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    async def test_health_does_not_touch_database(self):
        client = TestClient(TestServer(make_app(_ExplodingStorage())))
        await client.start_server()
        try:
            r = await client.get("/health")
            self.assertEqual(r.status, 200)
            self.assertEqual(await r.text(), "ok")
        finally:
            await client.close()

    async def test_health_post_not_allowed(self):
        r = await self.client.post("/health")
        self.assertEqual(r.status, 405)

    async def test_app_still_served(self):
        r = await self.client.get("/app")
        self.assertEqual(r.status, 200)
        self.assertIn("telegram-web-app.js", await r.text())

    async def test_state_reports_backend_without_leaking_dsn(self):
        r = await self.client.post("/api/state", json={"initData": make_init_data()})
        self.assertEqual(r.status, 200)
        data = await r.json()
        raw = json.dumps(data, ensure_ascii=False)
        self.assertEqual(data["readonly"]["STORAGE_BACKEND"], "Postgres (устойчиво)")
        self.assertIn("STORAGE_BACKEND", data["schema"]["readonly"])
        self.assertNotIn("DATABASE_URL", data["readonly"])
        self.assertNotIn(DSN, raw)
        self.assertNotIn("supersecret", raw)
        self.assertNotIn(BOT_TOKEN, raw)
        self.assertNotIn("0123456789abcdef0123456789abcdef", raw)

    async def test_sqlite_backend_reported_as_temporary(self):
        client = TestClient(TestServer(make_app(
            Storage(":memory:"),
            readonly={**READONLY, "STORAGE_BACKEND": "SQLite (временный — сбросится при рестарте)"},
        )))
        await client.start_server()
        try:
            r = await client.post("/api/state", json={"initData": make_init_data()})
            self.assertEqual(r.status, 200)
            data = await r.json()
            self.assertIn("SQLite", data["readonly"]["STORAGE_BACKEND"])
            self.assertNotIn("postgresql://", json.dumps(data, ensure_ascii=False))
        finally:
            await client.close()

    async def test_storage_backend_is_readonly_for_client(self):
        r = await self.client.post(
            "/api/settings",
            json={"initData": make_init_data(), "settings": {"STORAGE_BACKEND": "hack"}},
        )
        self.assertEqual(r.status, 400)
        data = await r.json()
        self.assertIn("STORAGE_BACKEND", data.get("errors", {}))
