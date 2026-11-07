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

OWNER_NAME = os.environ.get("OWNER_NAME", "владельца")
BOT_TIMEZONE = os.environ.get("BOT_TIMEZONE", "Asia/Omsk")
TAKEOVER_MINUTES = int(os.environ.get("BUSINESS_TAKEOVER_MINUTES", "60"))
MAX_REPLIES_PER_HOUR = int(os.environ.get("BUSINESS_MAX_REPLIES_PER_HOUR", "20"))
IGNORE_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("BUSINESS_IGNORE_IDS", ""))}

DB_PATH = os.environ.get("DB_PATH", "secretary.db")
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

HISTORY_LIMIT = 12
PERSONA_LIMIT = 800
