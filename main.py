"""Секретарь на Telegram Business: отвечает в личных чатах владельца от его лица.

Логика перенесена из ForUs (tgbot/business.py) и дополнена:
  * каскад моделей Gemini (как в ForUs/tgbot/llm.py) вместо одиночного вызова;
  * голосовые, кружки и фото снова обрабатываются (через inline_data Gemini);
  * владелец задаётся ТОЛЬКО через OWNER_ID — /start больше не захватывает бота;
  * владельцу приходит копия КАЖДОГО входящего, а не только копия автоответа;
  * system prompt не содержит имени собеседника (защита от инъекции через first_name);
  * в промпт возвращено текущее время (BOT_TIMEZONE).

Mini App (/app): все настройки читаются из SQLite на КАЖДОЕ сообщение, поэтому
смена «пауза 60 → 10 минут», лимитов, persona и списка моделей применяется без
перезапуска. Env-переменные остаются слоем дефолтов.

Имя переменной ANTHROPIC_API_KEY сохранено исторически: в ней лежит ключ Gemini,
Claude не подключён. См. config.py.

Устойчивость на Render Free: при заданном DATABASE_URL хранилище — внешний
Postgres (настройки переживают рестарт), иначе локальный SQLite (сбросятся).
drop_pending_updates=False, дедуп update_id и внешний ping /health не дают
потерять сообщения на холодном старте. Подробности — в README.
"""

import asyncio
import base64
import logging
import time
from collections import defaultdict, deque

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BusinessConnection,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import config as cfg
from app_settings import build_defaults, cascade_kwargs, load_state
from dedup import DedupGuard
from helpers import (
    build_prompt,
    current_time_str,
    sender_label,
    split_text,
    strip_markdown,
)
from llm import GeminiCascade
from storage import Storage
from webapp import register_webapp_routes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("secretary")

bot = Bot(cfg.TOKEN)
dp = Dispatcher()

# Повторная доставка webhook-обновления не должна давать второй автоответ.
# Регистрируем ДО обработчиков: outer-middleware обёртывает все типы updates.
class _DedupMiddleware(BaseMiddleware):
    def __init__(self, guard: DedupGuard) -> None:
        self.guard = guard

    async def __call__(self, handler, event, data):
        uid = getattr(event, "update_id", None)
        if uid is not None and self.guard.is_duplicate(uid):
            log.info("дубль update_id=%s проигнорирован", uid)
            return
        return await handler(event, data)


_DEDUP = DedupGuard()
dp.update.outer_middleware.register(_DedupMiddleware(_DEDUP))

# Эфемерный диск Render Free теряет secretary.db при рестарте/сне. Внешняя БД
# живёт отдельно от сервиса, поэтому при заданном DATABASE_URL используем её.
if cfg.DATABASE_URL:
    try:
        from pg_storage import PgStorage  # noqa: PLC0415 (драйвер нужен только здесь)
        storage = PgStorage(cfg.DATABASE_URL)
        STORAGE_BACKEND = "Postgres (устойчиво)"
        log.info("хранилище: внешний Postgres — настройки переживут рестарт")
    except Exception as e:
        # Не пишем в лог DSN и текст ошибки: там может быть хост и пароль.
        log.error("не удалось подключиться к Postgres: %s", type(e).__name__)
        raise SystemExit(
            "DATABASE_URL задан, но подключиться не удалось. "
            "Проверьте DSN, sslmode и доступность БД."
        ) from None
else:
    storage = Storage(cfg.DB_PATH)
    STORAGE_BACKEND = "SQLite (временный — сбросится при рестарте)"
    log.warning(
        "хранилище: локальный SQLite. На Render Free настройки СБРОСЯТСЯ при "
        "рестарте/сне — задайте DATABASE_URL."
    )

# Дефолты из env. Значения из БД перекрывают их на каждом запросе.
DEFAULTS = build_defaults(cfg)

_connections: dict[str, dict] = storage.load_connections()
# Храним НАЧАЛО паузы, а не дедлайн: тогда смена 60 → 10 минут применяется
# к уже активной паузе, а не только к следующим.
_paused_since: dict[int, float] = {}
# Без фиксированного maxlen: лимит теперь настраивается на ходу.
_reply_times: dict[int, deque] = defaultdict(deque)
_warned_no_rights: set[str] = set()


