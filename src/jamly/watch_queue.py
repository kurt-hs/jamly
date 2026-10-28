"""Durable job state, explicit retries, and exclusive queue ownership."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from filelock import FileLock, Timeout

from .config import state_directory


class QueueError(RuntimeError):
    pass


def default_queue_path(scope_dir: str = "") -> str:
    return str(state_directory(scope_dir or None) / "jamly_queue.json")


@contextmanager
def queue_owner(path: str):
    Path(path).absolute().parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(Path(path).absolute()) + ".owner.lock")
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise QueueError(
            "This queue is already owned by another Jamly process. "
            "Submit through the browser/API or stop that process before using the CLI."
        ) from exc
    try:
        yield
    finally:
        lock.release()


class WatchQueue:
    PENDING = "pending"
    DONE = "done"
    STATUSES = {"pending", "running", "done", "skipped", "error", "cancelled"}

    def __init__(self, path: str = "", items=None, done=None):
        self.path = str(Path(path).absolute()) if path else ""
        self._items = copy.deepcopy(items or [])
        self._revision = 0
        self._save_lock = threading.Lock()
        for wid in done or []:
            if not any(item["watch_id"] == wid for item in self._items):
                self.add(wid)
            self.mark_done(wid)

    @classmethod
    def from_path(cls, path: str) -> "WatchQueue":
        queue = cls(path)
        if not Path(path).exists():
            return queue
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise ValueError("expected an object with an items list")
            done = data.get("done", [])
            if not isinstance(done, list):
                raise ValueError("done must be a list")
            queue._revision = int(data.get("revision", 0))
            seen = set()
            for record in data["items"]:
                if not isinstance(record, dict):
                    raise ValueError("each job must be an object")
                wid = record.get("watch_id")
                if not isinstance(wid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", wid):
                    raise ValueError("job has an invalid stable id")
                if wid in seen:
                    continue
                seen.add(wid)
                status = record.get("status", "done" if wid in done else "pending")
                if status not in cls.STATUSES:
                    raise ValueError("unknown job status: %r" % status)
                # Only a job interrupted while running is automatically resumed.
                if status == "running":
                    status = "pending"
                source = record.get("source_url") or wid
                if not isinstance(source, str):
                    raise ValueError("source_url must be a string")
                options = record.get("options", {})
                if not isinstance(options, dict):
                    raise ValueError("job options must be an object")
                queue._items.append(dict(
                    record, watch_id=wid, source_url=source, status=status,
                    options=options, title=record.get("title"),
                ))
        except (OSError, ValueError, TypeError) as exc:
            raise QueueError("Cannot load queue %s: %s" % (path, exc)) from exc
        return queue

    @classmethod
    def create(cls, path: str) -> "WatchQueue":
        return cls(path)

    def add(self, watch_id: str, title=None, source_url=None, *, retry=False, options=None) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", watch_id):
            raise ValueError("invalid stable id")
        previous = next((item for item in self._items if item["watch_id"] == watch_id), None)
        if previous is not None:
            if previous["status"] in ("pending", "running") or not retry:
                return False
            previous.update(status="pending", error=None, outputs={}, generation=None,
                            source_url=source_url or previous["source_url"],
                            options=copy.deepcopy(options if options is not None else previous["options"]),
                            updated_at=time.time())
            if title:
                previous["title"] = title
            return True
        self._items.append({
            "watch_id": watch_id, "source_url": source_url or watch_id, "title": title,
            "status": "pending", "error": None, "outputs": {}, "options": copy.deepcopy(options or {}),
            "attempts": 0, "updated_at": time.time(),
        })
        return True

    def add_all(self, watch_ids, titles=None, source_urls=None, *, retry=False, options=None) -> int:
        titles, source_urls = list(titles or []), list(source_urls or [])
        return sum(self.add(
            wid, titles[index] if index < len(titles) else None,
            source_urls[index] if index < len(source_urls) else None,
            retry=retry, options=options,
        ) for index, wid in enumerate(watch_ids))

    def get(self, watch_id: str) -> dict | None:
        return next((copy.deepcopy(item) for item in self._items if item["watch_id"] == watch_id), None)

    def copy(self) -> "WatchQueue":
        result = WatchQueue(self.path, items=self.items)
        result._revision = self._revision
        return result

    def remove(self, watch_id: str) -> bool:
        before = len(self._items)
        self._items = [item for item in self._items if item["watch_id"] != watch_id]
        return len(self._items) != before

    def mark_running(self, watch_id: str) -> None:
        self.finish(watch_id, "running")
        for item in self._items:
            if item["watch_id"] == watch_id:
                item["attempts"] = item.get("attempts", 0) + 1

    def finish(self, watch_id: str, status: str, *, error=None, outputs=None,
               title=None, generation=None) -> None:
        if status not in self.STATUSES:
            raise ValueError("unknown job status")
        for item in self._items:
            if item["watch_id"] == watch_id:
                item.update(status=status, error=error, updated_at=time.time())
                if outputs is not None:
                    item["outputs"] = dict(outputs)
                if title:
                    item["title"] = title
                if generation is not None:
                    item["generation"] = generation
                return
        raise QueueError("Unknown job: %s" % watch_id)

    def mark_done(self, watch_id: str) -> None:
        self.finish(watch_id, "done")

    def is_complete(self, watch_id: str) -> bool:
        return watch_id in self.done_ids()

    def done_ids(self) -> list[str]:
        return [item["watch_id"] for item in self._items if item["status"] in ("done", "skipped")]

    def pending_ids(self) -> list[str]:
        return [item["watch_id"] for item in self.pending_items()]

    def pending_items(self) -> list[dict]:
        return [copy.deepcopy(item) for item in self._items if item["status"] == "pending"]

    def count_pending(self) -> int:
        return len(self.pending_ids())

    @property
    def items(self) -> list[dict]:
        return copy.deepcopy(self._items)

    def save(self) -> None:
        if not self.path:
            return
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._save_lock, FileLock(self.path + ".write.lock", timeout=5):
            try:
                current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                if not isinstance(current, dict):
                    raise ValueError("queue must be an object")
            except ValueError as exc:
                raise QueueError("Queue changed to an invalid document; refusing to overwrite it") from exc
            if current.get("revision", 0) != self._revision:
                raise QueueError("Queue changed in another process; refusing to overwrite newer jobs")
            next_revision = self._revision + 1
            data = {"items": self.items, "done": self.done_ids(), "revision": next_revision,
                    "updated_at": time.time()}
            temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
            try:
                with temporary.open("w", encoding="utf-8") as file:
                    json.dump(data, file, ensure_ascii=False, indent=2)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, path)
                self._revision = next_revision
            finally:
                temporary.unlink(missing_ok=True)

    def __len__(self) -> int:
        return len(self._items)
