"""Локальное хранилище на SQLite.

Синхронные методы без aiogram: в main вызываются через asyncio.to_thread,
поэтому блокирующие запросы не останавливают event loop. Изменения в одной
операции обёрнуты в `with self.db` (атомарная транзакция).

Mini App читает и пишет настройки из нескольких потоков (aiogram-хендлеры,
aiohttp-хендлеры, asyncio.to_thread) через ОДИН общий коннект с
check_same_thread=False. Поэтому все обращения защищены RLock, а БД
переведена в WAL: запись настроек не блокирует чтение истории.
"""

import sqlite3
import threading


class Storage:
    def __init__(self, path: str = "secretary.db"):
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            # PRAGMA выполняем вне явной транзакции: внутри `with self.db`
            # смена journal_mode запрещена.
            self.db.execute("PRAGMA journal_mode=WAL").fetchone()
            with self.db:
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS business_connections ("
                    "id TEXT PRIMARY KEY, user_id INTEGER, user_chat_id INTEGER, "
                    "can_reply INTEGER, is_enabled INTEGER)"
                )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
                )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS history ("
                    "id INTEGER PRIMARY KEY, chat_id INTEGER, role TEXT, text TEXT)"
                )

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row and row[0] is not None else default

    def get_all_settings(self) -> dict[str, str]:
        """Все настройки одним запросом — вместо 12 последовательных чтений."""
        with self._lock:
            rows = self.db.execute("SELECT key, value FROM settings").fetchall()
        return {k: v for k, v in rows if v is not None}

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value))
            )

    def set_settings_many(self, pairs: dict[str, str]) -> None:
        """Атомарная запись всего patch: либо применились все ключи, либо ни один."""
        if not pairs:
            return
        with self._lock, self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                [(k, str(v)) for k, v in pairs.items()],
            )

    def save_message(self, chat_id: int, role: str, text: str, keep: int = 40) -> None:
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO history (chat_id, role, text) VALUES (?, ?, ?)",
                (chat_id, role, text),
            )
            self.db.execute(
                "DELETE FROM history WHERE chat_id=? AND id NOT IN "
                "(SELECT id FROM history WHERE chat_id=? ORDER BY id DESC LIMIT ?)",
                (chat_id, chat_id, keep),
            )

    def get_history(self, chat_id: int, limit: int) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT role, text FROM (SELECT id, role, text FROM history WHERE chat_id=? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id",
                (chat_id, limit),
            ).fetchall()
        return [{"role": r, "text": t} for r, t in rows]

    def save_connection(self, cid: str, info: dict) -> None:
        with self._lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO business_connections VALUES (?, ?, ?, ?, ?)",
                (
                    cid,
                    info["user_id"],
                    info["user_chat_id"],
                    int(info["can_reply"]),
                    int(info["enabled"]),
                ),
            )

    def load_connections(self) -> dict[str, dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM business_connections").fetchall()
        return {
            cid: {
                "user_id": uid,
                "user_chat_id": ucid,
                "can_reply": bool(can_reply),
                "enabled": bool(enabled),
            }
            for cid, uid, ucid, can_reply, enabled in rows
        }
