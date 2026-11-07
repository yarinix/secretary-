#!/usr/bin/env python3
"""Watcher «владелец прочитал чат» для secretary-.

ПОЧЕМУ ВООБЩЕ НУЖЕН ОТДЕЛЬНЫЙ ПРОЦЕСС
--------------------------------------
Telegram Bot API НЕ присылает боту событие «владелец открыл чат / пометил
входящие прочитанными». В типе Update есть только business_connection,
business_message, edited_business_message и deleted_business_messages.
Метод readBusinessMessage — обратного направления: это БОТ помечает
сообщение прочитанным от имени бизнес-аккаунта.

Единственный автоматический способ узнать о прочтении — MTProto-сессия
ЛИЧНОГО аккаунта владельца: Telegram шлёт ей `UpdateReadHistoryInbox`.
Этот скрипт слушает только его и передаёт факт на бот через защищённый
POST /internal/seen. Содержимое сообщений НЕ читается и НЕ передаётся.

    [личный аккаунт] --MTProto--> [seen_watcher.py] --HTTPS--> [бот]
                                       UpdateReadHistoryInbox

ЗАПУСК
------
    pip install -r requirements-seen.txt
    python seen_watcher.py                 # обычный режим
    python seen_watcher.py --export-session   # один раз локально: получить строку сессии

Нужны переменные окружения (см. .env.example и README):
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION,
    SECRETARY_SEEN_URL, SECRETARY_SEEN_SECRET
Опционально: TELEGRAM_PHONE (первый вход), SEEN_CHAT_IDS (белый список чатов).

ВАЖНО: session string даёт ПОЛНЫЙ доступ к аккаунту. Не коммитьте его,
не логгируйте, не запускайте две копии с одной строкой (AUTH_KEY_DUPLICATED).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

log = logging.getLogger("secretary.seen_watcher")

MIN_SECRET_LEN = 16
MAX_ID = 2 ** 63 - 1


# ---------------------------------------------------------------------------
# Чистые функции: работают без telethon, покрыты юнит-тестами.
# ---------------------------------------------------------------------------

def load_config(env: dict | None = None) -> dict:
    """Читает и валидирует окружение. Бросает SystemExit с понятной подсказкой."""
    e = os.environ if env is None else env

    def need(key: str) -> str:
        value = str(e.get(key, "")).strip()
        if not value:
            raise SystemExit(
                f"{key} не задан. См. раздел «Автоматическое прочтение» в README."
            )
        return value

    try:
        api_id = int(need("TELEGRAM_API_ID"))
    except ValueError:
        raise SystemExit("TELEGRAM_API_ID должен быть целым числом") from None
    api_hash = need("TELEGRAM_API_HASH")
    url = need("SECRETARY_SEEN_URL").rstrip("/")
    secret = need("SECRETARY_SEEN_SECRET")
    if len(secret) < MIN_SECRET_LEN:
        raise SystemExit(
            f"SECRETARY_SEEN_SECRET короче {MIN_SECRET_LEN} символов — "
            "сервер такой секрет отвергнет"
        )
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
        raise SystemExit("SECRETARY_SEEN_URL должен быть HTTPS (иначе секрет утечёт)")

    session = str(e.get("TELEGRAM_SESSION", "")).strip()
    phone = str(e.get("TELEGRAM_PHONE", "")).strip()
    if not session and not phone:
        raise SystemExit(
            "Нужны TELEGRAM_SESSION или TELEGRAM_PHONE. "
            "Сначала выполните: python seen_watcher.py --export-session"
        )

    allowed = None
    raw_allowed = str(e.get("SEEN_CHAT_IDS", "")).strip()
    if raw_allowed:
        ids = []
        for chunk in raw_allowed.split(","):
            chunk = chunk.strip()
            if chunk.isdigit() and 0 < int(chunk) <= MAX_ID:
                ids.append(int(chunk))
        allowed = set(ids) or None

    return {
        "api_id": api_id,
        "api_hash": api_hash,
        "session": session,
        "phone": phone,
        "url": url,
        "secret": secret,
        "allowed": allowed,
    }


def extract_read(update: Any) -> dict | None:
    """`UpdateReadHistoryInbox` приватного чата -> {chat_id, max_id}.

    Игнорируем:
      * `UpdateReadHistoryOutbox` — это СОБЕСЕДНИК прочитал наши сообщения,
        а не владелец прочитал входящие;
      * `UpdateReadMessagesContents` — у него нет peer, чат не определить;
      * PeerChat / PeerChannel — группы и каналы, в них нет пачек бота;
      * чтение собственного исходящего (out=True).
    """
    if type(update).__name__ != "UpdateReadHistoryInbox":
        return None
    if getattr(update, "out", False):
        return None
    peer = getattr(update, "peer", None)
    user_id = getattr(peer, "user_id", None)
    max_id = getattr(update, "max_id", None)
    if user_id is None or max_id is None:
        return None
    if isinstance(user_id, bool) or isinstance(max_id, bool):
        return None
    try:
        chat_id, read_id = int(user_id), int(max_id)
    except (TypeError, ValueError):
        return None
    if not (0 < chat_id <= MAX_ID) or not (0 <= read_id <= MAX_ID):
        return None
    return {"chat_id": chat_id, "max_id": read_id}


def is_allowed(chat_id: int, allowed: set[int] | None) -> bool:
    return allowed is None or chat_id in allowed


def build_payload(info: dict, now: float | None = None) -> dict:
    """Уходим от зависимости от совпадения шкал message_id: шлём и id, и время."""
    return {
        "chat_id": int(info["chat_id"]),
        "max_read_id": int(info["max_id"]),
        "read_at": float(time.time() if now is None else now),
    }


# ---------------------------------------------------------------------------
# Сеть
# ---------------------------------------------------------------------------

async def post_seen(session, cfg: dict, payload: dict, retries: int = 3) -> bool:
    """POST с ретраями. Секрет и тело в логи NEVER."""
    import aiohttp

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + cfg["secret"],
        "Content-Type": "application/json",
    }
    for attempt in range(retries):
        try:
            async with session.post(
                cfg["url"],
                data=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if 200 <= resp.status < 300:
                    return True
                if 400 <= resp.status < 500 and resp.status != 429:
                    log.warning("приёмник отклонил сигнал: HTTP %s", resp.status)
                    return False
                log.warning("приёмник временно недоступен: HTTP %s", resp.status)
        except Exception as e:
            # Ni URL, ni секрет, ni traceback с заголовками в логи не попадают.
            log.warning("сеть: %s", type(e).__name__)
        if attempt + 1 < retries:
            await asyncio.sleep(2 ** attempt)
    return False


async def handle_update(update: Any, session, cfg: dict) -> bool:
    info = extract_read(update)
    if info is None:
        return False
    if not is_allowed(info["chat_id"], cfg["allowed"]):
        return False
    ok = await post_seen(session, cfg, build_payload(info))
    if not ok:
        log.warning("сигнал прочтения не доставлен (чат %s)", info["chat_id"])
    return ok


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def _run(cfg: dict, export_session: bool = False) -> None:
    try:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
        from telethon.tl import types
        import aiohttp
    except ImportError:
        raise SystemExit(
            "Telethon не установлен: pip install -r requirements-seen.txt"
        ) from None

    client = TelegramClient(
        StringSession(cfg["session"]) if cfg["session"] else StringSession(),
        cfg["api_id"],
        cfg["api_hash"],
    )
    await client.start(phone=cfg["phone"] or None)

    if export_session:
        # Преднамеренный вывод в stdout: запускается ОДИН РАЗ локально, чтобы
        # владелец скопировал строку в SECRETARY_SEEN_SECRET-совместимый env.
        print("\n=== TELEGRAM_SESSION (храните в секрете, не коммитьте) ===")
        print(client.session.save())
        print("=== конец ===\n")
        await client.disconnect()
        return

    log.info("watcher запущен, слушаю прочтения входящих сообщений")

    async with aiohttp.ClientSession() as http:
        async def on_raw(event_update):
            await handle_update(event_update, http, cfg)

        client.add_event_handler(on_raw, events.Raw(types.UpdateReadHistoryInbox))

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in ("SIGINT", "SIGTERM"):
            import signal
            signum = getattr(signal, sig, None)
            if signum is not None:
                try:
                    loop.add_signal_handler(signum, stop.set)
                except NotImplementedError:  # Windows
                    pass
        try:
            await stop.wait()
        finally:
            log.info("останавливаюсь")
            await client.disconnect()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    export = "--export-session" in sys.argv[1:]
    cfg = load_config()
    try:
        asyncio.run(_run(cfg, export_session=export))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
