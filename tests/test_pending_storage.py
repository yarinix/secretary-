"""Тесты отложенных пачек на SQLite :memory: (debounce, захват, отмена, очистка)."""

import unittest
from concurrent.futures import ThreadPoolExecutor

from storage import Storage

NOW = 1000.0


def add(db, *, chat_id=1, part="Иван: привет", message_id=11, due_at=NOW + 600,
        now=NOW, mode="wait", conn_id="cid", owner_chat_id=99, file_id=None,
        file_mime=None):
    return db.upsert_pending(
        chat_id=chat_id, part=part, message_id=message_id, due_at=due_at,
        now=now, mode=mode, conn_id=conn_id, owner_chat_id=owner_chat_id,
        file_id=file_id, file_mime=file_mime,
    )


class TestPendingSchema(unittest.TestCase):
    def setUp(self):
        self.db = Storage(":memory:")

    def test_tables_and_index_created(self):
        names = {r[0] for r in self.db.db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()}
        self.assertIn("secretary_pending", names)
        self.assertIn("secretary_meta", names)
        self.assertIn("secretary_pending_due_idx", names)

    def test_partial_index_excludes_claimed(self):
        sql = self.db.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='secretary_pending_due_idx'"
        ).fetchone()[0]
        self.assertIn("claimed_at IS NULL", sql)


class TestUpsertPending(unittest.TestCase):
    def setUp(self):
        self.db = Storage(":memory:")

    def test_insert_shape(self):
        row = add(self.db)
        self.assertEqual(row["parts"], ["Иван: привет"])
        self.assertEqual(row["seen_ids"], [11])
        self.assertEqual(row["due_at"], NOW + 600)
        self.assertEqual(row["created_at"], NOW)
        self.assertEqual(row["last_at"], NOW)
        self.assertEqual(row["mode"], "wait")
        self.assertEqual(row["conn_id"], "cid")
        self.assertEqual(row["owner_chat_id"], 99)
        self.assertIsNone(row["notify_message_id"])
        self.assertIsNone(row["claimed_at"])
        self.assertIsNone(row["claim_token"])
        self.assertEqual(row["attempts"], 0)
        self.assertFalse(row["duplicate"])

    def test_debounce_shifts_deadline_and_unions_ids(self):
        add(self.db)
        row = add(self.db, part="Иван: ты тут?", message_id=12,
                  due_at=NOW + 615, now=NOW + 5, mode="away")
        self.assertEqual(row["parts"], ["Иван: привет", "Иван: ты тут?"])
        self.assertEqual(row["seen_ids"], [11, 12])
        self.assertEqual(row["due_at"], NOW + 615)
        self.assertEqual(row["last_at"], NOW + 5)
        self.assertEqual(row["created_at"], NOW)  # начало пачки не сдвигаем
        self.assertEqual(row["mode"], "away")
        self.assertFalse(row["duplicate"])

    def test_duplicate_message_id_does_not_shift(self):
        add(self.db)
        row = add(self.db, part="Иван: привет", message_id=11,
                  due_at=NOW + 99999, now=NOW + 6)
        self.assertTrue(row["duplicate"])
        self.assertEqual(row["due_at"], NOW + 600)
        self.assertEqual(row["parts"], ["Иван: привет"])

    def test_duplicate_without_message_id_still_adds(self):
        add(self.db, message_id=None)
        row = add(self.db, part="ещё", message_id=None, due_at=NOW + 620, now=NOW + 1)
        self.assertFalse(row["duplicate"])
        self.assertEqual(len(row["parts"]), 2)
        self.assertEqual(row["seen_ids"], [])

    def test_new_message_resets_claim(self):
        add(self.db, due_at=NOW + 1)
        claimed = self.db.claim_due(NOW + 2)
        self.assertEqual(claimed[0]["attempts"], 1)
        row = add(self.db, part="Иван: ещё", message_id=12,
                  due_at=NOW + 700, now=NOW + 3)
        self.assertIsNone(row["claimed_at"])
        self.assertIsNone(row["claim_token"])
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(len(row["parts"]), 2)

    def test_notify_message_id_survives_shift(self):
        add(self.db)
        self.db.set_notify_message_id(1, 555)
        row = add(self.db, part="ещё", message_id=12, due_at=NOW + 620, now=NOW + 1)
        self.assertEqual(row["notify_message_id"], 555)

    def test_media_coalesce_keeps_previous_file(self):
        add(self.db, file_id="file-1", file_mime="audio/ogg")
        row = add(self.db, part="ещё", message_id=12, due_at=NOW + 620,
                  now=NOW + 1, file_id=None, file_mime=None)
        self.assertEqual(row["file_id"], "file-1")
        self.assertEqual(row["file_mime"], "audio/ogg")

    def test_media_updated_to_last_message(self):
        add(self.db, file_id="file-1", file_mime="audio/ogg")
        row = add(self.db, part="фото", message_id=12, due_at=NOW + 620,
                  now=NOW + 1, file_id="file-2", file_mime="image/jpeg")
        self.assertEqual(row["file_id"], "file-2")
        self.assertEqual(row["file_mime"], "image/jpeg")

    def test_broken_json_does_not_break_codec(self):
        add(self.db)
        with self.db._lock, self.db.db:
            self.db.db.execute(
                "UPDATE secretary_pending SET parts='не json', seen_ids='сломано' WHERE chat_id=1"
            )
        row = self.db.get_pending(1)
        self.assertEqual(row["parts"], [])
        self.assertEqual(row["seen_ids"], [])


