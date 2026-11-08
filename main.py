"""Секретарь на Telegram Business: отвечает в личных чатах владельца от его лица.

Логика перенесена из ForUs (tgbot/business.py) и дополнена:
  * каскад моделей Gemini (как в ForUs/tgbot/llm.py) вместо одиночного вызова;
  * голосовые, кружки и фото обрабатываются через inline_data Gemini;
  * владелец задаётся ТОЛЬКО через OWNER_ID — /start больше не захватывает бота;
  * system prompt не содержит имени собеседника (защита от инъекции через first_name);
  * в промпт возвращено текущее время (BOT_TIMEZONE).

Умные отложенные ответы (см. reply_policy.py):
  * бот НЕ отвечает на каждое сообщение. Пачка сообщений одного чата копится
    в БД, а каждое новое сообщение сдвигает дедлайн (debounce): 5 сообщений
    дают ровно ОДИН вызов Gemini и ОДИН ответ;
  * пока владелец недавно писал — ждём `reply_delay_minutes` минут тишины;
    если владельца не было дольше `away_minutes` — короткое окно сбора
    `away_collect_seconds`, чтобы пачка всё равно склеилась;
  * ответ владельца в чате отменяет пачку ТОЛЬКО в этом чате;
  * ночью (настраиваемые границы) и при долгом отсутствии в system prompt
    добавляется инструкция «скорее всего, уже спит, ответит утром»;
  * тумблер `smart_replies=0` возвращает мгновенные ответы (дедлайн = now,
    пачка подбирается тем же воркером без отдельной ветки кода).

Ответ не нужен и кнопки:
  * модель сама отвечает меткой [[NO_REPLY]] на «спасибо», «ок», эмодзи и т. п.
    (настройка skip_unneeded); метка собеседнику не уходит никогда;
  * под уведомлением 📨 три кнопки: «Не отвечать» (отменить пачку), «Ответить
    сейчас» и «Заглушить на 3 ч» (чат игнорируется, пока не истечёт срок);
  * любое исходящее сообщение владельца (голосовое, фото, стикер) отменяет пачку;
  * перед отправкой проверяется токен захвата: ответ на устаревшую пачку не уходит.

Mini App (/app): настройки читаются из БД на каждом сообщении и на доставке,
поэтому смена чисел, persona и списка моделей применяется без перезапуска.
Env-переменные остаются слоем дефолтов.

Имя переменной ANTHROPIC_API_KEY сохранено исторически: в ней лежит ключ Gemini,
Claude не подключён. См. config.py.

Устойчивость на Render Free: при заданном DATABASE_URL хранилище — внешний
Postgres. `due_at` хранится как wall-clock epoch (НЕ monotonic), поэтому
дедлайны переживают сон/рестарт и подбираются воркером сразу при подъёме.
drop_pending_updates=False, DedupGuard, last_update_id в БД и seen_ids в пачке
не дают потерять или продублировать сообщения. Внешний ping /health обязателен.
"""

import asyncio
import base64
import logging
import time
from collections import defaultdict, deque

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BusinessConnection,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import config as cfg
import reply_policy as policy
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

# ---------- хранилище (создаём ДО middleware: тому нужен last_update_id) ----------

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
# Анти-зацикливающий счётчик на час. Оставляем in-memory: его потеря при
# рестарте не теряет сообщения (в отличие от пачек, которые живут в БД).
_reply_times: dict[int, deque] = defaultdict(deque)
_warned_no_rights: set[str] = set()


def _to_float(raw):
    try:
        return None if raw is None else float(raw)
    except (TypeError, ValueError):
        return None


