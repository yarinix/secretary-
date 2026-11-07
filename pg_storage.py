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
from typing import Any, Callable

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

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
