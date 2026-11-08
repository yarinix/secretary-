"""Устойчивое хранилище во внешнем Postgres (Supabase / Neon / Render Postgres).

Интерфейс намеренно повторяет ``storage.Storage``, поэтому main.py переключает
бэкенд одной строкой::

    storage = PgStorage(cfg.DATABASE_URL) if cfg.DATABASE_URL else Storage(cfg.DB_PATH)

Зачем: у Free-сервисов Render эфемерная файловая система — локальный SQLite
(``secretary.db``) теряется при redeploy, restart и даже при spin down. Внешняя
БД живёт отдельно от сервиса, поэтому правки из Mini App переживают перезапуск.

Детали реализации:
  * ``psycopg`` импортируется отложенно: без ``DATABASE_URL`` проект работает на
    чистом SQLite и драйвер вообще не нужен;
  * одно соединение на процесс, все обращения под ``RLock`` (вызовы приходят из
    ``asyncio.to_thread``, aiogram- и aiohttp-хендлеров);
  * при обрыве соединения — один автоматический реконнект;
  * DSN содержит пароль: он не логируется, не попадает в тексты исключений для
    клиента и не уходит в Mini App.

Соединение и драйвер можно подменить (``connector`` / ``driver``) — этим пользуются
тесты, им не нужен реальный Postgres.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Callable

from pending_codec import (
    SELECT_COLUMNS,
    encode_ids,
    encode_parts,
    row_to_pending,
)

log = logging.getLogger("secretary.pg")


def connect_kwargs(connect_timeout: int = 10) -> dict:
    """Параметры psycopg-соединения.

    ``prepare_threshold=None`` отключает автоматические prepared statements:
    transaction pooler Supabase (порт 6543) их не поддерживает и может упасть
    с «prepared statement already exists». Для нашего небольшого бота это
    незаметная потеря производительности, зато работают оба варианта pooler'а
    (session 5432 и transaction 6543) и обычный Postgres.
    """
    return {
        "autocommit": True,
        "connect_timeout": connect_timeout,
        "prepare_threshold": None,
    }


# DDL выполняем по одной команде: psycopg3 с расширенным протоколом не даёт
# скармливать несколько statement'ов одним execute().
SCHEMA_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS secretary_settings ("
    " key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS secretary_history ("
    " id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL,"
    " role TEXT NOT NULL, text TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS secretary_connections ("
    " id TEXT PRIMARY KEY, user_id BIGINT, user_chat_id BIGINT,"
    " can_reply INTEGER, is_enabled INTEGER)",
    "CREATE INDEX IF NOT EXISTS secretary_history_chat_idx"
    " ON secretary_history (chat_id, id DESC)",
    # --- отложенные пачки ответов -----------------------------------------
    "CREATE TABLE IF NOT EXISTS secretary_pending ("
    " chat_id BIGINT PRIMARY KEY, parts TEXT NOT NULL,"
    " due_at DOUBLE PRECISION NOT NULL, created_at DOUBLE PRECISION NOT NULL,"
    " last_at DOUBLE PRECISION NOT NULL, mode TEXT NOT NULL, seen_ids TEXT NOT NULL,"
    " notify_message_id BIGINT, owner_chat_id BIGINT NOT NULL, conn_id TEXT NOT NULL,"
    " file_id TEXT, file_mime TEXT, claimed_at DOUBLE PRECISION, claim_token TEXT,"
    " attempts INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS secretary_pending_due_idx"
    " ON secretary_pending(due_at) WHERE claimed_at IS NULL",
    "CREATE TABLE IF NOT EXISTS secretary_meta ("
    " key TEXT PRIMARY KEY, value TEXT)",
)

_SQL_UPSERT_SETTING = (
    "INSERT INTO secretary_settings (key, value) VALUES (%s, %s) "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
)
_SQL_INSERT_HISTORY = (
    "INSERT INTO secretary_history (chat_id, role, text) VALUES (%s, %s, %s)"
)
_SQL_TRIM_HISTORY = (
    "DELETE FROM secretary_history WHERE chat_id=%s AND id NOT IN "
    "(SELECT id FROM secretary_history WHERE chat_id=%s ORDER BY id DESC LIMIT %s)"
)
_SQL_GET_HISTORY = (
    "SELECT role, text FROM ("
    "SELECT id, role, text FROM secretary_history WHERE chat_id=%s "
    "ORDER BY id DESC LIMIT %s) t ORDER BY id"
)
_SQL_UPSERT_CONNECTION = (
    "INSERT INTO secretary_connections "
    "(id, user_id, user_chat_id, can_reply, is_enabled) "
    "VALUES (%s, %s, %s, %s, %s) "
    "ON CONFLICT (id) DO UPDATE SET "
    "user_id=EXCLUDED.user_id, user_chat_id=EXCLUDED.user_chat_id, "
    "can_reply=EXCLUDED.can_reply, is_enabled=EXCLUDED.is_enabled"
)

_SQL_SELECT_PENDING = f"SELECT {SELECT_COLUMNS} FROM secretary_pending WHERE chat_id=%s"


class PgStorage:
    """Тот же контракт, что у SQLite-``Storage``, но поверх внешней БД."""

    backend_name = "postgres"

    def __init__(
        self,
        dsn: str,
        *,
        connect_timeout: int = 10,
        driver: Any = None,
        connector: Callable[[str, int], Any] | None = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip().startswith(
            ("postgresql://", "postgres://")
        ):
            raise ValueError(
                "DATABASE_URL должен быть DSN вида postgresql://user:pass@host:5432/db"
            )
        self._dsn = dsn.strip()
        self._connect_timeout = connect_timeout
        self._lock = threading.RLock()
        self._conn: Any = None

        if driver is None:
            try:
                import psycopg  # noqa: PLC0415  (осознанно отложенный импорт)
                driver = psycopg
            except ImportError as e:
                raise SystemExit(
                    "DATABASE_URL задан, но драйвер не установлен: "
                    "pip install 'psycopg[binary]>=3.1'"
                ) from e
        self._psycopg = driver
        self._connector = connector or (
            lambda d, t: driver.connect(d, **connect_kwargs(t))
        )
        self._connect_locked()

    # ---------- соединение ----------

    def _connect_locked(self) -> None:
        conn = self._connector(self._dsn, self._connect_timeout)
        try:
            with conn.cursor() as cur:
                for stmt in SCHEMA_STATEMENTS:
                    cur.execute(stmt)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise
        self._conn = conn
        log.info("Postgres: соединение установлено, схема готова")

    def _call(
        self, fn: Callable[[Any], Any], *, transactional: bool = False
    ) -> Any:
        """Выполняет ``fn(conn)`` под локом; при обрыве — один реконнект."""
        last: Exception | None = None
        for _attempt in range(2):
            try:
                with self._lock:
                    if self._conn is None or getattr(self._conn, "closed", False):
                        self._connect_locked()
                    conn = self._conn
                    if transactional:
                        with conn.transaction():
                            return fn(conn)
                    return fn(conn)
            except self._psycopg.OperationalError as e:
                last = e
                # Ни DSN, ни текст ошибки (там может быть хост) в лог не пишем.
                log.warning("Postgres: обрыв соединения (%s), реконнект", type(e).__name__)
                with self._lock:
                    old, self._conn = self._conn, None
                if old is not None:
                    try:
                        old.close()
                    except Exception:
                        pass
        assert last is not None
        raise last

    # ---------- настройки ----------

    def get_setting(self, key: str, default: str = "") -> str:
        def fn(conn: Any) -> str:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM secretary_settings WHERE key=%s", (key,))
                row = cur.fetchone()
            return row[0] if row and row[0] is not None else default

        return self._call(fn)

    def get_all_settings(self) -> dict[str, str]:
        def fn(conn: Any) -> dict[str, str]:
            with conn.cursor() as cur:
                cur.execute("SELECT key, value FROM secretary_settings")
                rows = cur.fetchall()
            return {k: v for k, v in rows if v is not None}

        return self._call(fn)

    def set_setting(self, key: str, value: str) -> None:
        self.set_settings_many({key: value})

    def set_settings_many(self, pairs: dict[str, str]) -> None:
        """Атомарная запись всего patch: либо все ключи, ни одного."""
        if not pairs:
            return
        rows = [(str(k), str(v)) for k, v in pairs.items()]

        def fn(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.executemany(_SQL_UPSERT_SETTING, rows)

        self._call(fn, transactional=True)

    # ---------- история ----------

    def save_message(self, chat_id: int, role: str, text: str, keep: int = 40) -> None:
        def fn(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute(_SQL_INSERT_HISTORY, (chat_id, role, text))
                cur.execute(_SQL_TRIM_HISTORY, (chat_id, chat_id, keep))

        self._call(fn, transactional=True)

    def get_history(self, chat_id: int, limit: int) -> list[dict]:
        def fn(conn: Any) -> list[dict]:
            with conn.cursor() as cur:
                cur.execute(_SQL_GET_HISTORY, (chat_id, limit))
                rows = cur.fetchall()
            return [{"role": r, "text": t} for r, t in rows]

        return self._call(fn)

    # ---------- подключения Telegram Business ----------

    def save_connection(self, cid: str, info: dict) -> None:
        values = (
            cid,
            info["user_id"],
            info["user_chat_id"],
            int(info["can_reply"]),
            int(info["enabled"]),
        )

        def fn(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute(_SQL_UPSERT_CONNECTION, values)

        self._call(fn)

    def load_connections(self) -> dict[str, dict]:
        def fn(conn: Any) -> dict[str, dict]:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, user_id, user_chat_id, can_reply, is_enabled "
                    "FROM secretary_connections"
                )
                rows = cur.fetchall()
            return {
                cid: {
                    "user_id": uid,
                    "user_chat_id": ucid,
                    "can_reply": bool(can_reply),
                    "enabled": bool(enabled),
                }
                for cid, uid, ucid, can_reply, enabled in rows
            }

        return self._call(fn)

    # ---------- meta (глобальное присутствие владельца, update_id) ----------

    def get_meta(self, key: str, default=None):
        def fn(conn: Any):
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM secretary_meta WHERE key=%s", (key,))
                row = cur.fetchone()
            return row[0] if row and row[0] is not None else default

        return self._call(fn)

    def set_meta(self, key: str, value) -> None:
        def fn(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO secretary_meta (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                    (key, str(value)),
                )

        self._call(fn)

    # ---------- отложенные пачки (тот же контракт, что Storage) ----------

    def get_pending(self, chat_id: int) -> dict | None:
        def fn(conn: Any):
            with conn.cursor() as cur:
                cur.execute(_SQL_SELECT_PENDING, (chat_id,))
                return cur.fetchone()

        row = self._call(fn)
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
        """Добавляет сообщение в пачку и сдвигает ``due_at`` (debounce).

        Выполняется в одной транзакции: SELECT → INSERT/UPDATE → SELECT.
        При ``DATABASE_URL`` это один процесс с ``RLock``, поэтому гонки между
        воркерами исключены; ``RETURNING`` в claim_due страхует межпроцессный
        случай (несколько инстансов на одном DSN).
        """

        def fn(conn: Any) -> dict:
            with conn.cursor() as cur:
                cur.execute(_SQL_SELECT_PENDING, (chat_id,))
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "INSERT INTO secretary_pending (chat_id, parts, due_at, created_at,"
                        " last_at, mode, seen_ids, notify_message_id, owner_chat_id,"
                        " conn_id, file_id, file_mime, claimed_at, claim_token, attempts) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            chat_id, encode_parts([part]), due_at, now, now, mode,
                            encode_ids([message_id] if message_id is not None else []),
                            None, owner_chat_id, conn_id, file_id, file_mime,
                            None, None, 0,
                        ),
                    )
                    cur.execute(_SQL_SELECT_PENDING, (chat_id,))
                    out = row_to_pending(cur.fetchone())
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
                cur.execute(
                    "UPDATE secretary_pending SET parts=%s, due_at=%s, last_at=%s,"
                    " mode=%s, seen_ids=%s, conn_id=%s, owner_chat_id=%s,"
                    " file_id=COALESCE(%s, file_id), file_mime=COALESCE(%s, file_mime),"
                    " claimed_at=NULL, claim_token=NULL, attempts=0 WHERE chat_id=%s",
                    (
                        encode_parts(parts), due_at, now, mode, encode_ids(ids),
                        conn_id, owner_chat_id, file_id, file_mime, chat_id,
                    ),
                )
                cur.execute(_SQL_SELECT_PENDING, (chat_id,))
                out = row_to_pending(cur.fetchone())
                out["duplicate"] = False
                return out

        return self._call(fn, transactional=True)

    def set_due_now(self, chat_id: int, now: float) -> bool:
        """Кнопка «Ответить сейчас»: дедлайн пачки = сейчас. Захваченную не трогаем."""

        def fn(conn: Any) -> bool:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE secretary_pending SET due_at=%s "
                    "WHERE chat_id=%s AND claimed_at IS NULL",
                    (float(now), chat_id),
                )
                return cur.rowcount > 0

        return self._call(fn)

    def set_notify_message_id(self, chat_id: int, message_id: int | None) -> None:
        def fn(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE secretary_pending SET notify_message_id=%s WHERE chat_id=%s",
                    (message_id, chat_id),
                )

        self._call(fn)

    def cancel_pending(self, chat_id: int) -> dict | None:
        """Владелец ответил сам: удаляем пачку ТОЛЬКО этого чата."""

        def fn(conn: Any):
            with conn.cursor() as cur:
                cur.execute(_SQL_SELECT_PENDING, (chat_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                cur.execute("DELETE FROM secretary_pending WHERE chat_id=%s", (chat_id,))
            return row_to_pending(row)

        return self._call(fn, transactional=True)

    def claim_due(
        self, now: float, limit: int = 20, stale_after: float = 120
    ) -> list[dict]:
        """At-least-once захват через ``UPDATE … RETURNING``.

        Пачка НЕ удаляется до подтверждённой отправки: при сбое Gemini/сети
        или рестарте между SELECT и DELETE сообщение было бы потеряно
        навсегда, а владелец уже жаловался на потерю сообщений.
        """
        cutoff = float(now) - float(stale_after)

        def fn(conn: Any) -> list[dict]:
            claimed: list[dict] = []
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT chat_id FROM secretary_pending "
                    "WHERE due_at<=%s AND (claimed_at IS NULL OR claimed_at<%s) "
                    "ORDER BY due_at LIMIT %s",
                    (now, cutoff, int(limit)),
                )
                candidates = cur.fetchall()
                for (cid,) in candidates:
                    token = uuid.uuid4().hex
                    cur.execute(
                        "UPDATE secretary_pending SET claimed_at=%s, claim_token=%s,"
                        " attempts=attempts+1 WHERE chat_id=%s "
                        "AND (claimed_at IS NULL OR claimed_at<%s) "
                        f"RETURNING {SELECT_COLUMNS}",
                        (now, token, cid, cutoff),
                    )
                    row = cur.fetchone()
                    if row:
                        claimed.append(row_to_pending(row))
            return claimed

        return self._call(fn, transactional=True)

    def delete_pending(self, chat_id: int, claim_token: str | None) -> bool:
        def fn(conn: Any) -> bool:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM secretary_pending WHERE chat_id=%s AND claim_token=%s",
                    (chat_id, claim_token),
                )
            return cur.rowcount > 0

        return self._call(fn)

    def pending_stats(self) -> dict:
        def fn(conn: Any) -> dict:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*), MIN(due_at) FROM secretary_pending "
                    "WHERE claimed_at IS NULL"
                )
                row = cur.fetchone()
            return {
                "pending_chats": int(row[0] or 0),
                "next_due_at": float(row[1]) if row[1] is not None else None,
            }

        return self._call(fn)

    def purge_stale(self, now: float, max_age: float = 86400) -> int:
        def fn(conn: Any) -> int:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM secretary_pending WHERE last_at < %s",
                    (float(now) - float(max_age),),
                )
            return cur.rowcount

        return self._call(fn)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