def _load_last_update_id() -> int:
    try:
        return int(storage.get_meta("last_update_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


# DedupGuard живет только в памяти и пустеет после рестарта. Поэтому последний
# обработанный update_id дублируем в БД: после холодного старта Telegram может
# повторно доставить апдейт, который мы уже приняли в пачку.
_LAST_UPDATE_ID = _load_last_update_id()

# status_provider() обязан быть синхронным (webapp._safe_status отклоняет
# awaitable). Воркер обновляет этот кэш на каждом тике; провайдер только читает.
_STATUS_CACHE: dict = {}
# Ближайший дедлайн: чтобы не дёргать БД на каждом входящем сообщении.
_NEXT_DUE_AT: float | None = None


class _DedupMiddleware(BaseMiddleware):
    def __init__(self, guard: DedupGuard) -> None:
        self.guard = guard

    async def __call__(self, handler, event, data):
        global _LAST_UPDATE_ID
        raw = getattr(event, "update_id", None)
        if raw is None:
            return await handler(event, data)
        uid = int(raw)
        if uid <= _LAST_UPDATE_ID or self.guard.is_duplicate(uid):
            log.info("дубль update_id=%s проигнорирован", uid)
            return
        result = await handler(event, data)
        # Помечаем обработанным ТОЛЬКО после успешного обработчика: если он
        # упал, апдейт не считается принятым.
        if uid > _LAST_UPDATE_ID:
            _LAST_UPDATE_ID = uid
            try:
                await asyncio.to_thread(storage.set_meta, "last_update_id", str(uid))
            except Exception as e:
                log.warning("не удалось сохранить last_update_id: %s", type(e).__name__)
        return result


_DEDUP = DedupGuard()
dp.update.outer_middleware.register(_DedupMiddleware(_DEDUP))


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


# ---------- настройки и статус ----------

async def rt() -> dict:
    """Живое состояние настроек: один запрос к БД вместо всех get_setting."""
    rows = await asyncio.to_thread(storage.get_all_settings)
    return load_state(rows, DEFAULTS)


async def get_setting(key: str, default: str = "") -> str:
    return await asyncio.to_thread(storage.get_setting, key, default)


async def set_setting(key: str, value: str) -> None:
    await asyncio.to_thread(storage.set_setting, key, value)


def status_provider() -> dict:
    """Синхронный снимок кэша воркера (см. _update_cache)."""
    return dict(_STATUS_CACHE)


async def _update_cache() -> None:
    global _NEXT_DUE_AT, _STATUS_CACHE
    now = time.time()
    try:
        stats = await asyncio.to_thread(storage.pending_stats)
        last_owner_raw = await asyncio.to_thread(storage.get_meta, "last_owner_at")
    except Exception as e:
        log.warning("статус: не удалось прочитать БД: %s", type(e).__name__)
        stats, last_owner_raw = {"pending_chats": 0, "next_due_at": None}, None
    state = await rt()
    last_owner = _to_float(last_owner_raw)
    away_minutes = None if last_owner is None else max(0, int((now - last_owner) / 60))
    next_due = stats.get("next_due_at")
    next_in = None if next_due is None else max(0, int(next_due - now))
    connected = any(c["user_id"] == cfg.OWNER_ID and c["enabled"] for c in _connections.values())
    can_reply = any(
        c["user_id"] == cfg.OWNER_ID and c["enabled"] and c["can_reply"]
        for c in _connections.values()
    )
    hour_ago = time.monotonic() - 3600
    replies = sum(sum(1 for t in q if t > hour_ago) for q in _reply_times.values())
    _NEXT_DUE_AT = next_due
    _STATUS_CACHE = {
        "connected": connected,
        "can_reply": can_reply,
        "pending_chats": int(stats.get("pending_chats") or 0),
        "owner_away_minutes": away_minutes,
        "next_reply_in": next_in,
        "night_now": policy.night_active(now, state),
        "replies_last_hour": replies,
        "max_replies_per_hour": state["max_replies_per_hour"],
        "persistent_storage": bool(cfg.DATABASE_URL),
    }


async def test_ai_provider() -> str:
    """Тестовый запрос к текущему каскаду из Mini App."""
    state = await rt()
    cascade = GeminiCascade(**cascade_kwargs(state, cfg.GEMINI_API_KEY))
    system = build_prompt(state["owner_name"], state["persona"], current_time_str(state["timezone"]))
    return await cascade.generate(system, [], "Ответь одним коротким предложением: ты на связи?")


# ---------- подключения Telegram Business ----------

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
        log.warning("не удалось получить подключение %s: %s", connection_id, type(e).__name__)
        return None
    info = connection_info(c)
    _connections[connection_id] = info
    await asyncio.to_thread(storage.save_connection, connection_id, info)
    return info


async def notify_owner(info: dict, text: str, markup=None):
    """Возвращает Message | None (нужен message_id для редактирования)."""
    try:
        if markup is None:
            return await bot.send_message(info["user_chat_id"], text)
        return await bot.send_message(info["user_chat_id"], text, reply_markup=markup)
    except Exception as e:
        log.warning("не удалось написать владельцу: %s", type(e).__name__)
        return None


async def _send_or_edit(
    info: dict, text: str, message_id: int | None, markup=None
) -> int | None:
    """Одно редактируемое уведомление на пачку вместо копии каждого сообщения."""
    chat = info["user_chat_id"]
    if message_id:
        # Правка без reply_markup убирает кнопки, поэтому передаём их каждый раз.
        kw = {"reply_markup": markup} if markup is not None else {}
        try:
            await bot.edit_message_text(text, chat_id=chat, message_id=message_id, **kw)
            return int(message_id)
        except Exception as e:
            if "not modified" in str(e).lower():
                return int(message_id)
            # MessageIdInvalid и прочее -> шлём новое сообщение.
            log.info("уведомление не отредактировано, шлём новое: %s", type(e).__name__)
    msg = await notify_owner(info, text, markup)
    return int(msg.message_id) if msg is not None else None


async def _mark_notification(row: dict, info: dict, text: str, markup=None) -> None:
    """Финальная пометка уведомления. Без ``markup`` кнопки под ним исчезают."""
    mid = row.get("notify_message_id")
    if not mid:
        return
    kw = {"reply_markup": markup} if markup is not None else {}
    try:
        await bot.edit_message_text(text, chat_id=info["user_chat_id"], message_id=mid, **kw)
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.info("не удалось пометить уведомление: %s", type(e).__name__)


def _batch_lines(parts: list) -> str:
    shown = parts[-4:]
    lines = "\n".join(f"«{str(p)[:160]}»" for p in shown)
    hidden = len(parts) - len(shown)
    if hidden > 0:
        lines += f"\n… и ещё {hidden}"
    return lines


def _batch_notification_text(row: dict) -> str:
    parts = row.get("parts") or []
    secs = max(0, int(float(row.get("due_at") or 0) - time.time()))
    return (
        f"📨 Сообщений в пачке: {len(parts)}\n{_batch_lines(parts)}\n\n"
        f"⏳ Ответ примерно через {secs} сек, если вы не ответите сами."
    )


def _with_batch(row: dict, status: str) -> str:
    """Итог по пачке, но с её текстом: владелец нажал кнопку, чтобы не отвечать,
    и должен по-прежнему видеть, что именно ему написали."""
    parts = row.get("parts") or []
    return f"📨 Сообщений: {len(parts)}\n{_batch_lines(parts)}\n\n{status}"


# ---------- кнопки под уведомлением ----------

def _inbox_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🙈 Не отвечать", callback_data=policy.make_callback("skip", chat_id)
            ),
            InlineKeyboardButton(
                text="⚡ Ответить сейчас", callback_data=policy.make_callback("now", chat_id)
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"🔕 Заглушить на {policy.MUTE_HOURS} ч",
                callback_data=policy.make_callback("mute", chat_id),
            ),
        ],
    ])


