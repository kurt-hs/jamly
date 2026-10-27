"""Unit tests for queue.py (persistent, resumable input queue). No network/GPU."""
import sys
import os
import tempfile
import unittest


# src-layout: make the ``jamly`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from jamly.watch_queue import WatchQueue, default_queue_path


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


class AccessorsTests(unittest.TestCase):
    def _tmp(self):
        return tempfile.mkdtemp(prefix="acq_")

    def test_done_ids_tracks_completed_and_survives_reload(self):
        d = self._tmp()
        q = WatchQueue(path=os.path.join(d, "queue.json"))
        for wid in ("a", "b", "c"):  # items are stored PENDING by default
            self.assertTrue(q.add(wid))
        self.assertEqual(q.done_ids(), [])   # nothing marked done yet

        q.mark_done("a")
        q.mark_done("c")
        self.assertEqual(q.done_ids(), ["a", "c"])  # _done is authoritative, order-preserving
        self.assertEqual(len(q.items), 3)            # .items exposes all raw records (not just pending)

        # persist before reload so the done set is written to disk (add/mark_done do not auto-save);
        # from_path() reads "done" back and rebuilds _done, which must match the in-memory value.
        q.save()
        reloaded = WatchQueue.from_path(q.path)
        self.assertEqual(reloaded.done_ids(), ["a", "c"])
        self.assertIn("b", reloaded.pending_ids())

    def test_items_exposes_raw_records_with_expected_keys(self):
        d = self._tmp()
        q = WatchQueue(path=os.path.join(d, "queue.json"))
        q.add("vid123")
        recs = {i["watch_id"]: i for i in q.items}
        self.assertEqual(len(recs), 1)
        self.assertIn("title", recs["vid123"])
        self.assertIn("status", recs["vid123"])
        # mark_done flips the status on the same raw record without duplicating it
        q.mark_done("vid123")
        self.assertEqual(len(q.items), 1)
        self.assertEqual(q.items[0]["status"], WatchQueue.DONE)
        self.assertIn("vid123", q.done_ids())


if __name__ == "__main__":
    unittest.main(verbosity=2)
