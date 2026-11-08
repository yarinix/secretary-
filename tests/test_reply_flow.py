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

import reply_policy as policy

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


class FakeCallback:
    """Нажатие инлайн-кнопки под уведомлением 📨 (owner chat = 77)."""

    def __init__(self, data, user_id=OWNER_ID, message_id=500, text="📨 Сообщений в пачке: 1"):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(
            message_id=message_id, text=text, chat=SimpleNamespace(id=77)
        )
        self.answers = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))


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

    # ---------- «ответ не нужен» ----------

    def silent_cascade(self):
        class Silent(FakeCascade):
            async def generate(self, system, history, text, media=None):
                await super().generate(system, history, text, media)
                return "[[NO_REPLY]]"
        return Silent

    async def test_no_reply_token_sends_nothing_but_keeps_history(self):
        self.main.GeminiCascade = self.silent_cascade()
        await self.main.on_business_message(make_message(text="спасибо!", message_id=1))
        self.force_due()
        await self.main.process_due()
        self.assertEqual(self.bot.replies, [])
        self.assertIsNone(self.storage.get_pending(1))
        history = self.storage.get_history(1, 20)
        self.assertEqual([h["role"] for h in history], ["user"])
        self.assertIn("спасибо!", history[0]["text"])
        # Владелец видит, что именно промолчали, а не пустой статус.
        notes = [e[2] for e in self.bot.edited if "Ответ не нужен" in e[2]]
        self.assertEqual(len(notes), 1)
        self.assertIn("спасибо!", notes[0])
        self.assertEqual(len(self.main._reply_times[1]), 0)  # лимит в час не тратится

    async def test_token_never_leaks_even_when_setting_is_off(self):
        self.storage.set_setting("skip_unneeded", "0")
        self.main.GeminiCascade = self.silent_cascade()
        await self.main.on_business_message(make_message(message_id=1))
        self.force_due()
        await self.main.process_due()
        self.assertEqual(self.bot.replies, [])

    async def test_skip_rule_in_prompt_only_when_enabled(self):
        systems = []

        class Capturing(FakeCascade):
            async def generate(self, system, history, text, media=None):
                systems.append(system)
                return await super().generate(system, history, text, media)

        self.main.GeminiCascade = Capturing
        await self.main.on_business_message(make_message(chat_id=1, message_id=1))
        self.force_due(1)
        await self.main.process_due()
        self.storage.set_setting("skip_unneeded", "0")
        await self.main.on_business_message(make_message(chat_id=2, message_id=2))
        self.force_due(2)
        await self.main.process_due()
        self.assertEqual(len(systems), 2)
        self.assertIn(policy.NO_REPLY_TOKEN, systems[0])
        self.assertNotIn("NO_REPLY", systems[1])

    # ---------- любое исходящее сообщение владельца отменяет пачку ----------

    async def test_owner_voice_message_cancels_pending(self):
        await self.main.on_business_message(make_message(chat_id=1, message_id=1))
        voice = make_message(chat_id=1, sender_id=OWNER_ID, text=None, message_id=2)
        voice.voice = SimpleNamespace(file_id="v1")
        await self.main.on_business_message(voice)
        self.assertIsNone(self.storage.get_pending(1))
        self.assertTrue(any("Вы ответили сами" in e[2] for e in self.bot.edited))
        self.assertIsNotNone(self.storage.get_meta("last_owner_at"))

    async def test_owner_sticker_without_text_cancels_pending(self):
        await self.main.on_business_message(make_message(chat_id=1, message_id=1))
        await self.main.on_business_message(
            make_message(chat_id=1, sender_id=OWNER_ID, text=None, message_id=2)
        )
        self.assertIsNone(self.storage.get_pending(1))

    # ---------- кнопки ----------

    async def test_skip_button_cancels_batch_and_remembers_it(self):
        await self.main.on_business_message(make_message(text="спасибо", message_id=1))
        cb = FakeCallback(policy.make_callback("skip", 1))
        await self.main.on_button(cb)
        self.assertIsNone(self.storage.get_pending(1))
        self.assertEqual(self.bot.replies, [])
        self.assertTrue(any("Отвечать не буду" in e[2] and "спасибо" in e[2]
                            for e in self.bot.edited))
        self.assertEqual([h["role"] for h in self.storage.get_history(1, 20)], ["user"])
        self.assertEqual(len(cb.answers), 1)

    async def test_skip_button_on_already_handled_batch_is_harmless(self):
        cb = FakeCallback(policy.make_callback("skip", 1))
        await self.main.on_button(cb)
        self.assertEqual(cb.answers[0][0], "Уже обработано")
        self.assertEqual(self.bot.edited, [])

    async def test_now_button_replies_without_waiting_for_deadline(self):
        await self.main.on_business_message(make_message(message_id=1))
        self.assertEqual(self.bot.replies, [])  # по умолчанию ждём тишины
        cb = FakeCallback(policy.make_callback("now", 1))
        await self.main.on_button(cb)
        self.assertEqual(len(self.bot.replies), 1)
        self.assertIsNone(self.storage.get_pending(1))
        self.assertEqual(len(cb.answers), 1)  # на нажатие ответили ровно один раз

    async def test_mute_button_silences_only_this_chat(self):
        await self.main.on_business_message(make_message(chat_id=1, message_id=1))
        await self.main.on_button(FakeCallback(policy.make_callback("mute", 1)))
        self.assertIsNone(self.storage.get_pending(1))
        await self.main.on_business_message(make_message(chat_id=1, text="ещё", message_id=2))
        self.assertIsNone(self.storage.get_pending(1))
        self.assertEqual(self.bot.replies, [])
        await self.main.on_business_message(make_message(chat_id=2, message_id=3))
        self.assertIsNotNone(self.storage.get_pending(2))
        self.assertTrue(any("заглушён" in e[2] for e in self.bot.edited))

    async def test_unmute_button_restores_chat(self):
        await self.main.on_button(FakeCallback(policy.make_callback("mute", 1)))
        await self.main.on_button(FakeCallback(policy.make_callback("unmute", 1)))
        await self.main.on_business_message(make_message(chat_id=1, message_id=1))
        self.assertIsNotNone(self.storage.get_pending(1))

    async def test_buttons_reject_other_users(self):
        await self.main.on_business_message(make_message(message_id=1))
        cb = FakeCallback(policy.make_callback("skip", 1), user_id=999)
        await self.main.on_button(cb)
        self.assertIsNotNone(self.storage.get_pending(1))  # пачка цела
        self.assertTrue(cb.answers[0][1])  # показано как предупреждение

    async def test_inbox_notification_has_three_buttons(self):
        sent = []
        real = self.bot.send_message

        async def spy(chat_id, text, **kw):
            sent.append(kw)
            return await real(chat_id, text, **kw)

        self.bot.send_message = spy
        await self.main.on_business_message(make_message(message_id=1))
        markup = sent[0]["reply_markup"]
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(
            sorted(policy.parse_callback(d)[0] for d in data), ["mute", "now", "skip"]
        )

    # ---------- устаревший ответ не отправляется ----------

    async def test_reply_not_sent_if_new_message_arrived_during_generation(self):
        main, texts = self.main, []

        class Racing(FakeCascade):
            async def generate(self, system, history, text, media=None):
                texts.append(text)
                # Пока модель думала, собеседник дописал: пачка пересоздалась.
                await main.on_business_message(
                    make_message(text="ещё одно", message_id=2)
                )
                return "Ответ"

        main.GeminiCascade = Racing
        await main.on_business_message(make_message(text="первое", message_id=1))
        self.force_due()
        await main.process_due()
        self.assertEqual(self.bot.replies, [])  # устаревший ответ не ушёл
        self.assertEqual(len(self.storage.get_pending(1)["parts"]), 2)  # ничего не потеряно

        self.force_due()
        await main.process_due()
        self.assertEqual(len(self.bot.replies), 1)  # один ответ на обе реплики
        self.assertIn("первое", texts[-1])
        self.assertIn("ещё одно", texts[-1])

    async def test_reply_not_sent_if_owner_answered_during_generation(self):
        main = self.main

        class Racing(FakeCascade):
            async def generate(self, system, history, text, media=None):
                await main.on_business_message(
                    make_message(chat_id=1, sender_id=OWNER_ID, text="сам", message_id=3)
                )
                return "Ответ"

        main.GeminiCascade = Racing
        await main.on_business_message(make_message(message_id=1))
        self.force_due()
        await main.process_due()
        self.assertEqual(self.bot.replies, [])
        self.assertIsNone(self.storage.get_pending(1))


if __name__ == "__main__":
    unittest.main()
