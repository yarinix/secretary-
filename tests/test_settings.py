"""Тесты чистой схемы настроек Mini App (без env, без aiogram/aiohttp)."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import app_settings as s
from storage import Storage


def fake_cfg(**overrides):
    base = dict(
        MODELS=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
        IGNORE_IDS={777000},
        OWNER_NAME="Артём",
        BOT_TIMEZONE="Asia/Omsk",
        MAX_REPLIES_PER_HOUR=20,
        HISTORY_LIMIT=12,
        PERSONA_LIMIT=800,
        SMART_REPLIES="1",
        REPLY_DELAY_MINUTES=10,
        AWAY_MINUTES=60,
        AWAY_COLLECT_SECONDS=15,
        NIGHT_ENABLED="1",
        NIGHT_START="01:00",
        NIGHT_END="08:00",
        NIGHT_HINT="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestBuildDefaults(unittest.TestCase):
    def test_from_object(self):
        d = s.build_defaults(fake_cfg())
        self.assertEqual(d["reply_delay_minutes"], 10)
        self.assertEqual(d["away_minutes"], 60)
        self.assertEqual(d["away_collect_seconds"], 15)
        self.assertEqual(d["smart_replies"], "1")
        self.assertEqual(d["skip_unneeded"], "1")
        self.assertEqual(d["night_enabled"], "1")
        self.assertEqual(d["night_start"], "01:00")
        self.assertEqual(d["night_end"], "08:00")
        self.assertEqual(d["ignore_ids"], [777000])
        self.assertEqual(d["models"], ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])
        self.assertEqual(d["enabled"], "1")
        self.assertEqual(d["persona"], "")
        self.assertEqual(d["timezone"], "Asia/Omsk")

    def test_from_dict_and_bad_values(self):
        d = s.build_defaults({
            "BOT_TIMEZONE": "Mars/Phobos",
            "REPLY_DELAY_MINUTES": "нет",
            "NIGHT_START": "99:99",
            "NIGHT_END": "нет",
        })
        self.assertEqual(d["timezone"], "Asia/Omsk")
        self.assertEqual(d["reply_delay_minutes"], 10)
        self.assertEqual(d["night_start"], "01:00")
        self.assertEqual(d["night_end"], "08:00")
        self.assertEqual(d["models"], ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])

    def test_env_overrides_new_keys(self):
        d = s.build_defaults(fake_cfg(
            SMART_REPLIES="0", REPLY_DELAY_MINUTES=3, AWAY_MINUTES=90,
            AWAY_COLLECT_SECONDS=30, NIGHT_ENABLED="0",
            NIGHT_START="23:30", NIGHT_END="07:15", NIGHT_HINT="скажи, что я в отъезде",
        ))
        self.assertEqual(d["smart_replies"], "0")
        self.assertEqual(d["reply_delay_minutes"], 3)
        self.assertEqual(d["away_minutes"], 90)
        self.assertEqual(d["away_collect_seconds"], 30)
        self.assertEqual(d["night_enabled"], "0")
        self.assertEqual(d["night_start"], "23:30")
        self.assertEqual(d["night_end"], "07:15")
        self.assertEqual(d["night_hint"], "скажи, что я в отъезде")

    def test_night_hint_sanitized_and_bounded(self):
        d = s.build_defaults(fake_cfg(NIGHT_HINT="привет\x00мир" + "x" * 500))
        self.assertNotIn("\x00", d["night_hint"])
        self.assertLessEqual(len(d["night_hint"]), 300)

    def test_empty_models_fallback(self):
        # Пустой список в env == «не задан» → полный каскад по умолчанию.
        d = s.build_defaults(fake_cfg(MODELS=[]))
        self.assertEqual(d["models"], ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])

    def test_owner_name_cleaned(self):
        d = s.build_defaults(fake_cfg(OWNER_NAME="Артём\n<script>"))
        self.assertNotIn("\n", d["owner_name"])
        self.assertEqual(d["owner_name"], "Артём <script>")


class TestEncodeDecode(unittest.TestCase):
    def setUp(self):
        self.d = s.build_defaults(fake_cfg())

    def test_roundtrip_all_types(self):
        values = {
            "enabled": "0", "inbox": "1", "notify": "0",
            "persona": "Отвечай коротко", "reply_delay_minutes": 10,
            "away_minutes": 60, "away_collect_seconds": 15,
            "smart_replies": "0", "night_enabled": "1",
            "night_start": "23:30", "night_end": "07:15",
            "night_hint": "ответь, что я сплю",
            "max_replies_per_hour": 50, "owner_name": "Артём",
            "timezone": "Europe/Moscow", "ignore_ids": [1, 2, 3],
            "models": ["a", "b"], "history_limit": 20, "persona_limit": 900,
        }
        for key, value in values.items():
            with self.subTest(key=key):
                raw = s.encode(key, value, self.d)
                self.assertIsInstance(raw, str)
                self.assertEqual(s.decode(key, raw, self.d), value)

    def test_bool_aliases(self):
        self.assertEqual(s.encode("enabled", True, self.d), "1")
        self.assertEqual(s.encode("enabled", "0", self.d), "0")
        self.assertEqual(s.decode("enabled", "1", self.d), "1")
        self.assertEqual(s.decode("enabled", "garbage", self.d), "0")

    def test_decode_bad_data_falls_back(self):
        self.assertEqual(s.decode("reply_delay_minutes", "нет", self.d), 10)
        self.assertEqual(s.decode("reply_delay_minutes", "9999", self.d), 10)
        self.assertEqual(s.decode("night_start", "99:99", self.d), "01:00")
        self.assertEqual(s.decode("night_end", "7:5", self.d), "08:00")
        self.assertEqual(s.decode("timezone", "Mars/Phobos", self.d), "Asia/Omsk")
        self.assertEqual(s.decode("models", "не json", self.d), self.d["models"])
        self.assertEqual(s.decode("ignore_ids", "[1, -2]", self.d), [1])
        self.assertEqual(s.decode("persona", None, self.d), "")

    def test_decode_keeps_model_order(self):
        raw = s.encode("models", ["z", "a", "m"], self.d)
        self.assertEqual(s.decode("models", raw, self.d), ["z", "a", "m"])

    def test_unknown_key_rejected(self):
        with self.assertRaises(KeyError):
            s.encode("BOT_TOKEN", "x", self.d)

    def test_clock_encode_rejects_bad_format(self):
        for bad in ("99:99", "7:5", "24:00", "12:60", "", "12:00:00", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    s.encode("night_start", bad, self.d)


class TestLoadState(unittest.TestCase):
    def setUp(self):
        self.d = s.build_defaults(fake_cfg())

    def test_env_is_fallback(self):
        self.assertEqual(s.load_state({}, self.d), self.d)

    def test_db_overrides_env(self):
        rows = {
            "reply_delay_minutes": "3", "smart_replies": "0",
            "night_start": "22:00", "night_hint": "сплю",
            "models": '["only-one"]', "enabled": "0",
        }
        st = s.load_state(rows, self.d)
        self.assertEqual(st["reply_delay_minutes"], 3)
        self.assertEqual(st["smart_replies"], "0")
        self.assertEqual(st["night_start"], "22:00")
        self.assertEqual(st["night_hint"], "сплю")
        self.assertEqual(st["models"], ["only-one"])
        self.assertEqual(st["enabled"], "0")
        self.assertEqual(st["max_replies_per_hour"], 20)  # не тронуто

    def test_broken_row_does_not_break_state(self):
        st = s.load_state({"reply_delay_minutes": "сломано", "night_end": "нет"}, self.d)
        self.assertEqual(st["reply_delay_minutes"], 10)
        self.assertEqual(st["night_end"], "08:00")

    def test_dead_takeover_row_ignored(self):
        # Продовская строка takeover_minutes=30 после обновления просто
        # игнорируется (ничего не ломается, разовая очистка — в SQL чек-листе).
        st = s.load_state({"takeover_minutes": "30"}, self.d)
        self.assertNotIn("takeover_minutes", st)
        self.assertEqual(st["reply_delay_minutes"], 10)


class TestValidatePatch(unittest.TestCase):
    def setUp(self):
        self.d = s.build_defaults(fake_cfg())

    def test_valid_full_patch(self):
        patch = {
            "enabled": "1", "inbox": "0", "notify": "1", "persona": "коротко",
            "smart_replies": "1", "reply_delay_minutes": "10",
            "away_minutes": "60", "away_collect_seconds": "15",
            "night_enabled": "1", "night_start": "01:00", "night_end": "08:00",
            "night_hint": "скажи, что я сплю",
            "max_replies_per_hour": "50", "owner_name": "Артём",
            "timezone": "Europe/Moscow", "ignore_ids": [1, 2],
            "models": ["m1", "m2"], "history_limit": "20", "persona_limit": "900",
        }
        self.assertEqual(s.validate_patch(patch, self.d), {})

    def test_int_bounds(self):
        cases = [
            ("reply_delay_minutes", -1), ("reply_delay_minutes", 241),
            ("away_minutes", 4), ("away_minutes", 721),
            ("away_collect_seconds", -1), ("away_collect_seconds", 121),
            ("max_replies_per_hour", -1), ("max_replies_per_hour", 101),
            ("history_limit", -1), ("history_limit", 51),
            ("persona_limit", 99), ("persona_limit", 2001),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.assertIn(key, s.validate_patch({key: value}, self.d))

    def test_valid_boundaries(self):
        patch = {
            "reply_delay_minutes": "0", "away_minutes": "5",
            "away_collect_seconds": "0", "max_replies_per_hour": "100",
            "history_limit": "50", "persona_limit": "2000",
        }
        self.assertEqual(s.validate_patch(patch, self.d), {})

    def test_non_integer_and_bool(self):
        self.assertIn("reply_delay_minutes", s.validate_patch({"reply_delay_minutes": "десять"}, self.d))
        self.assertIn("reply_delay_minutes", s.validate_patch({"reply_delay_minutes": True}, self.d))

    def test_clock_validation(self):
        for bad in ("99:99", "7:5", "24:00", "12:60", "", "12:00:00", None, 1200):
            with self.subTest(bad=bad):
                self.assertIn("night_start", s.validate_patch({"night_start": bad}, self.d))
        for good in ("00:00", "01:00", "23:59", "07:05"):
            with self.subTest(good=good):
                self.assertEqual(s.validate_patch({"night_start": good}, self.d), {})

    def test_clock_canonical_keeps_leading_zero(self):
        self.assertEqual(s.canonical({"night_start": "07:05"}, self.d)["night_start"], "07:05")

    def test_night_hint_uses_own_limit_not_persona_limit(self):
        # Раньше text-поля наследовали persona_limit; у night_hint свой max=300.
        self.assertEqual(s.validate_patch({"night_hint": "x" * 300}, self.d), {})
        self.assertIn("night_hint", s.validate_patch({"night_hint": "x" * 301}, self.d))
        patch = {"persona_limit": "2000", "night_hint": "y" * 500}
        self.assertIn("night_hint", s.validate_patch(patch, self.d))

    def test_night_hint_control_chars_stripped(self):
        self.assertEqual(
            s.canonical({"night_hint": "сплю\x00утром"}, self.d)["night_hint"],
            "сплюутром",
        )

    def test_unknown_and_readonly_keys_rejected(self):
        for key in ("BOT_TOKEN", "OWNER_ID", "api_key_set", "WEBHOOK_SECRET",
                    "whatever", "takeover_minutes"):
            with self.subTest(key=key):
                self.assertIn(key, s.validate_patch({key: "x"}, self.d))

    def test_patch_not_a_dict(self):
        self.assertIn("settings", s.validate_patch([1, 2], self.d))

    def test_timezone_mars_rejected(self):
        self.assertIn("timezone", s.validate_patch({"timezone": "Mars/Phobos"}, self.d))

    def test_timezone_from_list_ok(self):
        self.assertEqual(s.validate_patch({"timezone": "UTC"}, self.d), {})

    def test_models_empty_invalid_duplicate(self):
        self.assertIn("models", s.validate_patch({"models": []}, self.d))
        self.assertIn("models", s.validate_patch({"models": ["a", "a"]}, self.d))
        self.assertIn("models", s.validate_patch({"models": ["bad name"]}, self.d))
        self.assertIn("models", s.validate_patch({"models": ["m"] * 9}, self.d))

    def test_models_keep_order(self):
        patch = {"models": ["z", "a", "m"]}
        self.assertEqual(s.canonical(patch, self.d)["models"], '["z","a","m"]')

    def test_ignore_ids_validation(self):
        self.assertIn("ignore_ids", s.validate_patch({"ignore_ids": [0]}, self.d))
        self.assertIn("ignore_ids", s.validate_patch({"ignore_ids": ["abc"]}, self.d))
        self.assertIn("ignore_ids", s.validate_patch({"ignore_ids": "123"}, self.d))
        self.assertIn("ignore_ids", s.validate_patch({"ignore_ids": list(range(1, 102))}, self.d))
        # дубли убираются, а не считаются ошибкой
        self.assertEqual(s.canonical({"ignore_ids": [5, 5, 7]}, self.d)["ignore_ids"], "[5,7]")

    def test_persona_over_limit(self):
        self.assertIn("persona", s.validate_patch({"persona": "x" * 801}, self.d))

    def test_persona_uses_raised_limit_from_same_patch(self):
        patch = {"persona": "x" * 900, "persona_limit": "1000"}
        self.assertEqual(s.validate_patch(patch, self.d), {})

    def test_owner_name_cleaned_and_bounded(self):
        self.assertIn("owner_name", s.validate_patch({"owner_name": ""}, self.d))
        self.assertIn("owner_name", s.validate_patch({"owner_name": "x" * 41}, self.d))
        self.assertEqual(s.canonical({"owner_name": "Иван\nПетров"}, self.d)["owner_name"], "Иван Петров")

    def test_control_chars_stripped_from_persona(self):
        raw = {"persona": "привет\x00мир"}
        self.assertEqual(s.canonical(raw, self.d)["persona"], "приветмир")


class TestAllOrNothing(unittest.TestCase):
    def setUp(self):
        self.d = s.build_defaults(fake_cfg())

    def test_canonical_only_after_valid_patch(self):
        patch = {"reply_delay_minutes": "10", "max_replies_per_hour": "999"}
        self.assertIn("max_replies_per_hour", s.validate_patch(patch, self.d))
        # webapp в этом случае вообще не вызывает canonical/запись — проверяем
        # контракт: canonical всё ещё умеет кодировать валидную часть.
        self.assertEqual(s.canonical({"reply_delay_minutes": "10"}, self.d)["reply_delay_minutes"], "10")


class TestSchemaAndCascade(unittest.TestCase):
    def setUp(self):
        self.d = s.build_defaults(fake_cfg())

    def test_public_schema_shape(self):
        schema = s.public_schema(self.d)
        self.assertEqual(set(schema["fields"]), set(s.EDITABLE_KEYS))
        self.assertEqual(schema["fields"]["reply_delay_minutes"]["min"], 0)
        self.assertEqual(schema["fields"]["reply_delay_minutes"]["max"], 240)
        self.assertEqual(schema["fields"]["reply_delay_minutes"]["default"], 10)
        self.assertEqual(schema["fields"]["night_start"]["type"], "clock")
        self.assertEqual(schema["fields"]["night_end"]["default"], "08:00")
        self.assertEqual(schema["fields"]["night_hint"]["max"], 300)
        self.assertEqual(schema["fields"]["smart_replies"]["type"], "bool")
        self.assertNotIn("takeover_minutes", schema["fields"])
        self.assertIn("Asia/Omsk", schema["timezones"])
        self.assertIn("BOT_TOKEN", schema["readonly"])

    def test_cascade_kwargs(self):
        st = dict(self.d)
        st["models"] = ["one", "two"]
        kw = s.cascade_kwargs(st, "  secret  ")
        self.assertEqual(kw, {"api_key": "secret", "models": ["one", "two"]})

    def test_cascade_kwargs_never_empty(self):
        st = dict(self.d)
        st["models"] = []
        self.assertTrue(s.cascade_kwargs(st, "k")["models"])


class TestStorageBulk(unittest.TestCase):
    def test_atomic_bulk_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(str(Path(tmp) / "s.db"))
            self.assertEqual(db.get_all_settings(), {})
            db.set_settings_many({"a": "1", "b": "2", "c": "3"})
            self.assertEqual(db.get_all_settings(), {"a": "1", "b": "2", "c": "3"})
            db.set_settings_many({"a": "9"})
            self.assertEqual(db.get_all_settings()["a"], "9")

    def test_parallel_writes_under_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(str(Path(tmp) / "s.db"))
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda i: db.set_settings_many({f"k{i}": str(i)}), range(80)))
            self.assertEqual(len(db.get_all_settings()), 80)

    def test_wal_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(str(Path(tmp) / "s.db"))
            mode = db.db.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode, "wal")


class TestSkipUnneededSetting(unittest.TestCase):
    def test_env_zero_turns_it_off_by_default(self):
        self.assertEqual(s.build_defaults(fake_cfg(SKIP_UNNEEDED="0"))["skip_unneeded"], "0")

    def test_enabled_when_env_is_missing(self):
        self.assertEqual(s.build_defaults(fake_cfg())["skip_unneeded"], "1")

    def test_patch_roundtrip(self):
        d = s.build_defaults(fake_cfg())
        self.assertEqual(s.validate_patch({"skip_unneeded": "0"}, d), {})
        self.assertEqual(s.canonical({"skip_unneeded": "0"}, d), {"skip_unneeded": "0"})
        self.assertEqual(s.load_state({"skip_unneeded": "0"}, d)["skip_unneeded"], "0")

    def test_is_exposed_in_public_schema(self):
        d = s.build_defaults(fake_cfg())
        self.assertEqual(s.public_schema(d)["fields"]["skip_unneeded"]["type"], "bool")


if __name__ == "__main__":
    unittest.main()