def _unmute_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔔 Включить снова", callback_data=policy.make_callback("unmute", chat_id)
        ),
    ]])


async def _upsert_inbox_notification(row: dict, info: dict, state: dict) -> None:
    if state["inbox"] != "1":
        return
    mid = await _send_or_edit(
        info,
        _batch_notification_text(row),
        row.get("notify_message_id"),
        _inbox_keyboard(int(row["chat_id"])),
    )
    if mid and mid != row.get("notify_message_id"):
        await asyncio.to_thread(storage.set_notify_message_id, int(row["chat_id"]), mid)


# ---------- медиа и лимиты ----------

MEDIA_LABELS = {
    "audio/ogg": "Голосовое сообщение",
    "video/mp4": "Видеосообщение",
    "image/jpeg": "Фотография",
}


def media_refs(m: Message) -> tuple[str | None, str | None]:
    """Только file_id + mime. base64 в БД НЕ храним — скачаем на доставке."""
    if m.voice:
        return m.voice.file_id, "audio/ogg"
    if m.video_note:
        return m.video_note.file_id, "video/mp4"
    if m.photo:
        return m.photo[-1].file_id, "image/jpeg"
    return None, None


async def _fetch_media(row: dict) -> dict | None:
    file_id, mime = row.get("file_id"), row.get("file_mime")
    if not file_id or not mime:
        return None
    try:
        f = await bot.get_file(file_id)
        raw = await bot.download_file(f.file_path)
        data = raw.getvalue() if hasattr(raw, "getvalue") else raw.read()
        return {"mime_type": mime, "data": base64.b64encode(data).decode()}
    except Exception as e:
        log.warning("не удалось скачать вложение пачки: %s", type(e).__name__)
        return None


