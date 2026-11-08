"""Тесты чистой политики отложенных ответов: теперь=явный аргумент, без freezegun."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import reply_policy as p


def at(hour, minute, tz="Asia/Omsk", day=8):
    """Epoch для заданного ЛОКАЛЬНОГО времени (ноябрь 2026, без DST)."""
    return datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo(tz)).timestamp()


def state(**over):
    base = {
        "smart_replies": "1",
        "reply_delay_minutes": 10,
        "away_minutes": 60,
        "away_collect_seconds": 15,
        "night_enabled": "1",
        "night_start": "01:00",
        "night_end": "08:00",
        "timezone": "Asia/Omsk",
        "night_hint": "",
        "owner_name": "Ярослав",
    }
    base.update(over)
    return base


class TestClockHelpers(unittest.TestCase):
    def test_parse_clock_valid(self):
        self.assertEqual(p.parse_clock("00:00"), (0, 0))
        self.assertEqual(p.parse_clock("01:00"), (1, 0))
        self.assertEqual(p.parse_clock("23:59"), (23, 59))
        self.assertEqual(p.parse_clock(" 07:05 "), (7, 5))

    def test_parse_clock_invalid(self):
        for bad in ("", None, "24:00", "12:60", "7:5", "12:00:00", "ab:cd", "1:00", "-1:00"):
            with self.subTest(bad=bad):
                self.assertIsNone(p.parse_clock(bad))

    def test_local_time_uses_zone_and_falls_back_to_utc(self):
        now = at(3, 30, tz="Asia/Omsk")
        self.assertEqual(p.local_time(now, "Asia/Omsk").strftime("%H:%M"), "03:30")
        self.assertEqual(p.local_time(now, "UTC").strftime("%H:%M"), "21:30")
        self.assertEqual(p.local_time(now, "Mars/Phobos").tzinfo,
                         p.local_time(now, "UTC").tzinfo)


class TestIsNight(unittest.TestCase):
    def test_default_bounds(self):
        for h, m, expected in ((0, 59, False), (1, 0, True), (3, 30, True),
                               (7, 59, True), (8, 0, False), (12, 0, False),
                               (23, 30, False)):
            with self.subTest(time=f"{h}:{m}"):
                self.assertEqual(
                    p.is_night(p.local_time(at(h, m), "Asia/Omsk"), "01:00", "08:00"),
                    expected,
                )

    def test_crosses_midnight(self):
        for h, m, expected in ((22, 59, False), (23, 0, True), (3, 0, True),
                               (6, 59, True), (7, 0, False), (12, 0, False)):
            with self.subTest(time=f"{h}:{m}"):
                self.assertEqual(
                    p.is_night(p.local_time(at(h, m), "Asia/Omsk"), "23:00", "07:00"),
                    expected,
                )

    def test_equal_bounds_mean_disabled(self):
        local = p.local_time(at(3, 0), "Asia/Omsk")
        self.assertFalse(p.is_night(local, "01:00", "01:00"))

    def test_broken_bounds_mean_disabled(self):
        local = p.local_time(at(3, 0), "Asia/Omsk")
        self.assertFalse(p.is_night(local, "99:99", "08:00"))
        self.assertFalse(p.is_night(local, "01:00", "нет"))


class TestNightActive(unittest.TestCase):
    def test_enabled_and_night(self):
        self.assertTrue(p.night_active(at(3, 0), state()))
        self.assertFalse(p.night_active(at(12, 0), state()))

    def test_disabled_even_at_night(self):
        self.assertFalse(p.night_active(at(3, 0), state(night_enabled="0")))

    def test_uses_configured_timezone(self):
        # 03:00 в Омске — ночь; тот же момент в UTC — утро (не ночь).
        now = at(3, 0, tz="Asia/Omsk")
        self.assertTrue(p.night_active(now, state(timezone="Asia/Omsk")))
        self.assertFalse(p.night_active(now, state(timezone="UTC")))

    def test_custom_bounds(self):
        self.assertTrue(p.night_active(at(23, 30), state(night_start="23:00", night_end="07:00")))


class TestPlanDeadline(unittest.TestCase):
    def test_no_last_owner_is_wait(self):
        # Первый деплой: нет доказательств отсутствия → не шлём «он спит» всем.
        self.assertEqual(p.plan_deadline(1000.0, None, state()), (600.0, "wait"))

    def test_recent_owner_is_wait(self):
        self.assertEqual(p.plan_deadline(1000.0, 900.0, state()), (600.0, "wait"))

    def test_stale_owner_is_away(self):
        now = 10000.0
        last = now - 60 * 60 - 1
        self.assertEqual(p.plan_deadline(now, last, state()), (15.0, "away"))

    def test_boundary_is_strict_greater_than(self):
        now = 10000.0
        last = now - 60 * 60  # ровно час — ещё НЕ отсутствие
        self.assertEqual(p.plan_deadline(now, last, state()), (600.0, "wait"))

    def test_smart_replies_off_is_immediate(self):
        self.assertEqual(p.plan_deadline(1000.0, None, state(smart_replies="0")), (0.0, "away"))
        self.assertEqual(p.plan_deadline(1000.0, 0.0, state(smart_replies="0")), (0.0, "away"))

    def test_custom_values(self):
        st = state(reply_delay_minutes=3, away_minutes=30, away_collect_seconds=40)
        self.assertEqual(p.plan_deadline(10000.0, 9900.0, st), (180.0, "wait"))
        self.assertEqual(p.plan_deadline(10000.0, 10000.0 - 30 * 60 - 1, st), (40.0, "away"))

    def test_zero_delay_is_allowed(self):
        self.assertEqual(p.plan_deadline(1000.0, None, state(reply_delay_minutes=0)), (0.0, "wait"))

    def test_broken_state_falls_back(self):
        st = state(reply_delay_minutes="нет", away_collect_seconds="нет")
        # Битая строка -> max(0, int(...)) упадёт в fallback.
        self.assertEqual(p.plan_deadline(1000.0, None, st), (600.0, "wait"))

    def test_future_last_owner_does_not_break(self):
        # Часы на сервере могли уехать: отрицательная разница -> wait.
        self.assertEqual(p.plan_deadline(1000.0, 5000.0, state()), (600.0, "wait"))


class TestBatchText(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(p.build_batch_text([]), "")
        self.assertEqual(p.build_batch_text(["", "  ", None]), "")

    def test_joins_in_order_and_strips(self):
        self.assertEqual(
            p.build_batch_text(["Иван: привет", " Иван: ты тут? "]),
            "Иван: привет\nИван: ты тут?",
        )

    def test_truncates_long_batch(self):
        text = p.build_batch_text(["x" * 5000, "y" * 5000])
        self.assertLessEqual(len(text), p._BATCH_LIMIT + 30)
        self.assertIn("дальше обрезано", text)


class TestNightInstruction(unittest.TestCase):
    def test_contains_owner_name_only(self):
        instruction = p.build_night_instruction("Ярослав", "")
        self.assertIn("Ярослав", instruction)
        self.assertNotIn("Иван", instruction)
        self.assertNotIn("first_name", instruction)

    def test_custom_hint_used(self):
        instruction = p.build_night_instruction("Ярослав", "скажи, что я в отъезде")
        self.assertIn("в отъезде", instruction)

    def test_empty_hint_uses_default(self):
        self.assertIn("сплю", p.build_night_instruction("Ярослав", ""))

    def test_empty_owner_name_falls_back(self):
        self.assertIn("владелец", p.build_night_instruction("", ""))

    def test_declares_sender_text_as_data(self):
        instruction = p.build_night_instruction("Ярослав", "")
        self.assertIn("данные разговора", instruction)


class TestCancelScope(unittest.TestCase):
    def test_same_chat_cancels(self):
        self.assertTrue(p.should_cancel_on_owner_reply(42, 42))

    def test_other_chat_keeps_its_batch(self):
        self.assertFalse(p.should_cancel_on_owner_reply(42, 43))
        self.assertFalse(p.should_cancel_on_owner_reply("42", 43))


class TestNoReplyToken(unittest.TestCase):
    def test_exact_token(self):
        self.assertTrue(p.is_no_reply("[[NO_REPLY]]"))
        self.assertTrue(p.is_no_reply(p.NO_REPLY_TOKEN))

    def test_tolerates_case_spaces_and_separators(self):
        for variant in ("[[no_reply]]", "[[ NO_REPLY ]]", "[[NO REPLY]]", "[[No-Reply]]",
                        "  [[NO_REPLY]]\n"):
            self.assertTrue(p.is_no_reply(variant), variant)

    def test_token_with_extra_text_still_silences(self):
        # Ложное молчание безопаснее, чем отправить собеседнику метку.
        self.assertTrue(p.is_no_reply("[[NO_REPLY]] Пожалуйста!"))
        self.assertTrue(p.is_no_reply("Не нужно. [[NO_REPLY]]"))

    def test_normal_reply_is_not_silenced(self):
        for text in ("Привет! Он ответит позже.", "no reply needed", "NO_REPLY", "", None,
                     "[NO_REPLY]", "[[REPLY]]"):
            self.assertFalse(p.is_no_reply(text), repr(text))


class TestCallbackData(unittest.TestCase):
    def test_roundtrip_for_every_action(self):
        for action in p.CALLBACK_ACTIONS:
            data = p.make_callback(action, 123456789)
            self.assertEqual(p.parse_callback(data), (action, 123456789))

    def test_fits_telegram_limit(self):
        # callback_data в Telegram: не больше 64 байт, chat_id бывает до 15 цифр.
        for action in p.CALLBACK_ACTIONS:
            self.assertLessEqual(len(p.make_callback(action, 10 ** 15 - 1).encode()), 64)

    def test_negative_chat_id_roundtrips(self):
        self.assertEqual(p.parse_callback(p.make_callback("mute", -100123)), ("mute", -100123))

    def test_unknown_action_rejected_on_make(self):
        with self.assertRaises(ValueError):
            p.make_callback("delete", 1)

    def test_garbage_is_rejected_on_parse(self):
        for bad in (None, "", "sec", "sec:skip", "sec:skip:", "sec:skip:abc",
                    "sec:explode:1", "x:skip:1", "sec:skip:1:2", 5):
            self.assertIsNone(p.parse_callback(bad), repr(bad))


class TestMute(unittest.TestCase):
    def test_default_is_three_hours(self):
        self.assertEqual(p.mute_until(1000.0), 1000.0 + 3 * 3600)

    def test_active_until_deadline(self):
        until = p.mute_until(1000.0)
        self.assertTrue(p.is_muted(repr(until), 1000.0))
        self.assertTrue(p.is_muted(str(until), until - 1))
        self.assertFalse(p.is_muted(str(until), until))
        self.assertFalse(p.is_muted(str(until), until + 1))

    def test_missing_or_broken_value_means_not_muted(self):
        for raw in (None, "", "0", "нет", "nan-not"):
            self.assertFalse(p.is_muted(raw, 1000.0), repr(raw))


class TestSkipRuleInPrompt(unittest.TestCase):
    def setUp(self):
        from helpers import build_prompt
        self.build = build_prompt

    def test_off_by_default_keeps_old_prompt(self):
        self.assertNotIn("NO_REPLY", self.build("Ярослав", "", "сейчас"))

    def test_enabled_adds_rule_with_the_same_token(self):
        prompt = self.build("Ярослав", "", "сейчас", allow_skip=True)
        self.assertIn(p.NO_REPLY_TOKEN, prompt)  # токен в промпте = токен в политике
        self.assertIn("благодарность", prompt)    # правило называет примеры
        self.assertIn("важнее остальных", prompt)

    def test_rule_comes_after_persona_and_night(self):
        prompt = self.build("Ярослав", "отвечай коротко", "сейчас",
                            "НОЧНОЕ ПРАВИЛО", allow_skip=True)
        self.assertGreater(prompt.index(p.NO_REPLY_TOKEN), prompt.index("НОЧНОЕ ПРАВИЛО"))
        self.assertGreater(prompt.index(p.NO_REPLY_TOKEN), prompt.index("отвечай коротко"))


if __name__ == "__main__":
    unittest.main()
