import logging
import os
import re
import sqlite3
import time

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import BusinessConnection, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("secretary")

def clean_token(raw: str) -> str:
    token = "".join(raw.split())  # убрать любые пробелы, переносы, табы
    for junk in ("​", "‌", "‍", "﻿", " "):
        token = token.replace(junk, "")
    if "=" in token:  # вставили "BOT_TOKEN=123:ABC"
        token = token.split("=", 1)[1]
    return token.strip("'\"")


TOKEN = clean_token(os.environ.get("BOT_TOKEN", ""))
if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", TOKEN):
    raise SystemExit(
        f"BOT_TOKEN выглядит неверно (длина {len(TOKEN)}, двоеточий: {TOKEN.count(':')}). "
        "Нужен токен из @BotFather вида 123456789:AAxxxx..."
    )
ENV_OWNER_ID = int(re.sub(r"\D", "", os.environ.get("OWNER_ID", "")) or 0)
SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("MODEL", "claude-haiku-5-5")
HANDOVER_MINUTES = int(os.environ.get("HANDOVER_MINUTES", "30"))
DB_PATH = os.environ.get("DB_PATH", "secretary.db")
WEBHOOK_PATH = "/webhook"

DEFAULT_INSTRUCTIONS = (
    "Ты — вежливый личный секретарь владельца аккаунта. Отвечай коротко, по-человечески, "
    "на языке собеседника. Сразу честно скажи, что ты ассистент, если спросят. "
    "Владелец сейчас недоступен: уточни суть вопроса и пообещай передать сообщение. "
    "Ничего не обещай от имени владельца (деньги, сроки, встречи) и не раскрывай личные данные."
)
DEFAULT_AWAY = "Здравствуйте! Я секретарь. Передал ваше сообщение, владелец ответит, как освободится."

bot = Bot(TOKEN)
dp = Dispatcher()

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
db.execute(
    "CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, chat_id INTEGER, role TEXT, text TEXT)"
)
db.commit()

last_owner_activity: dict[int, float] = {}


def get_setting(key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    db.commit()


def owner_id() -> int:
    return ENV_OWNER_ID or int(get_setting("owner_id", "0") or 0)


def add_history(chat_id: int, role: str, text: str) -> None:
    db.execute("INSERT INTO history (chat_id, role, text) VALUES (?, ?, ?)", (chat_id, role, text))
    db.execute(
        "DELETE FROM history WHERE chat_id=? AND id NOT IN "
        "(SELECT id FROM history WHERE chat_id=? ORDER BY id DESC LIMIT 20)",
        (chat_id, chat_id),
    )
    db.commit()


def build_messages(chat_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT role, text FROM history WHERE chat_id=? ORDER BY id", (chat_id,)
    ).fetchall()
    messages: list[dict] = []
    for role, text in rows:
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + text
        else:
            messages.append({"role": role, "content": text})
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


async def ai_reply(chat_id: int) -> str:
    fallback = get_setting("away_text", DEFAULT_AWAY)
    if not ANTHROPIC_API_KEY:
        return fallback
    messages = build_messages(chat_id)
    if not messages:
        return fallback
    payload = {
        "model": MODEL,
        "max_tokens": 400,
        "system": get_setting("instructions", DEFAULT_INSTRUCTIONS),
        "messages": messages,
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://api.anthropic.com/v1/messages",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                data = await r.json()
                if r.status != 200:
                    log.error("Anthropic API error %s: %s", r.status, data)
                    return fallback
                return "".join(b.get("text", "") for b in data["content"]).strip() or fallback
    except Exception:
        log.exception("AI reply failed")
        return fallback


def is_owner(m: Message) -> bool:
    return bool(m.from_user) and m.from_user.id == owner_id()


# ---------- Business: переписка клиентов ----------

@dp.business_connection()
async def on_connection(c: BusinessConnection):
    if not ENV_OWNER_ID:
        set_setting("owner_id", str(c.user.id))
    state = "подключён" if c.is_enabled else "отключён"
    try:
        await bot.send_message(c.user_chat_id, f"Business-аккаунт {state}.")
    except Exception:
        log.exception("notify connection failed")


@dp.business_message()
async def on_business_message(m: Message):
    chat_id = m.chat.id
    text = m.text or m.caption or ""

    # Пишет сам владелец — запоминаем, секретарь замолкает на время
    if m.from_user and m.from_user.id == owner_id():
        last_owner_activity[chat_id] = time.time()
        if text:
            add_history(chat_id, "assistant", text)
        return

    name = m.from_user.full_name if m.from_user else "Кто-то"
    shown = text or "[сообщение без текста]"
    add_history(chat_id, "user", shown)

    if owner_id():
        try:
            await bot.send_message(owner_id(), f"💬 {name}: {shown}")
        except Exception:
            log.exception("notify owner failed")

    if get_setting("enabled", "1") != "1":
        return
    if time.time() - last_owner_activity.get(chat_id, 0) < HANDOVER_MINUTES * 60:
        return
    if not text:
        return

    reply = await ai_reply(chat_id)
    await bot.send_message(chat_id, reply, business_connection_id=m.business_connection_id)
    add_history(chat_id, "assistant", reply)


# ---------- Управление: личка с ботом ----------

@dp.message(Command("start"))
async def start(m: Message):
    if not owner_id() and m.from_user:
        set_setting("owner_id", str(m.from_user.id))
    if not is_owner(m):
        return
    await m.answer(
        "Я секретарь. Подключи меня: Настройки Telegram → Telegram для бизнеса → Чат-боты.\n\n"
        "/on, /off — включить/выключить автоответы\n"
        "/status — состояние\n"
        "/instructions текст — как мне вести себя\n"
        "/away текст — стандартный ответ без ИИ\n"
        f"Твой ID: {m.from_user.id}"
    )


@dp.message(Command("on"))
async def on(m: Message):
    if is_owner(m):
        set_setting("enabled", "1")
        await m.answer("Автоответы включены.")


@dp.message(Command("off"))
async def off(m: Message):
    if is_owner(m):
        set_setting("enabled", "0")
        await m.answer("Автоответы выключены. Сообщения я всё равно буду пересылать сюда.")


@dp.message(Command("status"))
async def status(m: Message):
    if not is_owner(m):
        return
    await m.answer(
        f"Автоответы: {'вкл' if get_setting('enabled', '1') == '1' else 'выкл'}\n"
        f"ИИ: {'да (' + MODEL + ')' if ANTHROPIC_API_KEY else 'нет, только стандартный ответ'}\n"
        f"Пауза после твоего ответа в чате: {HANDOVER_MINUTES} мин"
    )


@dp.message(Command("instructions"))
async def instructions(m: Message, command: CommandObject):
    if not is_owner(m):
        return
    if not command.args:
        await m.answer(get_setting("instructions", DEFAULT_INSTRUCTIONS))
        return
    set_setting("instructions", command.args)
    await m.answer("Инструкции обновлены.")


@dp.message(Command("away"))
async def away(m: Message, command: CommandObject):
    if not is_owner(m):
        return
    if not command.args:
        await m.answer(get_setting("away_text", DEFAULT_AWAY))
        return
    set_setting("away_text", command.args)
    await m.answer("Стандартный ответ обновлён.")


async def on_startup(app: web.Application):
    if BASE_URL:
        await bot.set_webhook(
            BASE_URL + WEBHOOK_PATH,
            secret_token=SECRET,
            allowed_updates=dp.resolve_used_update_types(),
        )


async def health(_):
    return web.Response(text="ok")


def main():
    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dp, bot, secret_token=SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


if __name__ == "__main__":
    main()