def rate_ok(chat_id: int, limit: int) -> bool:
    """Лимит берётся из живых настроек, а не из замороженного maxlen при импорте."""
    now = time.monotonic()
    times = _reply_times[chat_id]
    while times and now - times[0] > 3600:
        times.popleft()
    while len(times) > limit + 5:
        times.popleft()
    return len(times) < limit


# ---------- доставка отложенных пачек ----------

async def _drop_batch(row: dict, info: dict | None, text: str = "") -> None:
    """Удаляет пачку ПОСЛЕ подтверждённой отправки; метит уведомление."""
    chat_id = int(row["chat_id"])
    removed = await asyncio.to_thread(storage.delete_pending, chat_id, row.get("claim_token"))
    if not removed:
        # Пока воркер отвечал, пришло новое сообщение и пересоздало пачку.
        log.info("пачка chat_id=%s обновлена новым сообщением — не удаляем", chat_id)
        return
    if text and info:
        await _mark_notification(row, info, text)


async def _deliver(row: dict) -> None:
    chat_id = int(row["chat_id"])
    now = time.time()
    # Гейты доставки пересчитываются из СВЕЖИХ настроек: за 10 минут ожидания
    # владелец мог всё поменять в Mini App.
    state = await rt()
    conn_id = row.get("conn_id") or ""
    info = await get_connection(conn_id) if conn_id else None

    if (
        state["enabled"] != "1"
        or not info
        or info["user_id"] != cfg.OWNER_ID
        or not info["enabled"]
        or not info["can_reply"]
    ):
        log.info("пачка chat_id=%s отменена: автоответы недоступны", chat_id)
        await _drop_batch(row, info, "⏭ Автоответ отменён (настройки изменились)")
        return
    if not rate_ok(chat_id, state["max_replies_per_hour"]):
        log.info("пачка chat_id=%s отменена: лимит ответов в час", chat_id)
        await _drop_batch(row, info, "⏭ Автоответ отменён (лимит ответов в час)")
        return

    parts = row.get("parts") or []
    batch_text = policy.build_batch_text(parts)
    if not batch_text:
        await _drop_batch(row, info, "⏭ Пачка пуста")
        return

    history = await asyncio.to_thread(storage.get_history, chat_id, state["history_limit"])
    # Ночь проверяем НА МОМЕНТ ОТПРАВКИ: 10 минут ожидания могут пересечь 01:00.
    night = policy.night_active(now, state)
    night_instruction = (
        policy.build_night_instruction(state["owner_name"], state["night_hint"])
        if night else ""
    )
    system = build_prompt(
        state["owner_name"],
        state["persona"],
        current_time_str(state["timezone"]),
        night_instruction,
        allow_skip=state["skip_unneeded"] == "1",
    )
    media = await _fetch_media(row)

    # GeminiCascade не хранит состояние — собираем из текущих настроек, поэтому
    # смена списка моделей в Mini App действует сразу.
    cascade = GeminiCascade(**cascade_kwargs(state, cfg.GEMINI_API_KEY))
    try:
        reply = await cascade.generate(system, history, batch_text, media)
    except Exception as e:
        log.error("каскад Gemini не ответил для chat_id=%s: %s", chat_id, type(e).__name__)
        # НЕ удаляем: attempts уже увеличен, stale-перехват повторит попытку.
        if int(row.get("attempts") or 0) >= 3:
            await notify_owner(info, "⚠️ Не удалось получить автоответ после 3 попыток. Ответьте сами.")
            await _drop_batch(row, info, "⚠️ Автоответ не получился — ответьте сами")
        return

    reply = (reply or "").strip()
    if not reply:
        if int(row.get("attempts") or 0) >= 3:
            await _drop_batch(row, info, "⚠️ Пустой ответ модели — ответьте сами")
        return

    # Пока модель думала (до минуты), пачку могли отменить кнопкой, ответом
    # владельца или перехватить повторно, либо пришло новое сообщение и пачка
    # пересоздалась. Токен захвата изменился, значит этот ответ устарел: ничего
    # не шлём. Новая пачка (старые + новые сообщения) уйдёт одним ответом позже,
    # поэтому на уже отвеченное повторно не отвечаем.
    current = await asyncio.to_thread(storage.get_pending, chat_id)
    if not current or current.get("claim_token") != row.get("claim_token"):
        log.info(
            "пачка chat_id=%s изменилась или отменена, пока готовился ответ: не отправляю",
            chat_id,
        )
        return

    if policy.is_no_reply(reply):
        # Модель решила промолчать. Метку НИКОГДА не отправляем собеседнику, даже
        # если настройка выключена, а модель всё равно её написала.
        log.info("chat_id=%s: ответ не нужен, молчим", chat_id)
        keep = max(40, state["history_limit"])
        await asyncio.to_thread(storage.save_message, chat_id, "user", batch_text, keep)
        await _drop_batch(
            row, info,
            _with_batch(row, "🤐 Ответ не нужен — промолчал. Если нужно, ответьте сами."),
        )
        return

    try:
        await bot.send_chat_action(chat_id, "typing", business_connection_id=conn_id)
    except Exception:
        pass
    await asyncio.sleep(min(4.0, 0.5 + 0.03 * len(reply)))
    try:
        for chunk in split_text(strip_markdown(reply)):
            if chunk.strip():
                await bot.send_message(chat_id, chunk, business_connection_id=conn_id)
    except Exception as e:
        log.error("не удалось отправить ответ chat_id=%s: %s", chat_id, type(e).__name__)
        # Не удаляем — повторим. Риск дубля при частичной отправке осознанно
        # меньше риска навсегда потерять сообщение (владелец уже жаловался).
        if int(row.get("attempts") or 0) >= 3:
            await notify_owner(info, "⚠️ Не получилось отправить автоответ. Ответьте сами.")
            await _drop_batch(row, info, "⚠️ Отправка не удалась — ответьте сами")
        return

    _reply_times[chat_id].append(time.monotonic())
    keep = max(40, state["history_limit"])
    await asyncio.to_thread(storage.save_message, chat_id, "user", batch_text, keep)
    await asyncio.to_thread(storage.save_message, chat_id, "model", reply, keep)
    await _drop_batch(row, info, f"✅ Ответил за вас:\n«{reply[:300]}»")
    if state["notify"] == "1":
        await notify_owner(
            info,
            f"🤖 Ответил за вас ({len(parts)} сообщ.):\n→ «{reply[:500]}»",
        )


