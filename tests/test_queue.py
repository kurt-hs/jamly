"""Unit tests for queue.py (persistent, resumable input queue). No network/GPU."""
import sys
import json
import os
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.abspath(__file__))   # project root (holds queue.py)
sys.path.insert(0, _ROOT)                            # ensure `import queue` resolves
from queue import WatchQueue, default_queue_path


class DedupTests(unittest.TestCase):
    def test_add_dedupes_by_watch_id_order_preserved(self):
        # WatchQueue stores already-normalized ids (gather_inputs normalizes URL->id upstream);
        # so dedup is by exact id, and insertion order is preserved.
        q = WatchQueue()
        self.assertTrue(q.add("aBcDeFg1234"))
        self.assertFalse(q.add("aBcDeFg1234"), "exact duplicate rejected")
        self.assertTrue(q.add("zYxWvUt5678"))
        self.assertEqual(q.pending_ids(), ["aBcDeFg1234", "zYxWvUt5678"])

    def test_add_all_returns_newly_added_count(self):
        q = WatchQueue()
        ids = ["a1", "b2", "a1", "c3"]
        self.assertEqual(q.add_all(ids), 3)   # a1 counted once
        self.assertEqual(len(q), 3)

    def test_remove_and_is_complete(self):
        q = WatchQueue()
        q.add("x")
        self.assertTrue(q.remove("x"))
        self.assertFalse(q.remove("x"))
        self.assertFalse(q.is_complete("x"))


class PersistenceTests(unittest.TestCase):
    def _tmp(self):
        return tempfile.mkdtemp(prefix="acq_")

    def test_save_reload_roundtrip_with_done(self):
        d = self._tmp()
        q = WatchQueue(path=os.path.join(d, "queue.json"))
        # ids are already normalized upstream; queue dedups by exact id only
        for wid in ("a", "b", "c"):
            self.assertTrue(q.add(wid))
        q.mark_done("b")          # simulate completion of an item mid-run
        q.save()
        reloaded = WatchQueue.from_path(q.path)
        self.assertEqual(reloaded.pending_ids(), ["a", "c"])
        self.assertTrue(reloaded.is_complete("b"))
        # newly-added ids are NOT marked done (resumable: they remain pending on reload)
        q.add("d")
        q.save()
        again = WatchQueue.from_path(q.path)
        self.assertIn("d", again.pending_ids())
        self.assertTrue(reloaded.is_complete("b"))

    def test_init_accepts_done_list(self):
        q = WatchQueue(done=["done_id"])
        self.assertTrue(q.is_complete("done_id"))
        self.assertEqual(q.count_pending(), 0)

    def test_from_missing_file_is_empty(self):
        q = WatchQueue.from_path(os.path.join(self._tmp(), "does_not_exist.json"))
        self.assertEqual(len(q), 0)

    def test_invalid_json_ignored(self):
        path = os.path.join(self._tmp(), "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        q = WatchQueue.from_path(path)   # must not raise
        self.assertEqual(len(q), 0)

    def test_default_queue_path_is_in_scope(self):
        d = tempfile.mkdtemp(prefix="acscope_")
        p = default_queue_path(d)
        self.assertTrue(p.startswith(os.path.abspath(d)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