def _mask_secret(value: str) -> str:
    if not value:
        return "(не задан)"
    if ":" in value:
        head, tail = value.split(":", 1)
        return f"{head}:{tail[:2]}••••••••••••"
    return value[:4] + "••••••••"


# В /api/state уходят ТОЛЬКО замаскированные значения; webapp дополнительно
# фильтрует по белому списку. Полный ключ и токен не сериализуются вообще.
READONLY_SNAPSHOT = {
    "BOT_TOKEN": _mask_secret(cfg.TOKEN),
    "OWNER_ID": cfg.OWNER_ID,
    "WEBHOOK_SECRET": _mask_secret(cfg.WEBHOOK_SECRET),
    "PORT": cfg.PORT,
    "DB_PATH": cfg.DB_PATH,
    "BASE_URL": cfg.BASE_URL or "(не задан)",
    "APP_URL": cfg.APP_URL or "(не задан)",
    "STORAGE_BACKEND": STORAGE_BACKEND,
    "api_key_set": bool(cfg.GEMINI_API_KEY),
}


# ---------- хранилище (синхронный SQLite уводим из event loop) ----------

async def rt() -> dict:
    """Живое состояние настроек: один запрос к БД вместо 12 get_setting."""
    rows = await asyncio.to_thread(storage.get_all_settings)
    return load_state(rows, DEFAULTS)


async def get_setting(key: str, default: str = "") -> str:
    return await asyncio.to_thread(storage.get_setting, key, default)


async def set_setting(key: str, value: str) -> None:
    await asyncio.to_thread(storage.set_setting, key, value)


def status_provider() -> dict:
    """Живой статус для /api/state (синхронный: только память + быстрый SQLite)."""
    state = load_state(storage.get_all_settings(), DEFAULTS)
    connected = any(c["user_id"] == cfg.OWNER_ID and c["enabled"] for c in _connections.values())
    can_reply = any(
        c["user_id"] == cfg.OWNER_ID and c["enabled"] and c["can_reply"]
        for c in _connections.values()
    )
    now = time.monotonic()
    paused = sum(
        1 for since in _paused_since.values()
        if state["takeover_minutes"] > 0 and now - since < state["takeover_minutes"] * 60
    )
    hour_ago = now - 3600
    replies = sum(sum(1 for t in q if t > hour_ago) for q in _reply_times.values())
    return {
        "connected": connected,
        "can_reply": can_reply,
        "paused_chats": paused,
        "replies_last_hour": replies,
        "takeover_minutes": state["takeover_minutes"],
        "max_replies_per_hour": state["max_replies_per_hour"],
        "persistent_storage": bool(cfg.DATABASE_URL),
    }


async def test_ai_provider() -> str:
    """Тестовый запрос к текущему каскаду из Mini App."""
    state = await rt()
    cascade = GeminiCascade(**cascade_kwargs(state, cfg.GEMINI_API_KEY))
    system = build_prompt(state["owner_name"], state["persona"], current_time_str(state["timezone"]))
    return await cascade.generate(system, [], "Ответь одним коротким предложением: ты на связи?")


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


def rate_ok(chat_id: int, limit: int) -> bool:
    """Лимит берётся из живых настроек, а не из замороженного maxlen при импорте."""
    now = time.monotonic()
    times = _reply_times[chat_id]
    while times and now - times[0] > 3600:
        times.popleft()
    while len(times) > limit + 5:
        times.popleft()
    return len(times) < limit