class TestClaimAndDelivery(unittest.TestCase):
    def setUp(self):
        self.db = Storage(":memory:")

    def test_not_due_yet(self):
        add(self.db, due_at=NOW + 600)
        self.assertEqual(self.db.claim_due(NOW + 10), [])

    def test_claim_returns_row_with_token(self):
        add(self.db, due_at=NOW + 600)
        rows = self.db.claim_due(NOW + 601)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["claim_token"])
        self.assertEqual(rows[0]["claimed_at"], NOW + 601)
        self.assertEqual(rows[0]["attempts"], 1)

    def test_claim_is_at_least_once_not_delete(self):
        add(self.db, due_at=NOW + 600)
        self.db.claim_due(NOW + 601)
        self.assertEqual(self.db.pending_stats()["pending_chats"], 0)  # claimed != NULL
        self.assertIsNotNone(self.db.get_pending(1))  # строка жива

    def test_second_claim_before_stale_is_empty(self):
        add(self.db, due_at=NOW + 600)
        self.db.claim_due(NOW + 601)
        self.assertEqual(self.db.claim_due(NOW + 602), [])

    def test_stale_claim_reclaims_and_increments(self):
        add(self.db, due_at=NOW + 1)
        self.db.claim_due(NOW + 2)
        rows = self.db.claim_due(NOW + 200, stale_after=120)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempts"], 2)

    def test_limit_is_respected(self):
        for cid in range(5):
            add(self.db, chat_id=cid, message_id=cid + 100, due_at=NOW + 1)
        rows = self.db.claim_due(NOW + 2, limit=2)
        self.assertEqual(len(rows), 2)

    def test_delete_requires_matching_token(self):
        add(self.db, due_at=NOW + 600)
        rows = self.db.claim_due(NOW + 601)
        self.assertFalse(self.db.delete_pending(1, "wrong-token"))
        self.assertTrue(self.db.delete_pending(1, rows[0]["claim_token"]))
        self.assertIsNone(self.db.get_pending(1))

    def test_delete_does_not_remove_recreated_batch(self):
        add(self.db, due_at=NOW + 600)
        rows = self.db.claim_due(NOW + 601)
        token = rows[0]["claim_token"]
        add(self.db, part="новое", message_id=12, due_at=NOW + 700, now=NOW + 602)
        self.assertFalse(self.db.delete_pending(1, token))
        self.assertIsNotNone(self.db.get_pending(1))


class TestCancelAndStats(unittest.TestCase):
    def setUp(self):
        self.db = Storage(":memory:")

    def test_cancel_returns_row_and_deletes(self):
        add(self.db)
        row = self.db.cancel_pending(1)
        self.assertEqual(row["chat_id"], 1)
        self.assertEqual(row["parts"], ["Иван: привет"])
        self.assertIsNone(self.db.get_pending(1))

    def test_cancel_missing_returns_none(self):
        self.assertIsNone(self.db.cancel_pending(999))

    def test_cancel_only_this_chat(self):
        add(self.db, chat_id=1, message_id=11)
        add(self.db, chat_id=2, message_id=21)
        self.db.cancel_pending(1)
        self.assertIsNone(self.db.get_pending(1))
        self.assertIsNotNone(self.db.get_pending(2))

    def test_stats_counts_unclaimed_and_min_due(self):
        add(self.db, chat_id=1, message_id=11, due_at=NOW + 600)
        add(self.db, chat_id=2, message_id=21, due_at=NOW + 300)
        stats = self.db.pending_stats()
        self.assertEqual(stats["pending_chats"], 2)
        self.assertEqual(stats["next_due_at"], NOW + 300)

    def test_claimed_rows_excluded_from_stats(self):
        add(self.db, due_at=NOW + 1)
        self.db.claim_due(NOW + 2)
        self.assertEqual(self.db.pending_stats(),
                         {"pending_chats": 0, "next_due_at": None})

    def test_purge_stale_removes_only_old(self):
        add(self.db, chat_id=1, message_id=11, now=NOW)
        add(self.db, chat_id=2, message_id=21, now=NOW + 80000)
        removed = self.db.purge_stale(NOW + 100000, max_age=86400)
        self.assertEqual(removed, 1)
        self.assertIsNone(self.db.get_pending(1))
        self.assertIsNotNone(self.db.get_pending(2))


class TestMeta(unittest.TestCase):
    def setUp(self):
        self.db = Storage(":memory:")

    def test_missing_meta_returns_default(self):
        self.assertIsNone(self.db.get_meta("last_owner_at"))
        self.assertEqual(self.db.get_meta("last_owner_at", "0"), "0")

    def test_meta_roundtrip_and_overwrite(self):
        self.db.set_meta("last_owner_at", "1234.5")
        self.assertEqual(self.db.get_meta("last_owner_at"), "1234.5")
        self.db.set_meta("last_owner_at", "9999.0")
        self.assertEqual(self.db.get_meta("last_owner_at"), "9999.0")

    def test_update_id_meta(self):
        self.db.set_meta("last_update_id", "42")
        self.assertEqual(self.db.get_meta("last_update_id"), "42")


class TestConcurrency(unittest.TestCase):
    def test_parallel_upserts_different_chats(self):
        db = Storage(":memory:")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(
                lambda cid: add(db, chat_id=cid, message_id=cid + 100,
                                due_at=NOW + 600 + cid),
                range(60),
            ))
        self.assertEqual(db.pending_stats()["pending_chats"], 60)
        self.assertEqual(db.pending_stats()["next_due_at"], NOW + 600)

    def test_parallel_same_chat_keeps_all_parts(self):
        db = Storage(":memory:")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(
                lambda i: add(db, part=f"Иван: сообщение {i}", message_id=100 + i,
                              due_at=NOW + 600 + i, now=NOW + i),
                range(30),
            ))
        row = db.get_pending(1)
        self.assertEqual(len(row["parts"]), 30)
        self.assertEqual(len(row["seen_ids"]), 30)


if __name__ == "__main__":
    unittest.main()
