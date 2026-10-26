"""Persistent discovery config for splitfy (reuse-first provisioning, DESIGN.md §11).

Backed by a small JSON file at the repo/scope root so run.py / bootstrap.py / provision.py can
record what was found on one host and reuse it next time -- without rescanning PATH and app roots
every launch -- and report which tool came from an existing install vs. freshly downloaded into
``.portable/``. Schema::

    {
      "version": 1,
      "tools": {
        "ffmpeg": {"paths": ["/abs/dir-with-both"], "preferred": true},
        "yt-dlp": {"paths": ["/abs/yt-dlp.exe"]}
      },
      "overrides": {...}   # user-specified tool flags and override keys
    }

A missing or invalid file -> empty :class:`ConfigStore` (no crash); callers treat that as
"nothing recorded yet". Persistence is isolated so the store can be unit-tested with a temp file.
"""
from __future__ import annotations

import json
import os
import uuid


def _abs(path: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


DEFAULT_VERSION = 1
_TOOLS = ("ffmpeg", "yt-dlp")


def default_config_path(scope_dir: str = "") -> str:
    """Default config-file location (repo/scope root; gitignored state file)."""
    base = scope_dir if scope_dir else "."
    return os.path.join(_abs(base), "splitfy.json")


class ConfigStore:
    """In-memory view over ``splitfy.json`` with atomic persistence.

    Like :mod:`watch_queue`, a missing/invalid file loads as empty so discovery never fails on a
    first run or when the file is hand-corrupted. Records are keyed by tool category; each entry
    holds the discovered paths plus any extra fields (e.g. ``preferred``) written alongside them.
    """

    def __init__(self, path: str = "", data=None):
        self.path = _abs(path) if path else ""
        merged = {"version": DEFAULT_VERSION, "tools": {}, "overrides": {}}
        if isinstance(data, dict):
            for k in ("version", "tools", "overrides"):
                val = data.get(k)
                if (k == "version" and isinstance(val, int)) or isinstance(val, (dict, list)):
                    merged[k] = val
        self._data = merged

    # -- construction -------------------------------------------------------- #
    @classmethod
    def create(cls, path: str = "") -> "ConfigStore":
        """Return an empty store bound to `path` (no file written until .save())."""
        return cls(path=path)

    @classmethod
    def from_path(cls, path: str = "") -> "ConfigStore":
        """Load the config at `path` (defaults to the scope root); empty if missing/invalid."""
        store = cls.create(path or default_config_path())
        if not os.path.isfile(store.path):
            return store
        try:
            with open(store.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return store
        good = {}
        if isinstance(data, dict):
            for k in ("version", "tools", "overrides"):
                val = data.get(k)
                if (k == "version" and isinstance(val, int)) or isinstance(val, (dict, list)):
                    good[k] = val
        return cls(path=store.path, data=good)

    # -- queries -------------------------------------------------------------- #
    @property
    def empty(self) -> bool:
        """True when nothing has been recorded yet."""
        tools = self._data.get("tools", {})
        return not any(t in tools for t in _TOOLS)

    def has(self, tool: str) -> bool:
        return tool in self._data.get("tools", {})

    def get_paths(self, tool: str):
        """Return the recorded paths for `tool` (empty list if unknown/unrecorded)."""
        entry = self._data.get("tools", {}).get(tool, {}) or {}
        return list(entry.get("paths", [])) if isinstance(entry, dict) else []

    def get_field(self, tool: str, key: str, default=None):
        entry = self._data.get("tools", {}).get(tool, {}) or {}
        return entry.get(key, default) if isinstance(entry, dict) else default

    # -- mutation ------------------------------------------------------------- #
    def record(self, tool: str, paths=None, **fields) -> dict:
        """Record discovered `paths` for a tool category plus extra fields; returns the entry."""
        tools = self._data.setdefault("tools", {})
        if not isinstance(tools, dict):
            tools = {}
            self._data["tools"] = tools
        entry = {"paths": list(paths) if paths else [], **fields}
        tools[tool] = entry
        return entry

    def set_override(self, key: str, value) -> None:
        """Record a user-chosen override key/value for this host."""
        self._data.setdefault("overrides", {})[key] = value

    # -- persistence ---------------------------------------------------------- #
    def save(self) -> None:
        """Atomically write the config file (write-temp + rename). No-op without a path."""
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # unique temp name so two concurrent writers don't collide on the same .tmp before replace.
        tmp = "%s.tmp.%d.%s" % (self.path, os.getpid(), uuid.uuid4().hex[:8])
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


if __name__ == "__main__":
    store = ConfigStore.create(default_config_path())
    print("config path:", store.path)
    # portable demo values only — no hard-coded username/device paths
    store.record("ffmpeg", ["<user>/.portable/win64/ffmpeg"], preferred=True)
    store.set_override("ffmpeg.path", "<user>/.portable/win64/ffmpeg")
    store.save()
    reloaded = ConfigStore.from_path(store.path)
    assert not reloaded.empty and reloaded.get_paths("ffmpeg") == ["<user>/.portable/win64/ffmpeg"]
    print("record + reload OK:", reloaded._data["tools"]["ffmpeg"])