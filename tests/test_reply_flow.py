"""Интеграционный тест потока отложенных ответов на реальном main.py.

Импорт main.py требует aiogram; если его нет в окружении, тесты пропускаются
(чистая логика и SQL-контракт покрыты остальными файлами без этой зависимости).
Сеть не используется: Bot и GeminiCascade подменяются фейками, БД — SQLite
в памяти.
"""

import asyncio
import os
import time
import unittest
from types import SimpleNamespace

try:
    import aiogram  # noqa: F401
    HAS_AIOGRAM = True
except ImportError:
    HAS_AIOGRAM = False

OWNER_ID = 5084782149


def make_message(*, chat_id=1, sender_id=999, text="привет", message_id=1,
                 conn_id="cid"):
    return SimpleNamespace(
        business_connection_id=conn_id,
        chat=SimpleNamespace(id=chat_id, type="private"),
        from_user=SimpleNamespace(
            id=sender_id, is_bot=False, first_name="Иван", last_name=None,
            username="ivan",
        ),
        text=text, caption=None, voice=None, video_note=None, photo=None,
        message_id=message_id, sender_business_bot=None,
    )


class FakeBot:
    def __init__(self):
        self.replies = []       # send_message с business_connection_id
        self.notifications = []  # уведомления владельцу
        self.edited = []
        self.actions = []
        self._mid = 1000

    async def send_message(self, chat_id, text, **kw):
        self._mid += 1
        if kw.get("business_connection_id"):
            self.replies.append((chat_id, text, kw))
        else:
            self.notifications.append((chat_id, text))
        return SimpleNamespace(message_id=self._mid)

    async def edit_message_text(self, text, **kw):
        self.edited.append((kw.get("chat_id"), kw.get("message_id"), text))
        return True

    async def send_chat_action(self, *a, **kw):
        self.actions.append((a, kw))

    async def get_business_connection(self, cid):
        raise AssertionError("подключение должно браться из _connections")


class FakeCascade:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.systems = []
        self.texts = []
        self.histories = []

    async def generate(self, system, history, text, media=None):
        self.systems.append(system)
        self.histories.append(history)
        self.texts.append(text)
        return "Я сейчас занят, отвечу позже."