async def process_due() -> None:
    """Забирает дозревшие пачки и доставляет их. Вызывается воркером и на приёме."""
    now = time.time()
    try:
        batches = await asyncio.to_thread(storage.claim_due, now, 20, 120)
    except Exception as e:
        log.warning("не удалось забрать пачки: %s", type(e).__name__)
        return
    for row in batches:
        try:
            await _deliver(row)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("ошибка доставки пачки chat_id=%s: %s", row.get("chat_id"), type(e).__name__)
    try:
        await _update_cache()
    except Exception as e:
        log.warning("не удалось обновить кэш статуса: %s", type(e).__name__)


async def _tick_loop() -> None:
    while True:
        await asyncio.sleep(2)
        try:
            await process_due()
            await asyncio.to_thread(storage.purge_stale, time.time())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("тик воркера пачек: %s", type(e).__name__)


async def pending_worker(_: web.Application):
    """cleanup_ctx: парный startup/shutdown с гарантированной отменой таски."""
    await _update_cache()
    # Дедлайны, прошедшие пока сервис спал на Render Free, подбираем сразу.
    await process_due()
    ticker = asyncio.create_task(_tick_loop())
    try:
        yield
    finally:
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


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
            f"Отвечаю не сразу: жду {state['reply_delay_minutes']} мин тишины, а если вас не будет "
            f"больше {state['away_minutes']} мин — собираю все сообщения в один ответ. "
            "Если напишете сами — автоответ отменю.",
        )


