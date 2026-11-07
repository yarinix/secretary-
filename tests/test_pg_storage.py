"""Тесты SQL-контракта PgStorage на подставном соединении.

Реальный Postgres не нужен: проверяем DSN, DDL, параметры запросов,
атомарность записи и реконнект при обрыве.
"""

import unittest

from pending_codec import PENDING_COLUMNS, row_to_pending
from pg_storage import PgStorage, SCHEMA_STATEMENTS, connect_kwargs

DSN = "postgresql://user:secret@db.example.com:5432/secretary?sslmode=require"


class FakeDriver:
    class OperationalError(Exception):
        pass

    @staticmethod
    def connect(*args, **kwargs):
        raise AssertionError("в тестах всегда инъектируется connector")


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append(("execute", sql, params))
        if self.conn.fail_marker and self.conn.fail_marker in sql and self.conn.fail_left > 0:
            self.conn.fail_left -= 1
            raise FakeDriver.OperationalError("обрыв соединения")
        return None

    def executemany(self, sql, rows):
        self.conn.calls.append(("executemany", sql, list(rows)))

    def fetchone(self):
        # Очередь нужна для upsert_pending: SELECT -> INSERT -> SELECT.
        if self.conn.ones:
            return self.conn.ones.pop(0)
        return self.conn.one

    def fetchall(self):
        return self.conn.many

    @property
    def rowcount(self):
        return self.conn.rowcount


class FakeConn:
    def __init__(self, *, one=None, many=None, ones=None, rowcount=0,
                 fail_marker=None, fail_left=0):
        self.calls = []
        self.one = one
        self.many = many if many is not None else []
        self.ones = list(ones) if ones is not None else []
        self.rowcount = rowcount
        self.fail_marker = fail_marker
        self.fail_left = fail_left
        self.closed = False
        self.transactions = 0

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        self.transactions += 1
        return _NullCtx()

    def close(self):
        self.closed = True


def build(conns, *, timeout=7):
    """connector выдаёт соединения по очереди (второй — после реконнекта)."""
    issued = []
    remaining = list(conns)

    def connector(dsn, t):
        issued.append((dsn, t))
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    st = PgStorage(
        DSN, driver=FakeDriver, connector=connector, connect_timeout=timeout
    )
    return st, issued


def pending_row(**over):
    """Строка secretary_pending в порядке PENDING_COLUMNS."""
    base = dict(
        chat_id=1, parts='["Иван: привет"]', due_at=1600.0,
        created_at=1000.0, last_at=1000.0, mode="wait", seen_ids="[11]",
        notify_message_id=None, owner_chat_id=99, conn_id="cid",
        file_id=None, file_mime=None, claimed_at=None, claim_token=None,
        attempts=0,
    )
    base.update(over)
    return tuple(base[k] for k in PENDING_COLUMNS)