class _AsyncioShim:
    """Убирает реальную задержку перед отправкой, сохраняя остальной asyncio."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def sleep(self, _seconds):
        return None


@unittest.skipUnless(HAS_AIOGRAM, "aiogram не установлен — интеграционный тест пропущен")
class TestReplyFlow(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # ВАЖНО: env выставляем ДО первого импорта config/main.
        os.environ["BOT_TOKEN"] = "123456789:AAFakeTokenForTests_xxxxxxxxxxxxxxxxxx"
        os.environ["OWNER_ID"] = str(OWNER_ID)
        os.environ["DB_PATH"] = ":memory:"
        os.environ.pop("DATABASE_URL", None)
        os.environ["WEBHOOK_SECRET"] = "0123456789abcdef0123456789abcdef"
        os.environ.pop("WEBHOOK_BASE_URL", None)
        os.environ.pop("RENDER_EXTERNAL_URL", None)
        os.environ.pop("WEBAPP_URL", None)
        import main
        cls.main = main
        cls._real_asyncio = main.asyncio
        cls._real_cascade = main.GeminiCascade

    @classmethod
    def tearDownClass(cls):
        cls.main.asyncio = cls._real_asyncio
        cls.main.GeminiCascade = cls._real_cascade

    async def asyncSetUp(self):
        m = self.main
        m.storage = self.storage = __import__("storage").Storage(":memory:")
        m.bot = self.bot = FakeBot()
        m.GeminiCascade = self.Cascade = FakeCascade
        m.asyncio = _AsyncioShim(self._real_asyncio)
        m._connections = {"cid": {
            "user_id": OWNER_ID, "user_chat_id": 77,
            "can_reply": True, "enabled": True,
        }}
        m._reply_times.clear()
        m._warned_no_rights.clear()
        m._STATUS_CACHE = {}
        m._NEXT_DUE_AT = None

    def force_due(self, chat_id=1):
        """Переводит дедлайн пачки в прошлое (эмуляция «прошло 10 минут»)."""
        with self.storage._lock, self.storage.db:
            self.storage.db.execute(
                "UPDATE secretary_pending SET due_at=? WHERE chat_id=?",
                (time.time() - 1, chat_id),
            )

    # ---------- группировка ----------

    async def test_five_messages_make_one_batch_and_no_reply_yet(self):
        for i in range(5):
            await self.main.on_business_message(
                make_message(text=f"сообщение {i}", message_id=100 + i)
            )
        row = self.storage.get_pending(1)
        self.assertIsNotNone(row)
        self.assertEqual(len(row["parts"]), 5)
        self.assertEqual(len(row["seen_ids"]), 5)
        self.assertEqual(self.bot.replies, [])
        self.assertEqual(self.storage.pending_stats()["pending_chats"], 1)

    async def test_each_message_shifts_deadline(self):
        await self.main.on_business_message(make_message(message_id=1))
        first = self.storage.get_pending(1)["due_at"]
        await self.main.on_business_message(make_message(text="второе", message_id=2))
        second = self.storage.get_pending(1)["due_at"]
        self.assertGreaterEqual(second, first)

    async def test_one_inbox_notification_per_batch(self):
        for i in range(5):
            await self.main.on_business_message(
                make_message(text=f"сообщение {i}", message_id=100 + i)
            )
        self.assertEqual(len(self.bot.notifications), 1)
        self.assertEqual(len(self.bot.edited), 4)  # остальные — редактирование
        self.assertIsNotNone(self.storage.get_pending(1)["notify_message_id"])

    async def test_duplicate_webhook_delivery_does_not_grow_batch(self):
        await self.main.on_business_message(make_message(message_id=1))
        await self.main.on_business_message(make_message(message_id=1))
        self.assertEqual(len(self.storage.get_pending(1)["parts"]), 1)
        self.assertEqual(len(self.bot.notifications), 1)

    # ---------- доставка ----------

    async def test_due_batch_produces_exactly_one_reply(self):
        for i in range(5):
            await self.main.on_business_message(
                make_message(text=f"сообщение {i}", message_id=100 + i)
            )
        self.force_due()
        await self.main.process_due()
        self.assertEqual(len(self.bot.replies), 1)
        self.assertIsNone(self.storage.get_pending(1))
        self.assertEqual(self.storage.pending_stats()["pending_chats"], 0)

    async def test_batch_text_contains_all_five_messages(self):
        for i in range(5):
            await self.main.on_business_message(
                make_message(text=f"сообщение {i}", message_id=100 + i)
            )
        self.force_due()
        await self.main.process_due()
        text = self.bot.replies[0][1]
        self.assertIn("Я сейчас занят", text)
        # Пачка сохранена ОДНОЙ репликой user со всеми пятью сообщениями.
        history = self.storage.get_history(1, 20)
        user_rows = [h for h in history if h["role"] == "user"]
        self.assertEqual(len(user_rows), 1)
        for i in range(5):
            self.assertIn(f"сообщение {i}", user_rows[0]["text"])

    async def test_history_has_one_user_and_one_model(self):
        for i in range(3):
            await self.main.on_business_message(
                make_message(text=f"сообщение {i}", message_id=100 + i)
            )
        self.force_due()
        await self.main.process_due()
        history = self.storage.get_history(1, 20)
        self.assertEqual([h["role"] for h in history], ["user", "model"])

    # ---------- отмена владельцем ----------

    async def test_owner_reply_cancels_only_this_chat(self):
        await self.main.on_business_message(make_message(chat_id=1, message_id=1))
        await self.main.on_business_message(make_message(chat_id=2, message_id=2))
        await self.main.on_business_message(
            make_message(chat_id=1, sender_id=OWNER_ID, text="сам отвечу", message_id=3)
        )
        self.assertIsNone(self.storage.get_pending(1))
        self.assertIsNotNone(self.storage.get_pending(2))
        self.assertEqual(self.storage.pending_stats()["pending_chats"], 1)
        self.assertTrue(any("Вы ответили сами" in e[2] for e in self.bot.edited))

    async def test_owner_reply_updates_last_owner_at(self):
        await self.main.on_business_message(
            make_message(sender_id=OWNER_ID, text="привет", message_id=1)
        )
        raw = self.storage.get_meta("last_owner_at")
        self.assertIsNotNone(raw)
        self.assertAlmostEqual(float(raw), time.time(), delta=5)

    # ---------- ночь и откат ----------

    async def test_night_instruction_reaches_prompt(self):
        self.storage.set_settings_many({
            "night_enabled": "1", "night_start": "00:00", "night_end": "23:58",
        })
        await self.main.on_business_message(make_message(message_id=1))
        self.force_due()
        # Перехватываем system, который уйдёт в GeminiCascade.
        captured = {}

        class CapturingCascade(FakeCascade):
            async def generate(self, system, history, text, media=None):
                captured["system"] = system
                return await super().generate(system, history, text, media)

        self.main.GeminiCascade = CapturingCascade
        await self.main.process_due()
        self.assertIn("спит", captured.get("system", ""))
        self.assertIn("утром", captured.get("system", ""))

    async def test_smart_replies_off_replies_immediately(self):
        self.storage.set_setting("smart_replies", "0")
        await self.main.on_business_message(make_message(message_id=1))
        self.assertEqual(len(self.bot.replies), 1)
        self.assertIsNone(self.storage.get_pending(1))

    async def test_disabled_autoanswers_drop_batch(self):
        self.storage.set_setting("enabled", "0")
        await self.main.on_business_message(make_message(message_id=1))
        self.assertIsNone(self.storage.get_pending(1))
        self.assertEqual(self.bot.replies, [])

    # ---------- «прочитал, но намеренно не ответил» --------------------------

    async def test_owner_read_by_id_cancels_batch(self):
        for i in range(5):
            await self.main.on_business_message(
                make_message(text=f"сообщение {i}", message_id=100 + i)
            )
        result = await self.main.owner_seen_provider(1, 104, None)
        self.assertTrue(result["cancelled"])
        self.assertIsNone(self.storage.get_pending(1))
        self.assertEqual(self.bot.replies, [])
        self.assertIsNotNone(self.storage.get_meta("owner_seen_at"))
        self.assertTrue(any("прочитали" in e[2].lower() for e in self.bot.edited))

    async def test_owner_read_by_time_cancels_when_ids_mismatch(self):
        """Страховка по времени, если шкалы message_id Bot API/MTProto разошлись."""
        await self.main.on_business_message(make_message(message_id=900))
        row = self.storage.get_pending(1)
        result = await self.main.owner_seen_provider(1, 1, float(row["last_at"]) + 1)
        self.assertTrue(result["cancelled"])
        self.assertIsNone(self.storage.get_pending(1))

    async def test_read_before_new_messages_does_not_cancel(self):
        """Прочитал старое, потом пришли новые — автоответ всё ещё нужен."""
        await self.main.on_business_message(make_message(message_id=1))
        await self.main.on_business_message(make_message(text="второе", message_id=2))
        row = self.storage.get_pending(1)
        result = await self.main.owner_seen_provider(
            1, 1, float(row["last_at"]) - 100
        )
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["reason"], "new_messages_after_read")
        self.assertIsNotNone(self.storage.get_pending(1))

    async def test_owner_read_is_idempotent(self):
        await self.main.on_business_message(make_message(message_id=1))
        first = await self.main.owner_seen_provider(1, 1, None)
        second = await self.main.owner_seen_provider(1, 1, None)
        self.assertTrue(first["cancelled"])
        self.assertFalse(second["cancelled"])
        self.assertEqual(second["reason"], "no_pending")

    async def test_owner_read_cancels_only_this_chat(self):
        await self.main.on_business_message(make_message(chat_id=1, message_id=1))
        await self.main.on_business_message(make_message(chat_id=2, message_id=2))
        await self.main.owner_seen_provider(1, 1, None)
        self.assertIsNone(self.storage.get_pending(1))
        self.assertIsNotNone(self.storage.get_pending(2))

    async def test_claimed_batch_not_cancelled(self):
        await self.main.on_business_message(make_message(message_id=1))
        with self.storage._lock, self.storage.db:
            self.storage.db.execute(
                "UPDATE secretary_pending SET claimed_at=?, claim_token=? WHERE chat_id=1",
                (time.time(), "token"),
            )
        result = await self.main.owner_seen_provider(1, 1, None)
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["reason"], "already_claimed")
        self.assertIsNotNone(self.storage.get_pending(1))

    async def test_owner_read_keeps_dialog_context(self):
        for i in range(3):
            await self.main.on_business_message(
                make_message(text=f"реплика {i}", message_id=10 + i)
            )
        await self.main.owner_seen_provider(1, 12, None)
        history = self.storage.get_history(1, 20)
        users = [h["text"] for h in history if h["role"] == "user"]
        self.assertEqual(len(users), 1)
        self.assertIn("реплика 2", users[0])

    async def test_owner_read_does_not_change_last_owner_at(self):
        await self.main.on_business_message(make_message(message_id=1))
        await self.main.owner_seen_provider(1, 1, None)
        self.assertIsNone(self.storage.get_meta("last_owner_at"))


if __name__ == "__main__":
    unittest.main()