# ---------- Business: сообщения ----------

@dp.business_message()
async def on_business_message(m: Message):
    global _NEXT_DUE_AT
    # Догоняем дозревшие пачки до обработки нового сообщения. Кэш греется
    # воркером; пока он не прогрет — проверяем всё равно (один дешёвый запрос).
    if _NEXT_DUE_AT is None or time.time() >= _NEXT_DUE_AT:
        await process_due()

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

    # --- владелец написал сам -------------------------------------------------
    if sender is None or sender.id == cfg.OWNER_ID:
        # ЛЮБОЕ исходящее сообщение владельца (текст, голосовое, кружок, фото,
        # стикер) значит «я здесь и ответил сам». Раньше считался только текст, и
        # бот отвечал после того, как вы уже ответили голосовым.
        # Присутствие ГЛОБАЛЬНОЕ: время последнего ИСХОДЯЩЕГО сообщения
        # владельца в любом подключённом Business-чате. Telegram Business
        # не даёт реальный online-статус, это единственный доступный сигнал.
        own_text = (m.text or m.caption or "").strip()
        await asyncio.to_thread(storage.set_meta, "last_owner_at", repr(time.time()))
        cancelled = await asyncio.to_thread(storage.cancel_pending, chat_id)
        keep = max(40, state["history_limit"])
        if cancelled and cancelled.get("parts"):
            await asyncio.to_thread(
                storage.save_message, chat_id, "user",
                policy.build_batch_text(cancelled["parts"]), keep,
            )
        if own_text:
            await asyncio.to_thread(storage.save_message, chat_id, "model", own_text, keep)
        if cancelled:
            await _mark_notification(
                cancelled, info,
                _with_batch(cancelled, "✅ Вы ответили сами — автоответ отменён"),
            )
        return

    if sender.is_bot or sender.id in state["ignore_ids"]:
        return

    # Чат заглушён кнопкой «Заглушить»: бот молчит и уведомлений не шлёт.
    if policy.is_muted(
        await asyncio.to_thread(storage.get_meta, f"mute:{chat_id}"), time.time()
    ):
        return

    text = (m.text or m.caption or "").strip()
    file_id, mime = media_refs(m)
    label = text or (MEDIA_LABELS.get(mime, "Вложение") if file_id else "")
    if not text and not file_id:  # стикеры, документы и прочее: не отвечаем
        return
    name = sender_label(sender)

    if state["enabled"] != "1":
        return
    if not info["can_reply"]:
        if conn_id not in _warned_no_rights:
            _warned_no_rights.add(conn_id)
            await notify_owner(
                info, "⚠️ Мне написали, но у бота нет права отвечать. Разрешите его в "
                "Настройки → Telegram для бизнеса → Чат-боты."
            )
        return

    now = time.time()
    last_owner_at = _to_float(await asyncio.to_thread(storage.get_meta, "last_owner_at"))
    # smart_replies=0 -> (0.0, "away") -> пачка с due_at=now, process_due()
    # ниже доставит её немедленно. Отдельной ветки «старого кода» нет.
    delay, mode = policy.plan_deadline(now, last_owner_at, state)
    due_at = now + delay

    row = await asyncio.to_thread(
        storage.upsert_pending,
        chat_id=chat_id,
        part=f"{name}: {label}",
        message_id=int(m.message_id) if m.message_id is not None else None,
        due_at=due_at,
        now=now,
        mode=mode,
        conn_id=conn_id,
        owner_chat_id=int(info["user_chat_id"]),
        file_id=file_id,
        file_mime=mime,
    )
    if row.get("duplicate"):
        # Повторная доставка вебхука после рестарта: пачку не трогаем и
        # дедлайн не сдвигаем, иначе собеседник мог бы бесконечно его откладывать.
        return
    if _NEXT_DUE_AT is None or due_at < _NEXT_DUE_AT:
        _NEXT_DUE_AT = due_at

    await _upsert_inbox_notification(row, info, state)
    if delay <= 0:
        await process_due()


