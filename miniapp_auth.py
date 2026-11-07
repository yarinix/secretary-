"""Проверка `initData` Telegram Mini App.

ЧИСТЫЙ МОДУЛЬ: только стандартная библиотека, без config/aiohttp/aiogram.

Алгоритм (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app):
  secret = HMAC_SHA256(key="WebAppData", msg=BOT_TOKEN)
  hash   = HMAC_SHA256(key=secret, msg=data_check_string)
  data_check_string = отсортированные по ключу пары "k=v" через "\\n",
                      без поля `hash`.

`initDataUnsafe` НЕ используется: это данные без подписи, им нельзя верить.
Проверяем также свежесть auth_date, иначе украденную строку можно
переиспользовать бесконечно. Полный initData и BOT_TOKEN никогда не логируем.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class AuthError(Exception):
    """Ошибка авторизации с готовым HTTP-статусом для webapp."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


def webapp_secret(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", (bot_token or "").encode("utf-8"), hashlib.sha256).digest()


def data_check_string(pairs: dict[str, str]) -> str:
    return "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()) if k != "hash")


def verify_init_data(
    init_data: str,
    bot_token: str,
    *,
    now: float | None = None,
    ttl: int = 3600,
) -> dict:
    """Возвращает разобранные поля (включая `user` как dict) или бросает AuthError.

    now/ttl вынесены в аргументы, чтобы тесты не зависели от реального времени.
    """
    if not isinstance(init_data, str) or not init_data.strip():
        raise AuthError("нет initData", 401)
    if not bot_token:
        raise AuthError("BOT_TOKEN не задан на сервере", 500)

    raw = init_data.strip()
    if len(raw) > 8192:
        raise AuthError("initData слишком длинный", 400)

    pairs: dict[str, str] = dict(parse_qsl(raw, keep_blank_values=True))
    if not pairs:
        raise AuthError("initData пустой или не разбирается", 400)

    received = pairs.get("hash", "")
    if not received:
        raise AuthError("нет подписи hash", 401)

    expected = hmac.new(
        webapp_secret(bot_token),
        data_check_string(pairs).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    # compare_digest — защита от сравнения по времени.
    if not hmac.compare_digest(expected, received):
        raise AuthError("подпись initData не совпала", 401)

    # --- свежесть (проверяем ПОСЛЕ подписи, чтобы не орать на подделки) ---
    try:
        auth_date = int(pairs.get("auth_date", ""))
    except (TypeError, ValueError):
        raise AuthError("некорректный auth_date", 400)

    current = time.time() if now is None else now
    # Небольшой допуск на расхождение часов; «далеко в будущем» всё равно reject.
    if auth_date > current + 60:
        raise AuthError("initData из будущего", 401)
    if current - auth_date > ttl:
        raise AuthError("initData устарел, откройте приложение заново", 401)

    # --- пользователь ---
    user_raw = pairs.get("user")
    if not user_raw:
        raise AuthError("в initData нет пользователя", 400)
    try:
        user = json.loads(user_raw)
    except (json.JSONDecodeError, TypeError):
        raise AuthError("поле user повреждено", 400)
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise AuthError("поле user без корректного id", 400)

    result = dict(pairs)
    result.pop("hash", None)  # не сериализуем подпись дальше
    result["user"] = user
    result["auth_date"] = auth_date
    return result


def require_owner(user_id: int, owner_id: int) -> None:
    """Только владелец бота может читать и менять настройки."""
    if int(user_id or 0) != int(owner_id or 0):
        raise AuthError("доступ только для владельца бота", 403)
