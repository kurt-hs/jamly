"""Persistent, resumable input queue for auto_convert (progress + dedup).

Backed by a small JSON file so the CLI and the HTTP server (#16) share one protocol::

    {
      "items": [{"watch_id":"dQw4w9WgXcQ","title":null,"status":"pending",
                 "added_at":1710000000.0}],
      "done":  ["xYz..."],   # completed/failed ids (kept for history + dedup)
      "updated_at": <epoch float>
    }

WatchQueue is path-backed but also usable in-memory; persistence is isolated so it can be
unit-tested with a temp file. add() de-duplicates by watch id (order-preserving).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import List, Optional


def default_queue_path(scope_dir: str = "") -> str:
    """Default queue-file location (visible next to outputs; gitignored state file)."""
    base = scope_dir if scope_dir else "."
    return os.path.join(os.path.abspath(base), "auto_convert_queue.json")


class WatchQueue:
    """Deduplicating, resumable list of watch ids with per-item status."""

    PENDING = "pending"
    DONE = "done"

    def __init__(self, path: str = "", items: Optional[list] = None, done: Optional[list] = None):
        self.path = os.path.abspath(path) if path else ""
        self._items: List[dict] = list(items) if items else []
        self._done: List[str] = list(done) if done else []

    # -- construction -------------------------------------------------------- #
    @classmethod
    def from_path(cls, path: str) -> "WatchQueue":
        """Load a queue file (empty if missing/invalid)."""
        q = cls(path=path)
        if not os.path.isfile(q.path):
            return q
        try:
            with open(q.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return q
        items = [i for i in data.get("items", []) if isinstance(i, dict)]
        # de-dup on load, order-preserving
        seen = set()
        uniq = []
        for i in items:
            wid = str(i.get("watch_id") or "").strip()
            if not wid or wid in seen:
                continue
            seen.add(wid)
            uniq.append({"watch_id": wid, "title": i.get("title"), "status": q.PENDING})
        done = [str(d).strip() for d in data.get("done", []) if str(d).strip()]
        return cls(path=q.path, items=uniq, done=done)

    @classmethod
    def create(cls, path: str) -> "WatchQueue":
        """Return an empty queue bound to `path` (no file written until .save())."""
        return cls(path=path)

    # -- mutation ------------------------------------------------------------- #
    def add(self, watch_id: str, title: Optional[str] = None) -> bool:
        """Add a watch id if not already known; returns True iff it was newly added."""
        wid = (watch_id or "").strip()
        if not wid or wid in self._done or any(i["watch_id"] == wid for i in self._items):
            return False
        idx = len(self._items)
        self._items.append({"watch_id": wid, "title": title, "status": self.PENDING})
        return True

    def add_all(self, watch_ids: List[str], titles: Optional[List[Optional[str]]] = None) -> int:
        """Bulk-add; returns count newly added."""
        if not titles:
            titles = [None] * len(watch_ids)
        n = 0
        for wid, tit in zip(watch_ids, titles):
            if self.add(wid, tit):
                n += 1
        return n

    def remove(self, watch_id: str) -> bool:
        wid = (watch_id or "").strip()
        before = len(self._items)
        self._items = [i for i in self._items if i["watch_id"] != wid]
        return len(self._items) != before

    def is_complete(self, watch_id: str) -> bool:
        """True once an item has finished (success or failure)."""
        wid = (watch_id or "").strip()
        return wid in self._done

    def mark_done(self, watch_id: str) -> None:
        """Record that an item is complete; moves it to _done and stamps its status."""
        wid = (watch_id or "").strip()
        if wid not in self._done and any(i["watch_id"] == wid for i in self._items):
            self._done.append(wid)
            for i in self._items:
                if i["watch_id"] == wid and i["status"] == self.PENDING:
                    i["status"] = self.DONE

    def done_ids(self) -> List[str]:
        """Watch ids recorded complete (authoritative on _done; survives reloads)."""
        return [wid for wid in self._done]

# -- queries -------------------------------------------------------------- #
    def pending_ids(self) -> List[str]:
        # _done is authoritative (survives reloads even if a per-item status field is stale)
        return [i["watch_id"] for i in self._items if i["watch_id"] not in self._done]

    def count_pending(self) -> int:
        # authoritative on _done (matches pending_ids; robust across reloads)
        return sum(1 for i in self._items if i["watch_id"] not in self._done)

    @property
    def items(self):
        """Raw item records as stored (each: watch_id, title, status)."""
        return self._items

    def remaining_work(self) -> int:
        """True when nothing is left to do."""
        return not self._items or all(i["watch_id"] in self._done for i in self._items)

    # -- persistence ---------------------------------------------------------- #
    def save(self) -> None:
        """Atomically write the queue file (write-temp + rename)."""
        data = {
            "items": [{"watch_id": i["watch_id"], "title": i.get("title")} for i in self._items],
            "done": self._done,
            "updated_at": time.time(),
        }
        if self.path:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # unique temp name so two concurrent writers don't collide on the same .tmp before replace.
            tmp = "%s.tmp.%d.%s" % (self.path, os.getpid(), uuid.uuid4().hex[:8])
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def __len__(self) -> int:
        return len(self._items)


if __name__ == "__main__":
    q = WatchQueue.create(default_queue_path())
    print("queue path:", q.path)
    added = 0
    for wid in ("dQw4w9WgXcQ", "https://youtu.be/abcDEF12345", "aBcDeFg1234"):
        if q.add(wid):
            added += 1
    print("newly added:", added, "-> pending count:", q.count_pending())
    assert not q.add("dQw4w9WgXcQ"), "dup should be rejected"
    q.save()
    reloaded = WatchQueue.from_path(q.path)
    assert [i["watch_id"] for i in reloaded.pending_ids()] == \
        ["dQw4w9WgXcQ", "abcDEF12345", "aBcDeFg1234"], reloaded.pending_ids()
    print("dedup + reload OK:", [i["watch_id"] for i in reloaded.pending_ids()])
