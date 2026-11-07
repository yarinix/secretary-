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
import uuid

from pending_codec import (
    SELECT_COLUMNS,
    encode_ids,
    encode_parts,
    row_to_pending,
)


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
                # --- отложенные пачки ответов ---------------------------------
                # due_at/created_at/last_at/claimed_at — wall-clock epoch
                # (time.time()), НЕ monotonic(): счётчик monotonic сбрасывается
                # при каждом сне/рестарте Render Free и не сравним между
                # процессами. parts и seen_ids — JSON-TEXT (одинаково с Postgres).
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS secretary_pending ("
                    " chat_id INTEGER PRIMARY KEY, parts TEXT NOT NULL,"
                    " due_at REAL NOT NULL, created_at REAL NOT NULL, last_at REAL NOT NULL,"
                    " mode TEXT NOT NULL, seen_ids TEXT NOT NULL,"
                    " notify_message_id INTEGER, owner_chat_id INTEGER NOT NULL,"
                    " conn_id TEXT NOT NULL, file_id TEXT, file_mime TEXT,"
                    " claimed_at REAL, claim_token TEXT, attempts INTEGER NOT NULL DEFAULT 0)"
                )
                self.db.execute(
                    "CREATE INDEX IF NOT EXISTS secretary_pending_due_idx "
                    "ON secretary_pending(due_at) WHERE claimed_at IS NULL"
                )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS secretary_meta ("
                    " key TEXT PRIMARY KEY, value TEXT)"
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

    # ---------- meta (глобальное присутствие владельца, update_id) ----------

    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self.db.execute(
                "SELECT value FROM secretary_meta WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row and row[0] is not None else default

    def set_meta(self, key: str, value) -> None:
        with self._lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO secretary_meta (key, value) VALUES (?, ?)",
                (key, str(value)),
            )

    # ---------- отложенные пачки ----------

    def _read_pending_locked(self, chat_id: int):
        return self.db.execute(
            f"SELECT {SELECT_COLUMNS} FROM secretary_pending WHERE chat_id=?",
            (chat_id,),
        ).fetchone()

    def get_pending(self, chat_id: int) -> dict | None:
        with self._lock:
            row = self._read_pending_locked(chat_id)
        return row_to_pending(row) if row else None

    def upsert_pending(
        self,
        *,
        chat_id: int,
        part: str,
        message_id: int | None,
        due_at: float,
        now: float,
        mode: str,
        conn_id: str,
        owner_chat_id: int,
        file_id: str | None = None,
        file_mime: str | None = None,
    ) -> dict:
        """Добавляет сообщение в пачку и СДВИГАЕТ дедлайн (debounce).

        Возвращает строку пачки с полем ``duplicate``. Дубль того же
        ``message_id`` (повторная доставка вебхука после рестарта, когда
        in-memory DedupGuard пуст) не меняет пачку и не сдвигает дедлайн.
        """
        with self._lock, self.db:
            row = self._read_pending_locked(chat_id)
            if row is None:
                self.db.execute(
                    "INSERT INTO secretary_pending (chat_id, parts, due_at, created_at,"
                    " last_at, mode, seen_ids, notify_message_id, owner_chat_id, conn_id,"
                    " file_id, file_mime, claimed_at, claim_token, attempts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chat_id, encode_parts([part]), due_at, now, now, mode,
                        encode_ids([message_id] if message_id is not None else []),
                        None, owner_chat_id, conn_id, file_id, file_mime, None, None, 0,
                    ),
                )
                out = row_to_pending(self._read_pending_locked(chat_id))
                out["duplicate"] = False
                return out

            existing = row_to_pending(row)
            if message_id is not None and int(message_id) in existing["seen_ids"]:
                out = dict(existing)
                out["duplicate"] = True
                return out

            parts = existing["parts"] + [str(part)]
            ids = existing["seen_ids"] + (
                [int(message_id)] if message_id is not None else []
            )
            # Новое сообщение = новая пачка: сбрасываем захват, чтобы воркер,
            # уже отправивший старый ответ, не удалил свежие данные.
            self.db.execute(
                "UPDATE secretary_pending SET parts=?, due_at=?, last_at=?, mode=?,"
                " seen_ids=?, conn_id=?, owner_chat_id=?,"
                " file_id=COALESCE(?, file_id), file_mime=COALESCE(?, file_mime),"
                " claimed_at=NULL, claim_token=NULL, attempts=0 WHERE chat_id=?",
                (
                    encode_parts(parts), due_at, now, mode, encode_ids(ids),
                    conn_id, owner_chat_id, file_id, file_mime, chat_id,
                ),
            )
            out = row_to_pending(self._read_pending_locked(chat_id))
            out["duplicate"] = False
            return out

    def set_notify_message_id(self, chat_id: int, message_id: int | None) -> None:
        with self._lock, self.db:
            self.db.execute(
                "UPDATE secretary_pending SET notify_message_id=? WHERE chat_id=?",
                (message_id, chat_id),
            )

    def cancel_pending(self, chat_id: int) -> dict | None:
        """Владелец ответил сам: удаляем пачку ТОЛЬКО этого чата."""
        with self._lock, self.db:
            row = self._read_pending_locked(chat_id)
            if row is None:
                return None
            self.db.execute("DELETE FROM secretary_pending WHERE chat_id=?", (chat_id,))
        return row_to_pending(row)

    def claim_due(
        self, now: float, limit: int = 20, stale_after: float = 120
    ) -> list[dict]:
        """At-least-once захват: НЕ удаляем пачку до подтверждённой отправки.

        Каждая строка получает свой ``claim_token``; зависший захват старше
        ``stale_after`` секунд перехватывается повторно. Всё под одним RLock и
        в одной транзакции, поэтому гонок между воркерами одного процесса нет.
        """
        cutoff = float(now) - float(stale_after)
        claimed: list[dict] = []
        with self._lock, self.db:
            candidates = self.db.execute(
                "SELECT chat_id FROM secretary_pending "
                "WHERE due_at<=? AND (claimed_at IS NULL OR claimed_at<?) "
                "ORDER BY due_at LIMIT ?",
                (now, cutoff, int(limit)),
            ).fetchall()
            for (cid,) in candidates:
                token = uuid.uuid4().hex
                row = self.db.execute(
                    "UPDATE secretary_pending SET claimed_at=?, claim_token=?, "
                    "attempts=attempts+1 WHERE chat_id=? "
                    "AND (claimed_at IS NULL OR claimed_at<?) "
                    f"RETURNING {SELECT_COLUMNS}",
                    (now, token, cid, cutoff),
                ).fetchone()
                if row:
                    claimed.append(row_to_pending(row))
        return claimed

    def delete_pending(self, chat_id: int, claim_token: str | None) -> bool:
        """Удаляет пачку только если её никто не пересоздал (токен совпал)."""
        with self._lock, self.db:
            cur = self.db.execute(
                "DELETE FROM secretary_pending WHERE chat_id=? AND claim_token=?",
                (chat_id, claim_token),
            )
        return cur.rowcount > 0

    def pending_stats(self) -> dict:
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*), MIN(due_at) FROM secretary_pending "
                "WHERE claimed_at IS NULL"
            ).fetchone()
        return {
            "pending_chats": int(row[0] or 0),
            "next_due_at": float(row[1]) if row[1] is not None else None,
        }

    def purge_stale(self, now: float, max_age: float = 86400) -> int:
        """Чистит пачки, которые не двигались больше суток (зависшие)."""
        with self._lock, self.db:
            cur = self.db.execute(
                "DELETE FROM secretary_pending WHERE last_at < ?",
                (float(now) - float(max_age),),
            )
        return cur.rowcount
