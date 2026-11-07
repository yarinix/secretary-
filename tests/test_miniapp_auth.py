"""Тесты проверки initData Telegram Mini App."""

import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import parse_qsl, urlencode

from miniapp_auth import (
    AuthError,
    data_check_string,
    require_owner,
    verify_init_data,
    webapp_secret,
)

BOT_TOKEN = "123456789:AAFakeTokenForTests_xxxxxxxxxxxxxxxxxx"
OWNER_ID = 5084782149
NOW = 1_700_000_000.0


def make_init_data(bot_token=BOT_TOKEN, user_id=OWNER_ID, auth_date=None,
                   extra=None, corrupt_hash=False, drop=None):
    """Независимая сборка подписанной строки (не использует тестируемый код)."""
    pairs = {
        "auth_date": str(int(NOW if auth_date is None else auth_date)),
        "query_id": "AAHdF6IQAAAAANedF6I",
        "user": json.dumps(
            {"id": user_id, "first_name": "Артём", "language_code": "ru"},
            ensure_ascii=False, separators=(",", ":"),
        ),
    }
    if extra:
        pairs.update(extra)
    for key in (drop or []):
        pairs.pop(key, None)
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if corrupt_hash:
        digest = "0" * 64
    return urlencode({**pairs, "hash": digest})


class TestHelpers(unittest.TestCase):
    def test_secret_derivation(self):
        expected = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        self.assertEqual(webapp_secret(BOT_TOKEN), expected)
        self.assertNotEqual(webapp_secret(BOT_TOKEN), webapp_secret(BOT_TOKEN + "x"))

    def test_data_check_string_sorted_and_without_hash(self):
        pairs = {"b": "2", "a": "1", "hash": "deadbeef"}
        self.assertEqual(data_check_string(pairs), "a=1\nb=2")


class TestVerify(unittest.TestCase):
    def test_valid(self):
        parsed = verify_init_data(make_init_data(), BOT_TOKEN, now=NOW)
        self.assertEqual(parsed["user"]["id"], OWNER_ID)
        self.assertEqual(parsed["auth_date"], int(NOW))
        self.assertNotIn("hash", parsed)

    def test_valid_from_header_style_string(self):
        raw = make_init_data()
        self.assertEqual(parse_qsl(raw)[0][0], "auth_date")
        self.assertIn("hash", dict(parse_qsl(raw)))

    def test_tampered_hash(self):
        with self.assertRaises(AuthError) as ctx:
            verify_init_data(make_init_data(corrupt_hash=True), BOT_TOKEN, now=NOW)
        self.assertEqual(ctx.exception.status, 401)

    def test_tampered_user_id_keeps_old_signature(self):
        raw = make_init_data(user_id=OWNER_ID)
        pairs = dict(parse_qsl(raw))
        pairs["user"] = json.dumps({"id": 999999, "first_name": "Хакер"})
        forged = urlencode(pairs)
        with self.assertRaises(AuthError) as ctx:
            verify_init_data(forged, BOT_TOKEN, now=NOW)
        self.assertEqual(ctx.exception.status, 401)

    def test_wrong_bot_token(self):
        with self.assertRaises(AuthError) as ctx:
            verify_init_data(make_init_data(), BOT_TOKEN + "x", now=NOW)
        self.assertEqual(ctx.exception.status, 401)

    def test_expired(self):
        raw = make_init_data(auth_date=NOW - 7200)
        with self.assertRaises(AuthError) as ctx:
            verify_init_data(raw, BOT_TOKEN, now=NOW, ttl=3600)
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("устарел", ctx.exception.message)

    def test_future(self):
        raw = make_init_data(auth_date=NOW + 10_000)
        with self.assertRaises(AuthError):
            verify_init_data(raw, BOT_TOKEN, now=NOW)

    def test_small_clock_skew_tolerated(self):
        raw = make_init_data(auth_date=NOW + 30)
        self.assertEqual(verify_init_data(raw, BOT_TOKEN, now=NOW)["user"]["id"], OWNER_ID)

    def test_custom_ttl(self):
        stale = make_init_data(auth_date=NOW - 10)
        with self.assertRaises(AuthError):
            verify_init_data(stale, BOT_TOKEN, now=NOW, ttl=5)
        fresh = make_init_data(auth_date=NOW - 3)
        self.assertEqual(verify_init_data(fresh, BOT_TOKEN, now=NOW, ttl=5)["user"]["id"], OWNER_ID)

    def test_missing_hash(self):
        # Убираем hash из уже подписанной строки, остальные поля оставляем.
        pairs = dict(parse_qsl(make_init_data()))
        pairs.pop("hash")
        raw = urlencode(pairs)
        with self.assertRaises(AuthError) as ctx:
            verify_init_data(raw, BOT_TOKEN, now=NOW)
        self.assertEqual(ctx.exception.status, 401)

    def test_missing_auth_date(self):
        raw = make_init_data(drop=["auth_date"])
        with self.assertRaises(AuthError) as ctx:
            verify_init_data(raw, BOT_TOKEN, now=NOW)
        self.assertIn(ctx.exception.status, (400, 401))

    def test_missing_user(self):
        raw = make_init_data(drop=["user"])
        with self.assertRaises(AuthError) as ctx:
            verify_init_data(raw, BOT_TOKEN, now=NOW)
        self.assertEqual(ctx.exception.status, 400)

    def test_broken_user_json(self):
        raw = make_init_data(extra={"user": "не json"})
        with self.assertRaises(AuthError) as ctx:
            verify_init_data(raw, BOT_TOKEN, now=NOW)
        self.assertEqual(ctx.exception.status, 400)

    def test_empty_init_data(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw), self.assertRaises(AuthError):
                verify_init_data(raw, BOT_TOKEN, now=NOW)

    def test_too_long_init_data(self):
        with self.assertRaises(AuthError) as ctx:
            verify_init_data("a" * 9000, BOT_TOKEN, now=NOW)
        self.assertEqual(ctx.exception.status, 400)

    def test_no_bot_token_configured(self):
        with self.assertRaises(AuthError) as ctx:
            verify_init_data(make_init_data(), "", now=NOW)
        self.assertEqual(ctx.exception.status, 500)


class TestRequireOwner(unittest.TestCase):
    def test_owner_ok(self):
        self.assertIsNone(require_owner(OWNER_ID, OWNER_ID))

    def test_another_user_forbidden(self):
        with self.assertRaises(AuthError) as ctx:
            require_owner(123, OWNER_ID)
        self.assertEqual(ctx.exception.status, 403)

    def test_zero_id_forbidden(self):
        with self.assertRaises(AuthError):
            require_owner(0, OWNER_ID)


if __name__ == "__main__":
    unittest.main()