def is_paused(chat_id: int, minutes: int) -> bool:
    if minutes <= 0:
        return False
    since = _paused_since.get(chat_id)
    return since is not None and time.monotonic() - since < minutes * 60


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
    state = await rt()
    if not info["enabled"]:
        await notify_owner(info, "🔌 Бот отключён от вашего аккаунта Telegram Business. Автоответы остановлены.")
    elif not info["can_reply"]:
        await notify_owner(
            info,
            "🔌 Бот подключён, но у него нет права отвечать. Включите в Настройки → "
            "Telegram для бизнеса → Чат-боты → «Отвечать на сообщения».",
        )
    else:
        await notify_owner(
            info,
            "🔌 Бот подключён к вашему аккаунту и будет отвечать в личных чатах от вашего лица "
            f"(сейчас автоответы {'включены' if state['enabled'] == '1' else 'выключены'}).\n\n"
            f"Управление: /business или кнопка «Настройки». Если вы сами напишете в чат, "
            f"бот замолчит там на {state['takeover_minutes']} мин.",
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
    state = await rt()

    # Владелец сам пишет в чате: запоминаем и замолкаем. Пауза только на
    # осмысленное сообщение — стикер больше не выключает бота на час.
    if sender is None or sender.id == cfg.OWNER_ID:
        own_text = (m.text or m.caption or "").strip()
        if own_text:
            await asyncio.to_thread(
                storage.save_message, chat_id, "model", own_text,
                max(40, state["history_limit"]),
            )
            _paused_since[chat_id] = time.monotonic()
        return

    if sender.is_bot or sender.id in state["ignore_ids"]:
        return

    text, media = await collect_media(m)
    label = text or (MEDIA_LABELS.get(media["mime_type"], "Вложение") if media else "")
    if not text and not media:  # стикеры, документы и прочее: не отвечаем
        return

    name = sender_label(sender)

    # P1: копия КАЖДОГО входящего приходит владельцу независимо от того,
    # ответит бот или нет (выключено, пауза, лимит, нет прав, нет ключа).
    if state["inbox"] == "1":
        await notify_owner(info, f"📨 {name}: «{label[:300]}»")

    history = await asyncio.to_thread(storage.get_history, chat_id, state["history_limit"])
    await asyncio.to_thread(
        storage.save_message, chat_id, "user", f"{name}: {label}",
        max(40, state["history_limit"]),
    )

    if state["enabled"] != "1":
        return
    if is_paused(chat_id, state["takeover_minutes"]):
        return
    if not info["can_reply"]:
        if conn_id not in _warned_no_rights:
            _warned_no_rights.add(conn_id)
            await notify_owner(
                info, "⚠️ Мне написали, но у бота нет права отвечать. Разрешите его в "
                "Настройки → Telegram для бизнеса → Чат-боты."
            )
        return
    if not rate_ok(chat_id, state["max_replies_per_hour"]):
        log.info("лимит ответов в час для чата %s", chat_id)
        return

    system = build_prompt(state["owner_name"], state["persona"], current_time_str(state["timezone"]))
    try:
        await bot.send_chat_action(chat_id, "typing", business_connection_id=conn_id)
    except Exception:
        pass

    # GeminiCascade не хранит состояние — собираем его из текущих настроек,
    # поэтому смена списка моделей в Mini App действует сразу.
    cascade = GeminiCascade(**cascade_kwargs(state, cfg.GEMINI_API_KEY))
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
    await asyncio.to_thread(
        storage.save_message, chat_id, "model", reply, max(40, state["history_limit"])
    )
    if state["notify"] == "1":
        await notify_owner(info, f"🤖 Ответил за вас ({name}):\n«{label[:300]}»\n→ «{reply[:500]}»")


# ---------- Команды владельца ----------

async def business_help() -> str:
    state = await rt()
    text = (
        "🤖 Автоответы от вашего лица (Telegram Business)\n\n"
        "/business on | off — включить или выключить автоответы\n"
        "/business inbox on | off — присылать копию каждого входящего\n"
        "/business notify on | off — присылать копию каждого ответа\n"
        "/business persona <текст> — как отвечать (тон, что можно и нельзя)\n"
        "/business persona clear — убрать указания\n"
        "/business status — текущие настройки\n\n"
        "Подключить: Telegram → Настройки → Telegram для бизнеса → Чат-боты (нужен Premium). "
        f"Если вы сами напишете в чат, бот замолчит там на {state['takeover_minutes']} мин."
    )
    if cfg.APP_URL:
        text += "\n\n⚙️ Все настройки (пауза, лимиты, persona, модели) удобнее менять в Mini App — кнопка «Настройки» в меню бота."
    return text


@dp.message(Command("start"))
async def start(m: Message):
    # Владелец НЕ назначается здесь: только OWNER_ID из окружения.
    if not m.from_user or m.from_user.id != cfg.OWNER_ID:
        return
    keyboard = None
    if cfg.APP_URL and m.chat.type == "private" and not getattr(m, "business_connection_id", None):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="⚙️ Настройки", web_app=WebAppInfo(url=cfg.APP_URL))
            ]]
        )
    await m.answer(await business_help() + f"\n\nВаш ID: {m.from_user.id}", reply_markup=keyboard)