class TestPgStorageDSN(unittest.TestCase):
    def test_rejects_bad_dsn(self):
        for bad in ["", "   ", "sqlite:///x.db", "host=db user=u", None, 42,
                    "mysql://u:p@h/db"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                PgStorage(bad, driver=FakeDriver, connector=lambda d, t: FakeConn())

    def test_accepts_postgres_schemes(self):
        for good in ("postgresql://u:p@h/db", "postgres://u:p@h/db?sslmode=require"):
            with self.subTest(good=good):
                issued = []
                conn = FakeConn()

                def connector(dsn, t, _issued=issued, _conn=conn):
                    _issued.append((dsn, t))
                    return _conn

                st = PgStorage(good, driver=FakeDriver, connector=connector,
                               connect_timeout=7)
                self.assertEqual(issued, [(good, 7)])
                st.close()

    def test_backend_name(self):
        self.assertEqual(PgStorage.backend_name, "postgres")

    def test_connect_kwargs_disable_prepared_statements(self):
        self.assertEqual(connect_kwargs(13), {
            "autocommit": True,
            "connect_timeout": 13,
            "prepare_threshold": None,
        })


class TestPgStorageSchema(unittest.TestCase):
    def test_ddl_executed_one_statement_at_a_time(self):
        conn = FakeConn()
        st, issued = build([conn])
        ddl = [sql for kind, sql, _ in conn.calls if kind == "execute"]
        self.assertEqual(ddl, list(SCHEMA_STATEMENTS))
        self.assertEqual(issued, [(DSN, 7)])
        st.close()

    def test_schema_has_pending_and_meta(self):
        self.assertEqual(len(SCHEMA_STATEMENTS), 7)
        joined = "\n".join(SCHEMA_STATEMENTS)
        self.assertIn("secretary_pending", joined)
        self.assertIn("secretary_meta", joined)
        self.assertIn("CREATE INDEX IF NOT EXISTS secretary_pending_due_idx", joined)
        # Ни один statement не содержит двух DDL сразу: pooler Supabase
        # не умеет выполнять несколько команд одним execute().
        self.assertFalse(any("CREATE TABLE" in s and "CREATE INDEX" in s
                             for s in SCHEMA_STATEMENTS))

    def test_dsn_and_timeout_passed_to_connector(self):
        conn = FakeConn()
        st, issued = build([conn], timeout=13)
        self.assertEqual(issued, [(DSN, 13)])
        st.close()


class TestPgStorageSettings(unittest.TestCase):
    def test_get_all_settings_drops_nulls(self):
        conn = FakeConn(many=[("reply_delay_minutes", "10"), ("persona", "будь краток"),
                              ("broken", None)])
        st, _ = build([conn])
        self.assertEqual(st.get_all_settings(),
                         {"reply_delay_minutes": "10", "persona": "будь краток"})
        st.close()

    def test_get_setting_returns_default_when_missing(self):
        conn = FakeConn(one=None)
        st, _ = build([conn])
        self.assertEqual(st.get_setting("persona", "дефолт"), "дефолт")
        st.close()

    def test_get_setting_returns_value(self):
        conn = FakeConn(one=("10",))
        st, _ = build([conn])
        self.assertEqual(st.get_setting("reply_delay_minutes"), "10")
        st.close()

    def test_set_settings_many_is_atomic(self):
        conn = FakeConn()
        st, _ = build([conn])
        st.set_settings_many({"reply_delay_minutes": "10", "enabled": "1"})
        upserts = [c for c in conn.calls if c[0] == "executemany"]
        self.assertEqual(len(upserts), 1)
        _, sql, rows = upserts[0]
        self.assertIn("ON CONFLICT (key) DO UPDATE", sql)
        self.assertEqual(rows, [("reply_delay_minutes", "10"), ("enabled", "1")])
        self.assertEqual(conn.transactions, 1)
        st.close()

    def test_set_setting_delegates_to_bulk(self):
        conn = FakeConn()
        st, _ = build([conn])
        st.set_setting("persona", "коротко")
        upserts = [c for c in conn.calls if c[0] == "executemany"]
        self.assertEqual(upserts[0][2], [("persona", "коротко")])
        st.close()

    def test_empty_patch_writes_nothing(self):
        conn = FakeConn()
        st, _ = build([conn])
        st.set_settings_many({})
        self.assertFalse(any(c[0] == "executemany" for c in conn.calls))
        self.assertEqual(conn.transactions, 0)
        st.close()


class TestPgStorageHistory(unittest.TestCase):
    def test_save_message_is_transactional_and_trims(self):
        conn = FakeConn()
        st, _ = build([conn])
        st.save_message(42, "user", "привет", keep=15)
        execs = [c for c in conn.calls if c[0] == "execute"]
        self.assertEqual(execs[-2][2], (42, "user", "привет"))
        self.assertEqual(execs[-1][2], (42, 42, 15))
        self.assertIn("DELETE FROM secretary_history", execs[-1][1])
        self.assertEqual(conn.transactions, 1)
        st.close()

    def test_get_history_shape_and_params(self):
        conn = FakeConn(many=[("user", "привет"), ("model", "здравствуйте")])
        st, _ = build([conn])
        self.assertEqual(st.get_history(42, 12),
                         [{"role": "user", "text": "привет"},
                          {"role": "model", "text": "здравствуйте"}])
        select = [c for c in conn.calls if c[0] == "execute" and c[2] == (42, 12)]
        self.assertEqual(len(select), 1)
        st.close()


class TestPgStorageConnections(unittest.TestCase):
    def test_save_connection_params(self):
        conn = FakeConn()
        st, _ = build([conn])
        st.save_connection("cid-1", {"user_id": 5084782149, "user_chat_id": 77,
                                     "can_reply": True, "enabled": False})
        upsert = [c for c in conn.calls if c[0] == "execute" and c[2]][-1]
        self.assertEqual(upsert[2], ("cid-1", 5084782149, 77, 1, 0))
        st.close()

    def test_load_connections_maps_bools(self):
        conn = FakeConn(many=[("cid-1", 5084782149, 77, 1, 0),
                              ("cid-2", 5084782149, 88, 0, 1)])
        st, _ = build([conn])
        data = st.load_connections()
        self.assertEqual(data["cid-1"], {"user_id": 5084782149, "user_chat_id": 77,
                                         "can_reply": True, "enabled": False})
        self.assertTrue(data["cid-2"]["enabled"])
        self.assertFalse(data["cid-2"]["can_reply"])
        st.close()


class TestPgStorageReconnect(unittest.TestCase):
    def test_operational_error_triggers_single_reconnect(self):
        first = FakeConn(fail_marker="SELECT key, value", fail_left=1)
        second = FakeConn(many=[("reply_delay_minutes", "10")])
        st, issued = build([first, second])
        self.assertEqual(st.get_all_settings(), {"reply_delay_minutes": "10"})
        self.assertTrue(first.closed)
        self.assertEqual(len(issued), 2)  # ровно один реконнект
        st.close()

    def test_persistent_failure_is_raised(self):
        conn = FakeConn(fail_marker="SELECT key, value", fail_left=5)
        st, issued = build([conn])
        with self.assertRaises(FakeDriver.OperationalError):
            st.get_all_settings()
        self.assertEqual(len(issued), 2)  # не уходим в бесконечный цикл
        st.close()

    def test_close_marks_connection(self):
        conn = FakeConn()
        st, _ = build([conn])
        st.close()
        self.assertTrue(conn.closed)
        self.assertIsNone(st._conn)


class TestPgPendingContract(unittest.TestCase):
    """SQL-контракт пачек на подставном соединении (реальный Postgres не нужен)."""

    def test_codec_roundtrip(self):
        row = row_to_pending(pending_row())
        self.assertEqual(row["parts"], ["Иван: привет"])
        self.assertEqual(row["seen_ids"], [11])
        self.assertEqual(row["due_at"], 1600.0)
        self.assertEqual(row["conn_id"], "cid")
        self.assertIsNone(row["claim_token"])

    def test_upsert_insert_sql_and_params(self):
        conn = FakeConn(ones=[None, pending_row()])
        st, _ = build([conn])
        row = st.upsert_pending(
            chat_id=1, part="Иван: привет", message_id=11, due_at=1600.0,
            now=1000.0, mode="wait", conn_id="cid", owner_chat_id=99,
        )
        self.assertEqual(row["parts"], ["Иван: привет"])
        inserts = [c for c in conn.calls if c[0] == "execute" and
                   c[1].startswith("INSERT INTO secretary_pending")]
        self.assertEqual(len(inserts), 1)
        self.assertIn("VALUES (%s", inserts[0][1])
        self.assertEqual(inserts[0][2][0], 1)
        self.assertEqual(conn.transactions, 1)
        st.close()

    def test_upsert_existing_shifts_deadline_and_resets_claim(self):
        conn = FakeConn(ones=[pending_row(), pending_row(parts='["a","b"]',
                                                         seen_ids="[11,12]",
                                                         due_at=1700.0)])
        st, _ = build([conn])
        st.upsert_pending(
            chat_id=1, part="b", message_id=12, due_at=1700.0,
            now=1005.0, mode="away", conn_id="cid", owner_chat_id=99,
        )
        updates = [c for c in conn.calls if c[0] == "execute" and
                   c[1].startswith("UPDATE secretary_pending SET parts=")]
        self.assertEqual(len(updates), 1)
        self.assertIn("claimed_at=NULL", updates[0][1])
        self.assertIn("COALESCE(%s, file_id)", updates[0][1])
        self.assertEqual(conn.transactions, 1)
        st.close()

    def test_duplicate_message_id_skips_update(self):
        conn = FakeConn(one=pending_row(seen_ids="[11]"))
        st, _ = build([conn])
        row = st.upsert_pending(
            chat_id=1, part="Иван: привет", message_id=11, due_at=9999.0,
            now=1006.0, mode="wait", conn_id="cid", owner_chat_id=99,
        )
        self.assertTrue(row["duplicate"])
        self.assertFalse(any(c[1].startswith("UPDATE secretary_pending SET parts=")
                             for c in conn.calls if c[0] == "execute"))
        st.close()

    def test_claim_due_uses_update_returning_in_transaction(self):
        conn = FakeConn(many=[(1,), (2,)], one=pending_row(claim_token="tok", attempts=1))
        st, _ = build([conn])
        rows = st.claim_due(1700.0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["claim_token"], "tok")
        returning = [c for c in conn.calls if c[0] == "execute" and "RETURNING" in c[1]]
        self.assertEqual(len(returning), 2)
        self.assertIn("attempts=attempts+1", returning[0][1])
        self.assertIn("claimed_at<%s", returning[0][1])
        self.assertEqual(conn.transactions, 1)
        st.close()

    def test_delete_pending_uses_claim_token_and_rowcount(self):
        conn = FakeConn(rowcount=1)
        st, _ = build([conn])
        self.assertTrue(st.delete_pending(1, "tok"))
        deletes = [c for c in conn.calls if c[0] == "execute" and
                   c[1].startswith("DELETE FROM secretary_pending")]
        self.assertEqual(len(deletes), 1)
        self.assertIn("claim_token=%s", deletes[0][1])
        self.assertEqual(deletes[0][2], (1, "tok"))
        st.close()

    def test_cancel_pending_only_this_chat(self):
        conn = FakeConn(one=pending_row())
        st, _ = build([conn])
        row = st.cancel_pending(1)
        self.assertEqual(row["chat_id"], 1)
        deletes = [c for c in conn.calls if c[0] == "execute" and
                   c[1].startswith("DELETE FROM secretary_pending")]
        self.assertEqual(deletes[0][2], (1,))
        self.assertEqual(conn.transactions, 1)
        st.close()

    def test_pending_stats_and_purge(self):
        conn = FakeConn(one=(3, 1500.0), rowcount=2)
        st, _ = build([conn])
        self.assertEqual(st.pending_stats(),
                         {"pending_chats": 3, "next_due_at": 1500.0})
        self.assertEqual(st.purge_stale(100000.0), 2)
        purge = [c for c in conn.calls if c[0] == "execute" and
                 "last_at < %s" in c[1]]
        self.assertEqual(len(purge), 1)
        st.close()

    def test_meta_get_set(self):
        conn = FakeConn(one=("1234.5",))
        st, _ = build([conn])
        self.assertEqual(st.get_meta("last_owner_at"), "1234.5")
        st.set_meta("last_owner_at", "9999.0")
        upserts = [c for c in conn.calls if c[0] == "execute" and
                   "INSERT INTO secretary_meta" in c[1]]
        self.assertEqual(len(upserts), 1)
        self.assertIn("ON CONFLICT (key) DO UPDATE", upserts[0][1])
        self.assertEqual(upserts[0][2], ("last_owner_at", "9999.0"))
        st.close()

    def test_set_notify_message_id(self):
        conn = FakeConn()
        st, _ = build([conn])
        st.set_notify_message_id(1, 555)
        updates = [c for c in conn.calls if c[0] == "execute" and
                   "notify_message_id=%s" in c[1]]
        self.assertEqual(updates[0][2], (555, 1))
        st.close()


if __name__ == "__main__":
    unittest.main()