# ---------- кнопки: «Не отвечать» / «Ответить сейчас» / «Заглушить» ----------

async def _cb_answer(q: CallbackQuery, text: str = "", alert: bool = False) -> None:
    try:
        await q.answer(text, show_alert=alert)
    except Exception as e:
        log.info("не удалось ответить на нажатие: %s", type(e).__name__)


async def _cb_edit(q: CallbackQuery, text: str, markup=None) -> None:
    """Правит уведомление, под которым нажали кнопку. Без ``markup`` кнопки уходят."""
    msg = getattr(q, "message", None)
    mid = getattr(msg, "message_id", None)
    if not mid:
        return
    chat = getattr(getattr(msg, "chat", None), "id", None) or q.from_user.id
    kw = {"reply_markup": markup} if markup is not None else {}
    try:
        await bot.edit_message_text(text, chat_id=chat, message_id=mid, **kw)
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.info("не удалось обновить уведомление: %s", type(e).__name__)


async def _remember_batch(chat_id: int, cancelled: dict | None, keep: int) -> None:
    """Отменённую пачку кладём в историю: модель помнит, что собеседник писал."""
    if cancelled and cancelled.get("parts"):
        await asyncio.to_thread(
            storage.save_message, chat_id, "user",
            policy.build_batch_text(cancelled["parts"]), keep,
        )


@dp.callback_query(F.data.startswith(policy.CALLBACK_PREFIX + ":"))
async def on_button(q: CallbackQuery):
    # Кнопки видит только владелец, но callback_data можно подделать запросом
    # к Bot API от чужого аккаунта, поэтому проверяем отправителя явно.
    if not q.from_user or q.from_user.id != cfg.OWNER_ID:
        await _cb_answer(q, "Только для владельца", True)
        return
    parsed = policy.parse_callback(q.data)
    if parsed is None:
        await _cb_answer(q)
        return
    action, chat_id = parsed
    state = await rt()
    keep = max(40, state["history_limit"])

    if action == "skip":
        cancelled = await asyncio.to_thread(storage.cancel_pending, chat_id)
        if not cancelled:
            await _cb_answer(q, "Уже обработано")
            return
        await _remember_batch(chat_id, cancelled, keep)
        await _cb_answer(q, "Не отвечаю")
        await _cb_edit(q, _with_batch(cancelled, "🙈 Отвечать не буду"))

    elif action == "now":
        due = await asyncio.to_thread(storage.set_due_now, chat_id, time.time())
        # Отвечаем на нажатие СРАЗУ: Telegram держит спиннер на кнопке, а
        # генерация ответа может занять десятки секунд.
        await _cb_answer(q, "Отвечаю сейчас…" if due else "Уже обработано")
        if due:
            await process_due()

    elif action == "mute":
        until = policy.mute_until(time.time())
        await asyncio.to_thread(storage.set_meta, f"mute:{chat_id}", repr(until))
        cancelled = await asyncio.to_thread(storage.cancel_pending, chat_id)
        await _remember_batch(chat_id, cancelled, keep)
        until_txt = policy.local_time(until, state["timezone"]).strftime("%H:%M")
        status = f"🔕 Чат заглушён до {until_txt}: бот в нём молчит."
        if cancelled:
            text = _with_batch(cancelled, status)
        else:
            prev = getattr(getattr(q, "message", None), "text", None)
            text = f"{prev}\n\n{status}" if prev else status
        await _cb_answer(q, f"Заглушён до {until_txt}")
        await _cb_edit(q, text, _unmute_keyboard(chat_id))

    elif action == "unmute":
        await asyncio.to_thread(storage.set_meta, f"mute:{chat_id}", "0")
        await _cb_answer(q, "Бот снова отвечает")
        await _cb_edit(q, "🔔 Заглушение снято: бот снова отвечает в этом чате.")


# ---------- Команды владельца ----------

