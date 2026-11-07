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


if __name__ == "__main__":
    unittest.main()
