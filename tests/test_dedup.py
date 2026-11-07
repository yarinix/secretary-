"""Тесты дедупликации update_id (защита от повторной доставки вебхука)."""

import threading
import unittest

from dedup import DedupGuard


class TestDedupGuard(unittest.TestCase):
    def test_first_call_is_not_duplicate(self):
        g = DedupGuard()
        self.assertFalse(g.is_duplicate(100, now=0.0))

    def test_second_call_is_duplicate(self):
        g = DedupGuard()
        self.assertFalse(g.is_duplicate(100, now=0.0))
        self.assertTrue(g.is_duplicate(100, now=1.0))
        self.assertTrue(g.is_duplicate(100, now=2.0))

    def test_distinct_ids_are_independent(self):
        g = DedupGuard()
        for i in range(20):
            self.assertFalse(g.is_duplicate(i, now=float(i)))
        self.assertEqual(len(g), 20)
        for i in range(20):
            self.assertTrue(g.is_duplicate(i, now=100.0))

    def test_string_id_normalized_to_int(self):
        g = DedupGuard()
        self.assertFalse(g.is_duplicate("42", now=0.0))
        self.assertTrue(g.is_duplicate(42, now=1.0))

    def test_ttl_expiry_allows_again(self):
        g = DedupGuard(ttl=10.0)
        self.assertFalse(g.is_duplicate(1, now=0.0))
        self.assertTrue(g.is_duplicate(1, now=9.999))
        self.assertFalse(g.is_duplicate(1, now=11.0))
        self.assertTrue(g.is_duplicate(1, now=12.0))

    def test_invalid_ids_never_blocked_and_not_stored(self):
        g = DedupGuard()
        bad_values = (None, "abc", {}, [], object(), float("nan"), float("inf"))
        for bad in bad_values:
            with self.subTest(bad=repr(bad)):
                self.assertFalse(g.is_duplicate(bad, now=1.0))
        self.assertEqual(len(g), 0)

    def test_max_size_evicts_oldest(self):
        g = DedupGuard(ttl=1000.0, max_size=5)
        for i in range(5):
            g.is_duplicate(i, now=float(i))
        self.assertEqual(len(g), 5)
        self.assertFalse(g.is_duplicate(99, now=10.0))
        self.assertLessEqual(len(g), 5)
        self.assertTrue(g.is_duplicate(99, now=11.0))
        # самый старый (id=0) мог быть вытеснен — это допустимо
        self.assertIn(len(g), (4, 5))

    def test_concurrent_same_id_processed_once(self):
        g = DedupGuard()
        results = []
        barrier = threading.Barrier(8)
        lock = threading.Lock()

        def worker():
            barrier.wait()
            outcome = g.is_duplicate(777, now=1.0)
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 8)
        self.assertEqual(results.count(False), 1)