@dp.message(Command("business"))
async def cmd_business(m: Message, command: CommandObject):
    if not m.from_user or m.from_user.id != cfg.OWNER_ID:
        return
    args = (command.args or "").strip()
    head, _, rest = args.partition(" ")
    head, rest = head.lower(), rest.strip()
    state = await rt()
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
            await set_setting("persona", rest[: state["persona_limit"]])
            await m.answer("✅ Указания сохранены. Они учитываются в каждом автоответе.")
        else:
            current = state["persona"]
            await m.answer(
                f"Сейчас: {current}" if current else
                "Указаний нет. Пример: /business persona Я занят до вечера, отвечай коротко и дружелюбно."
            )
    elif head == "status":
        await m.answer(
            f"Подключение: {'да' if connected else 'нет'}\n"
            f"Автоответы: {'включены' if state['enabled'] == '1' else 'выключены'}\n"
            f"Копии входящих: {'да' if state['inbox'] == '1' else 'нет'}\n"
            f"Копии ответов: {'да' if state['notify'] == '1' else 'нет'}\n"
            f"Каскад Gemini: {', '.join(state['models'])}\n"
            f"Ключ Gemini: {'задан' if cfg.GEMINI_API_KEY else 'НЕ задан'}\n"
            f"Пауза: {state['takeover_minutes']} мин · Лимит: {state['max_replies_per_hour']}/час\n"
            f"История: {state['history_limit']} · Часовой пояс: {state['timezone']}\n"
            f"Игнорируемые ID: {', '.join(map(str, state['ignore_ids'])) or 'нет'}\n"
            f"Указания: {state['persona'] or 'нет'}\n"
            f"Mini App: {cfg.APP_URL or 'не настроен'}"
        )
    else:
        await m.answer(await business_help())


# ---------- запуск ----------

async def on_startup(app: web.Application):
    if cfg.BASE_URL:
        # False — принципиально. Render Free поднимается ≈1 минуту и может
        # рестартовать в любой момент; с True мы бы сами вычищали очередь
        # Telegram, которая ещё должна доехать после холодного старта.
        await bot.set_webhook(
            cfg.BASE_URL + cfg.WEBHOOK_PATH,
            secret_token=cfg.WEBHOOK_SECRET,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        log.info("вебхук установлен: %s%s", cfg.BASE_URL, cfg.WEBHOOK_PATH)
    else:
        log.warning(
            "WEBHOOK_BASE_URL / RENDER_EXTERNAL_URL не задан — вебхук НЕ установлен, "
            "бот не будет получать обновления. Задайте переменную в Render."
        )

    # Кнопка меню — отдельный try: её отсутствие не должно ронять запуск бота.
    if cfg.APP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Настройки", web_app=WebAppInfo(url=cfg.APP_URL)
                )
            )
            log.info("кнопка меню Mini App установлена: %s", cfg.APP_URL)
        except Exception as e:
            log.warning("не удалось установить кнопку меню Mini App: %s", type(e).__name__)
    else:
        log.warning("APP_URL пуст — кнопка меню Mini App пропущена, /app доступен по домену")


async def health(_):
    return web.Response(text="ok")


def main():
    app = web.Application()
    app.router.add_get("/", health)
    # Явно фиксируем поведение aiogram: вебхук сразу отвечает Telegram 200, а
    # обработчик (включая вызов Gemini) уходит в фон. Иначе Telegram мог бы
    # счесть запрос неуспешным и прислать тот же update повторно.
    SimpleRequestHandler(
        dp, bot,
        secret_token=cfg.WEBHOOK_SECRET,
        handle_in_background=True,
    ).register(app, path=cfg.WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    register_webapp_routes(
        app,
        bot_token=cfg.TOKEN,
        owner_id=cfg.OWNER_ID,
        storage=storage,
        defaults=DEFAULTS,
        status_provider=status_provider,
        readonly=READONLY_SNAPSHOT,
        test_ai_provider=test_ai_provider,
    )
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=cfg.PORT)


if __name__ == "__main__":
    main()
