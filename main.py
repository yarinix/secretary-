"""Секретарь на Telegram Business: отвечает в личных чатах владельца от его лица.

Логика перенесена из ForUs (tgbot/business.py) и дополнена:
  * каскад моделей Gemini (как в ForUs/tgbot/llm.py) вместо одиночного вызова;
  * голосовые, кружки и фото снова обрабатываются (через inline_data Gemini);
  * владелец задаётся ТОЛЬКО через OWNER_ID — /start больше не захватывает бота;
  * владельцу приходит копия КАЖДОГО входящего, а не только копия автоответа;
  * system prompt не содержит имени собеседника (защита от инъекции через first_name);
  * в промпт возвращено текущее время (BOT_TIMEZONE).

Имя переменной ANTHROPIC_API_KEY сохранено исторически: в ней лежит ключ Gemini,
Claude не подключён. См. config.py.
"""

import asyncio
import base64
import logging
import time
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import BusinessConnection, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import config as cfg
from helpers import (
    build_prompt,
    current_time_str,
    sender_label,
    split_text,
    strip_markdown,
)
from llm import GeminiCascade
from storage import Storage

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("secretary")

bot = Bot(cfg.TOKEN)
dp = Dispatcher()
storage = Storage(cfg.DB_PATH)
cascade = GeminiCascade(cfg.GEMINI_API_KEY, cfg.MODELS)

_connections: dict[str, dict] = storage.load_connections()
_paused_until: dict[int, float] = {}
_reply_times: dict[int, deque] = defaultdict(
    lambda: deque(maxlen=cfg.MAX_REPLIES_PER_HOUR + 5)
)
_warned_no_rights: set[str] = set()


# ---------- хранилище (синхронный SQLite уводим из event loop) ----------

async def get_setting(key: str, default: str = "") -> str:
    return await asyncio.to_thread(storage.get_setting, key, default)


async def set_setting(key: str, value: str) -> None:
    await asyncio.to_thread(storage.set_setting, key, value)


def connection_info(c: BusinessConnection) -> dict:
    rights = getattr(c, "rights", None)
    can_reply = (
        bool(getattr(rights, "can_reply", False))
        if rights is not None
        else bool(getattr(c, "can_reply", True))
    )
    return {
        "user_id": int(c.user.id),
        "user_chat_id": int(c.user_chat_id),
        "can_reply": can_reply,
        "enabled": bool(getattr(c, "is_enabled", True)),
    }


async def get_connection(connection_id: str):
    info = _connections.get(connection_id)
    if info is not None:
        return info
    try:
        c = await bot.get_business_connection(connection_id)
    except Exception as e:
        log.warning("не удалось получить подключение %s: %s", connection_id, e)
        return None
    info = connection_info(c)
    _connections[connection_id] = info
    await asyncio.to_thread(storage.save_connection, connection_id, info)
    return info


async def notify_owner(info: dict, text: str) -> None:
    try:
        await bot.send_message(info["user_chat_id"], text)
    except Exception as e:
        log.warning("не удалось написать владельцу: %s", e)


# ---------- входящие сообщения ----------

MEDIA_LABELS = {
    "audio/ogg": "Голосовое сообщение",
    "video/mp4": "Видеосообщение",
    "image/jpeg": "Фотография",
}


async def collect_media(m: Message) -> tuple[str, dict | None]:
    """Текст + опциональный медиа-блок inline_data для Gemini."""
    text = (m.text or m.caption or "").strip()
    file_id = mime = None
    if m.voice:
        file_id, mime = m.voice.file_id, "audio/ogg"
    elif m.video_note:
        file_id, mime = m.video_note.file_id, "video/mp4"
    elif m.photo:
        file_id, mime = m.photo[-1].file_id, "image/jpeg"
    if not file_id:
        return text, None
    try:
        file = await bot.get_file(file_id)
        raw = await bot.download_file(file.file_path)
        data = raw.getvalue() if hasattr(raw, "getvalue") else raw.read()
        return text, {"mime_type": mime, "data": base64.b64encode(data).decode()}
    except Exception as e:
        log.warning("не удалось скачать вложение: %s", e)
        return text, None


def rate_ok(chat_id: int) -> bool:
    now = time.monotonic()
    times = _reply_times[chat_id]
    while times and now - times[0] > 3600:
        times.popleft()
    return len(times) < cfg.MAX_REPLIES_PER_HOUR


# ---------- Business: подключение ----------

