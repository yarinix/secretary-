"""Тесты SQL-контракта PgStorage на подставном соединении.

Реальный Postgres не нужен: проверяем DSN, DDL, параметры запросов,
атомарность записи и реконнект при обрыве.
"""

import unittest

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
        return self.conn.one

    def fetchall(self):
        return self.conn.many


class FakeConn:
    def __init__(self, *, one=None, many=None, fail_marker=None, fail_left=0):
        self.calls = []
        self.one = one
        self.many = many if many is not None else []
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

    def test_dsn_and_timeout_passed_to_connector(self):
        conn = FakeConn()
        st, issued = build([conn], timeout=13)
        self.assertEqual(issued, [(DSN, 13)])
        st.close()


class TestPgStorageSettings(unittest.TestCase):
    def test_get_all_settings_drops_nulls(self):
        conn = FakeConn(many=[("takeover_minutes", "10"), ("persona", "будь краток"),
                              ("broken", None)])
        st, _ = build([conn])
        self.assertEqual(st.get_all_settings(),
                         {"takeover_minutes": "10", "persona": "будь краток"})
        st.close()

    def test_get_setting_returns_default_when_missing(self):
        conn = FakeConn(one=None)
        st, _ = build([conn])
        self.assertEqual(st.get_setting("persona", "дефолт"), "дефолт")
        st.close()

    def test_get_setting_returns_value(self):
        conn = FakeConn(one=("10",))
        st, _ = build([conn])
        self.assertEqual(st.get_setting("takeover_minutes"), "10")
        st.close()

    def test_set_settings_many_is_atomic(self):
        conn = FakeConn()
        st, _ = build([conn])
        st.set_settings_many({"takeover_minutes": "10", "enabled": "1"})
        upserts = [c for c in conn.calls if c[0] == "executemany"]
        self.assertEqual(len(upserts), 1)
        _, sql, rows = upserts[0]
        self.assertIn("ON CONFLICT (key) DO UPDATE", sql)
        self.assertEqual(rows, [("takeover_minutes", "10"), ("enabled", "1")])
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
        second = FakeConn(many=[("takeover_minutes", "10")])
        st, issued = build([first, second])
        self.assertEqual(st.get_all_settings(), {"takeover_minutes": "10"})
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