async def business_help() -> str:
    state = await rt()
    text = (
        "🤖 Автоответы от вашего лица (Telegram Business)\n\n"
        "/business on | off — включить или выключить автоответы\n"
        "/business inbox on | off — присылать копию входящих\n"
        "/business notify on | off — присылать копию каждого ответа\n"
        "/business skip on | off — молчать, если ответ не нужен («спасибо», «ок», эмодзи)\n"
        "/business persona <текст> — как отвечать (тон, что можно и нельзя)\n"
        "/business persona clear — убрать указания\n"
        "/business status — текущие настройки и очередь\n\n"
        "Подключить: Telegram → Настройки → Telegram для бизнеса → Чат-боты (нужен Premium).\n"
        f"Ответ приходит не сразу: жду {state['reply_delay_minutes']} мин тишины, "
        f"а при отсутствии больше {state['away_minutes']} мин собираю пачку за "
        f"{state['away_collect_seconds']} сек и отвечаю один раз. "
        "Если вы ответите сами (текстом, голосовым или фото) — автоответ отменяется. "
        "Под уведомлением 📨 есть кнопки: «Не отвечать», «Ответить сейчас», «Заглушить чат»."
    )
    if cfg.APP_URL:
        text += "\n\n⚙️ Все настройки удобнее менять в Mini App — кнопка «Настройки» в меню бота."
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
    elif head == "skip" and rest.lower() in ("on", "off"):
        await set_setting("skip_unneeded", "1" if rest.lower() == "on" else "0")
        await m.answer(
            "🤐 Буду молчать, если ответ не нужен." if rest.lower() == "on"
            else "💬 Буду отвечать на всё."
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
        try:
            stats = await asyncio.to_thread(storage.pending_stats)
        except Exception:
            stats = {"pending_chats": 0, "next_due_at": None}
        last_owner = _to_float(await asyncio.to_thread(storage.get_meta, "last_owner_at"))
        away = "неизвестно" if last_owner is None else f"{max(0, int((time.time() - last_owner) / 60))} мин"
        await m.answer(
            f"Подключение: {'да' if connected else 'нет'}\n"
            f"Автоответы: {'включены' if state['enabled'] == '1' else 'выключены'}\n"
            f"Умные ответы: {'вкл' if state['smart_replies'] == '1' else 'выкл (сразу)'}\n"
            f"Молчать, если ответ не нужен: {'да' if state['skip_unneeded'] == '1' else 'нет'}\n"
            f"Копии входящих: {'да' if state['inbox'] == '1' else 'нет'}\n"
            f"Копии ответов: {'да' if state['notify'] == '1' else 'нет'}\n"
            f"Каскад Gemini: {', '.join(state['models'])}\n"
            f"Ключ Gemini: {'задан' if cfg.GEMINI_API_KEY else 'НЕ задан'}\n"
            f"Жду тишины: {state['reply_delay_minutes']} мин · "
            f"Отсутствие: {state['away_minutes']} мин · Сбор пачки: {state['away_collect_seconds']} сек\n"
            f"Ночь: {'вкл' if state['night_enabled'] == '1' else 'выкл'} "
            f"({state['night_start']}–{state['night_end']}, {state['timezone']})\n"
            f"Лимит: {state['max_replies_per_hour']}/час · История: {state['history_limit']}\n"
            f"В очереди пачек: {stats.get('pending_chats', 0)} · Вас нет: {away}\n"
            f"Игнорируемые ID: {', '.join(map(str, state['ignore_ids'])) or 'нет'}\n"
            f"Указания: {state['persona'] or 'нет'}\n"
            f"Mini App: {cfg.APP_URL or 'не настроен'}"
        )
    else:
        await m.answer(await business_help())


# ---------- запуск ----------

async def on_startup(app: web.Application):
    # Дедлайны, прошедшие во время сна Render Free, подбираем ДО установки
    # вебхука: иначе новые апдейты Telegram могут прийти раньше, чем мы
    # разберём старую очередь. Вызов идемпотентен (claim_due атомарен).
    await process_due()
    await _update_cache()

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
    # обработчик уходит в фон. Иначе Telegram мог бы счесть запрос неуспешным
    # и прислать тот же update повторно.
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
    app.cleanup_ctx.append(pending_worker)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=cfg.PORT)


if __name__ == "__main__":
    main()