@dp.business_connection()
async def on_business_connection(c: BusinessConnection):
    if c.user.id != cfg.OWNER_ID:
        log.warning("подключение от чужого пользователя (%s) проигнорировано", c.user.id)
        return
    info = connection_info(c)
    _connections[c.id] = info
    await asyncio.to_thread(storage.save_connection, c.id, info)
    _warned_no_rights.discard(c.id)
    if not info["enabled"]:
        await notify_owner(info, "🔌 Бот отключён от вашего аккаунта Telegram Business. Автоответы остановлены.")
    elif not info["can_reply"]:
        await notify_owner(
            info,
            "🔌 Бот подключён, но у него нет права отвечать. Включите в Настройки → "
            "Telegram для бизнеса → Чат-боты → «Отвечать на сообщения».",
        )
    else:
        on = await get_setting("enabled", "1") == "1"
        await notify_owner(
            info,
            "🔌 Бот подключён к вашему аккаунту и будет отвечать в личных чатах от вашего лица "
            f"(сейчас автоответы {'включены' if on else 'выключены'}).\n\n"
            f"Управление: /business. Если вы сами напишете в чат, бот замолчит там на "
            f"{cfg.TAKEOVER_MINUTES} мин.",
        )


# ---------- Business: сообщения ----------

@dp.business_message()
async def on_business_message(m: Message):
    conn_id = m.business_connection_id
    if not conn_id or m.chat.type != "private":
        return
    info = await get_connection(conn_id)
    if not info or info["user_id"] != cfg.OWNER_ID or not info["enabled"]:
        return
    if getattr(m, "sender_business_bot", None) is not None:
        return

    chat_id = m.chat.id
    sender = m.from_user

    # Владелец сам пишет в чате: запоминаем и замолкаем. Пауза только на
    # осмысленное сообщение — стикер больше не выключает бота на час.
    if sender is None or sender.id == cfg.OWNER_ID:
        own_text = (m.text or m.caption or "").strip()
        if own_text:
            await asyncio.to_thread(storage.save_message, chat_id, "model", own_text)
            _paused_until[chat_id] = time.monotonic() + cfg.TAKEOVER_MINUTES * 60
        return

    if sender.is_bot or sender.id in cfg.IGNORE_IDS:
        return

    text, media = await collect_media(m)
    label = text or (MEDIA_LABELS.get(media["mime_type"], "Вложение") if media else "")
    if not text and not media:  # стикеры, документы и прочее: не отвечаем
        return

    name = sender_label(sender)

    # P1: копия КАЖДОГО входящего приходит владельцу независимо от того,
    # ответит бот или нет (выключено, пауза, лимит, нет прав, нет ключа).
    if await get_setting("inbox", "1") == "1":
        await notify_owner(info, f"📨 {name}: «{label[:300]}»")

    history = await asyncio.to_thread(storage.get_history, chat_id, cfg.HISTORY_LIMIT)
    await asyncio.to_thread(storage.save_message, chat_id, "user", f"{name}: {label}")

    if await get_setting("enabled", "1") != "1":
        return
    if _paused_until.get(chat_id, 0) > time.monotonic():
        return
    if not info["can_reply"]:
        if conn_id not in _warned_no_rights:
            _warned_no_rights.add(conn_id)
            await notify_owner(
                info, "⚠️ Мне написали, но у бота нет права отвечать. Разрешите его в "
                "Настройки → Telegram для бизнеса → Чат-боты."
            )
        return
    if not rate_ok(chat_id):
        log.info("лимит ответов в час для чата %s", chat_id)
        return

    persona = await get_setting("persona", "")
    system = build_prompt(cfg.OWNER_NAME, persona, current_time_str(cfg.BOT_TIMEZONE))
    try:
        await bot.send_chat_action(chat_id, "typing", business_connection_id=conn_id)
    except Exception:
        pass

    try:
        reply = await cascade.generate(system, history, f"{name}: {label}", media)
    except Exception as e:
        log.error("каскад Gemini не ответил: %s", e)
        await notify_owner(
            info, f"⚠️ {name} написал(а): «{label[:200]}». Автоответ не получился, ответьте сами."
        )
        return

    reply = (reply or "").strip()
    if not reply:
        return
    await asyncio.sleep(min(4.0, 0.5 + 0.03 * len(reply)))
    try:
        for chunk in split_text(strip_markdown(reply)):
            if chunk.strip():
                await bot.send_message(chat_id, chunk, business_connection_id=conn_id)
    except Exception as e:
        log.error("не удалось отправить ответ: %s", e)
        await notify_owner(
            info, f"⚠️ Не получилось отправить автоответ. Сообщение от {name}: «{label[:200]}»."
        )
        return

    _reply_times[chat_id].append(time.monotonic())
    await asyncio.to_thread(storage.save_message, chat_id, "model", reply)
    if await get_setting("notify", "1") == "1":
        await notify_owner(info, f"🤖 Ответил за вас ({name}):\n«{label[:300]}»\n→ «{reply[:500]}»")


