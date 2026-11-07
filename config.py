"""Конфигурация и жёсткая валидация окружения при старте.

ВАЖНО про имя переменной: ANTHROPIC_API_KEY исторически осталась в Render,
но Claude не подключён — в этой переменной лежит ключ GEMINI. Название
сохранено специально по просьбе владельца, чтобы не переделывать деплой.
GEMINI_API_KEY — опциональный «правильный» алиас, имеет приоритет, если задан.
"""

import hashlib
import os
import re

from helpers import clean_token
from llm import DEFAULT_MODELS

TOKEN = clean_token(os.environ.get("BOT_TOKEN", ""))
if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", TOKEN):
    raise SystemExit(
        f"BOT_TOKEN выглядит неверно (длина {len(TOKEN)}, двоеточий: {TOKEN.count(':')}). "
        "Нужен токен из @BotFather вида 123456789:AAxxxx..."
    )

# P0: владелец задаётся явно. Авто-присвоение через /start убрано — иначе
# первый написавший боту забирал управление и блокировал подключение Business.
OWNER_ID = int(re.sub(r"\D", "", os.environ.get("OWNER_ID", "")) or 0)
if not OWNER_ID:
    raise SystemExit(
        "OWNER_ID обязателен. Укажите ваш числовой Telegram ID (@userinfobot), "
        "иначе бот не сможет отличить вас от постороннего."
    )

GEMINI_API_KEY = (
    os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ""
).strip()

_raw_models = os.environ.get("GEMINI_MODELS", "")
MODELS = [m.strip() for m in _raw_models.split(",") if m.strip()] or list(DEFAULT_MODELS)

def _env_int(key: str, fallback: int) -> int:
    """Опечатка в числовой переменной не должна ронять сервис при старте."""
    try:
        return int(os.environ.get(key, fallback))
    except (TypeError, ValueError):
        return fallback


OWNER_NAME = os.environ.get("OWNER_NAME", "владельца")
BOT_TIMEZONE = os.environ.get("BOT_TIMEZONE", "Asia/Omsk")
MAX_REPLIES_PER_HOUR = _env_int("BUSINESS_MAX_REPLIES_PER_HOUR", 20)
IGNORE_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("BUSINESS_IGNORE_IDS", ""))}

# Умные отложенные ответы. Это лишь ДЕФОЛТЫ для пустой базы: как только
# владелец сохранил значение в Mini App, источник истины — Postgres.
SMART_REPLIES = os.environ.get("BUSINESS_SMART_REPLIES", "1")
REPLY_DELAY_MINUTES = _env_int("BUSINESS_REPLY_DELAY_MINUTES", 10)
AWAY_MINUTES = _env_int("BUSINESS_AWAY_MINUTES", 60)
AWAY_COLLECT_SECONDS = _env_int("BUSINESS_AWAY_COLLECT_SECONDS", 15)
NIGHT_ENABLED = os.environ.get("BOT_NIGHT_ENABLED", "1")
NIGHT_START = os.environ.get("BOT_NIGHT_START", "01:00")
NIGHT_END = os.environ.get("BOT_NIGHT_END", "08:00")
NIGHT_HINT = os.environ.get("BOT_NIGHT_HINT", "")

DB_PATH = os.environ.get("DB_PATH", "secretary.db")

# Внешняя БД (Supabase / Neon / Render Postgres). Если задана — настройки,
# история и подключения живут вне эфемерного диска Render Free и переживают
# рестарт/сон. Если пусто — локальный SQLite (совместимость, но данные сбросятся).
# DSN содержит пароль: никуда не логируется и не отдаётся в Mini App.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Раньше секрет по умолчанию был «change-me» — зная его, можно было подделать
# апдейты. Теперь при отсутствии переменной берём непредсказуемый хэш токена.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or hashlib.sha256(
    TOKEN.encode()
).hexdigest()[:32]
WEBHOOK_PATH = "/webhook"
BASE_URL = (
    os.environ.get("WEBHOOK_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
).rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))

# Mini App живёт на том же домене, что и вебхук. На проде Render подставляет
# RENDER_EXTERNAL_URL автоматически → https://secretary-bot-htem.onrender.com/app.
# WEBAPP_URL нужен только если BASE_URL не задан (например, кастомный домен).
APP_PATH = "/app"
APP_URL = (
    os.environ.get("WEBAPP_URL") or (BASE_URL + APP_PATH if BASE_URL else "")
).strip().rstrip("/")

HISTORY_LIMIT = 12
PERSONA_LIMIT = 800