# ---------- Команды владельца ----------

BUSINESS_HELP = (
    "🤖 Автоответы от вашего лица (Telegram Business)\n\n"
    "/business on | off — включить или выключить автоответы\n"
    "/business inbox on | off — присылать копию каждого входящего\n"
    "/business notify on | off — присылать копию каждого ответа\n"
    "/business persona <текст> — как отвечать (тон, что можно и нельзя)\n"
    "/business persona clear — убрать указания\n"
    "/business status — текущие настройки\n\n"
    "Подключить: Telegram → Настройки → Telegram для бизнеса → Чат-боты (нужен Premium). "
    f"Если вы сами напишете в чат, бот замолчит там на {cfg.TAKEOVER_MINUTES} мин."
)


@dp.message(Command("start"))
async def start(m: Message):
    # Владелец НЕ назначается здесь: только OWNER_ID из окружения.
    if not m.from_user or m.from_user.id != cfg.OWNER_ID:
        return
    await m.answer(BUSINESS_HELP + f"\n\nВаш ID: {m.from_user.id}")


@dp.message(Command("business"))
async def cmd_business(m: Message, command: CommandObject):
    if not m.from_user or m.from_user.id != cfg.OWNER_ID:
        return
    args = (command.args or "").strip()
    head, _, rest = args.partition(" ")
    head, rest = head.lower(), rest.strip()
    connected = any(c["user_id"] == cfg.OWNER_ID and c["enabled"] for c in _connections.values())

    if head in ("on", "off"):
        await set_setting("enabled", "1" if head == "on" else "0")
        note = "" if connected else "\n\nБот пока не подключён к вашему аккаунту. Подключите его в настройках Telegram Business."
        await m.answer(("✅ Автоответы включены." if head == "on" else "⏸ Автоответы выключены.") + note)
    elif head in ("inbox", "notify") and rest.lower() in ("on", "off"):
        await set_setting(head, "1" if rest.lower() == "on" else "0")
        what = "входящих" if head == "inbox" else "ответов"
        await m.answer(
            f"🔔 Копии {what} будут приходить сюда." if rest.lower() == "on"
            else f"🔕 Копии {what} отключены."
        )
    elif head == "persona":
        if rest.lower() in ("clear", "очистить", "-"):
            await set_setting("persona", "")
            await m.answer("Указания убраны.")
        elif rest:
            await set_setting("persona", rest[:cfg.PERSONA_LIMIT])
            await m.answer("✅ Указания сохранены. Они учитываются в каждом автоответе.")
        else:
            current = await get_setting("persona", "")
            await m.answer(
                f"Сейчас: {current}" if current else
                "Указаний нет. Пример: /business persona Я занят до вечера, отвечай коротко и дружелюбно."
            )
    elif head == "status":
        await m.answer(
            f"Подключение: {'да' if connected else 'нет'}\n"
            f"Автоответы: {'включены' if await get_setting('enabled', '1') == '1' else 'выключены'}\n"
            f"Копии входящих: {'да' if await get_setting('inbox', '1') == '1' else 'нет'}\n"
            f"Копии ответов: {'да' if await get_setting('notify', '1') == '1' else 'нет'}\n"
            f"Каскад Gemini: {cascade.describe()}\n"
            f"Игнорируемые ID: {', '.join(map(str, sorted(cfg.IGNORE_IDS))) or 'нет'}\n"
            f"Указания: {await get_setting('persona', '') or 'нет'}"
        )
    else:
        await m.answer(BUSINESS_HELP)


# ---------- запуск ----------

async def on_startup(app: web.Application):
    if cfg.BASE_URL:
        await bot.set_webhook(
            cfg.BASE_URL + cfg.WEBHOOK_PATH,
            secret_token=cfg.WEBHOOK_SECRET,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=True,
        )
        log.info("вебхук установлен: %s%s", cfg.BASE_URL, cfg.WEBHOOK_PATH)
    else:
        log.warning(
            "WEBHOOK_BASE_URL / RENDER_EXTERNAL_URL не задан — вебхук НЕ установлен, "
            "бот не будет получать обновления. Задайте переменную в Render."
        )


async def health(_):
    return web.Response(text="ok")


def main():
    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dp, bot, secret_token=cfg.WEBHOOK_SECRET).register(app, path=cfg.WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=cfg.PORT)


if __name__ == "__main__":
    main()
