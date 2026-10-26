"""Terse, friendly, swappable run-time logging for splitfy.

Every user-facing line goes through :class:`Logger` so the wording/colour/layout lives in one
place and can be swapped or captured by tests (``logger.stream = io.StringIO()``). The default is
quiet-friendly: compact one-liners with a leading symbol, optional ANSI colour only on an interactive
terminal. Long-running steps use a small spinner that redraws; download progress is streamed live from
the subprocess so ``[download] 95% ...`` shows in real time.

Pure standard library; no third-party imports so it runs anywhere Python 3 does.
"""
from __future__ import annotations

import os
import re
import sys
import time
from typing import Callable, Dict, Optional


# --------------------------------------------------------------------------- #
# symbols + colour helpers (unicode renders on modern Windows/macOS/Linux consoles)
# --------------------------------------------------------------------------- #
SYMBOL = {"start": "\u25b6", "ok": "\u2713", "fail": "\u2717",
          "warn": "\u26a0", "info": "\u2022", "done": "\u2014"}

_ANSI = {
    "bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
    "blue": "34", "cyan": "36", "grey": "90", "reset": "0",
}


def _auto_color(stream=None) -> bool:
    """True when colour should be emitted (interactive terminal or explicitly forced)."""
    if os.environ.get("AUTOCONVERT_COLOR") == "1":
        return True
    try:
        s = stream or sys.stdout
    except Exception:
        return False
    return bool(getattr(s, "isatty", lambda: False)())


class Logger:
    """Emits a compact, friendly run log.

    Parameters: ``quiet`` suppresses all output (useful for machine pipes); ``color`` auto-detects an
    interactive terminal unless explicitly set; ``stream`` routes output to a file-like object instead
    of stdout -- handy for tests and redirecting. Every public method is pure string formatting plus one
    write, so they are trivially unit-testable by capturing the stream.
    """

    def __init__(self, quiet: bool = False, color: Optional[bool] = None) -> None:
        self.quiet = quiet
        self.color_enabled = bool(color if color is not None else _auto_color())
        self.stream: Optional[object] = None   # set by tests / callers to capture or redirect

    # -- low-level rendering ------------------------------------------------- #
    def _write(self, text: str) -> None:
        """Write `text` to the active stream (captured/redirected stream if set).

        Resolves ``sys.stderr`` at call time so tests can redirect it; failures writing to a closed/
        non-writable stream are swallowed so a broken pipe never aborts the run.
        """
        out = self.stream or sys.stderr
        try:
            out.write(text + "\n")
            out.flush()
        except (ValueError, OSError):
            pass  # e.g. closed pipe -- ignore

    def _emit(self, text: str) -> None:
        if self.quiet:
            return
        self._write(text)

    def _c(self, text: str, color_name: Optional[str]) -> str:
        if not color_name or not self.color_enabled:
            return text
        return "\x1b[%sm%s\x1b[0m" % (_ANSI[color_name], text)

    # -- public steps -------------------------------------------------------- #
    def banner(self, title: str = "splitfy") -> None:
        """A header rule + title for a fresh run."""
        self._emit("─" * 46)
        self._emit(self._c(title, "bold"))
        self._emit("─" * 46)

    def step(self, label: str) -> None:
        """Announce the start of a pipeline stage (►)."""
        self._emit("%s %s" % (SYMBOL["start"], self._c(label, "cyan")))

    def ok(self, msg: str = "") -> None:
        self._emit("%s %s" % (SYMBOL["ok"], self._c(msg or "", "green")))

    def fail(self, msg: str) -> None:
        self._emit("%s %s" % (SYMBOL["fail"], self._c(msg, "red")))

    def warn(self, msg: str) -> None:
        self._emit("%s %s" % (SYMBOL["warn"], self._c(msg, "yellow")))

    def note(self, msg: str) -> None:
        """A plain informational line (input echo, tool report, links)."""
        self._emit(self._c(msg, "grey"))

    def tool(self, name: str, version: Optional[str], kind: str = "", loc: str = "") -> None:
        """Friendly one-liner for a detected tool: ``yt-dlp 2024.7.9 [portable] AppData\\...``."""
        head = "%s %s%s" % (SYMBOL["ok"], self._c(name, "green"), version and (" %s" % version))
        tail = []
        if kind:
            tail.append(self._c("[%s]" % kind, "cyan"))
        if loc:
            tail.append(loc)
        rest = " ".join(tail).strip()
        self._emit(head + (("  " + rest) if rest else ""))

    def link(self, path: str, rel_path: Optional[str] = None) -> None:
        """Print an output file path as a clickable Windows link; plain elsewhere."""
        target = rel_path or path
        if os.path.isabs(target):
            display = os.path.relpath(target, os.getcwd()) or target
        else:
            display = target
        if sys.platform.startswith("win") and not self.quiet:
            open_hint = "click here to open" + ("  [%s]" % display) if display != target else ""
            self._emit("%s\n   %s%s" % (display, self._c(target, "cyan"), open_hint))
        else:
            self._emit(display)

    def summary(self, done: int, total: int, ok: int, err: int = 0) -> None:
        """Final per-run tally."""
        verb = "ok" if err == 0 else ("%d failed" % err)
        self._emit("%s %d/%d complete (%s)" % (SYMBOL["done"], ok, total, verb))

    @staticmethod
    def parse_download_progress(text: Optional[str]) -> Dict[str, Optional[str]]:
        """Parse yt-dlp ``[download]`` telemetry into {pct, dl, total} (e.g. ``95% (23.4M/24.8M)``).

        Returns an empty dict when there is no usable line. Percent is a string digit-run; the
        downloaded/total byte sizes are returned verbatim so callers can format any unit.
        """
        if not text:
            return {}
        m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%\s*\(([^)]*)\)", text or "")
        if not m:
            return {}
        pct, spec = m.group(1), m.group(2).strip()
        dl = total = None
        if "/" in spec:
            a, b = spec.split("/", 1)
            if a.strip():
                dl = a.strip()
            if b.strip():
                total = b.strip()
        return {"pct": pct, "dl": dl, "total": total}

    @staticmethod
    def render_bar(pct: str, dl=None, total=None) -> str:
        """Render a compact one-line download bar from parsed yt-dlp telemetry.

        ``pct`` is the percent string (e.g. "95"); ``dl``/``total`` are byte-size strings or None.
        Returns something like ``\u2581\u2581\u2588\u2588 95% (23.4M/24.8M)``.
        """
        try:
            n = max(int(float(pct)), 0)
        except (TypeError, ValueError):
            n = 0
        bar_len = 16
        filled = round(bar_len * n / 100.0)
        head = "\u2588" * filled + "\u2581" * (bar_len - filled)
        tail = []
        if dl is not None:
            tail.append(dl)
        if total is not None and total != dl:
            tail.append("/" + total)
        size = " ".join(tail).strip()
        return "%s %d%% (%s)" % (head, n, size or pct)

    def version_of(self, exe: str, argv: tuple = ("-V",), timeout: int = 20) -> Optional[str]:
        """Query a tool binary for its version once (e.g. ``yt-dlp -V``). Returns the first line.

        Tolerant of anything that is not really a version printer: missing tool, non-zero exit,
        empty output, or timeout all yield ``None`` so callers can fall back to just the binary name.
        """
        if self.quiet:
            return None
        try:
            from .core import run as _run
            r = _run([exe] + list(argv), capture=True)
            if not r.ok or not (r.stdout or "").strip():
                return None
            line = str(r.stdout).splitlines()[0].strip()
            return line or None
        except Exception:  # noqa: BLE001 - version is a nicety, never fatal
            return None

    def spinner(self, label: str, callback: Callable[[], None]) -> bool:
        """Redraw a small spinner while ``callback`` runs; return whether it finished.

        Disabled when quiet so pipes stay clean. Returns True on success (returncode 0) unless the
        caller's callback signals otherwise -- here we simply track that it started and stopped.
        """
        if self.quiet:
            return bool(callback())
        done = False
        try:
            while not done:
                sys.stdout.write("\r%s %s   ..." % (SYMBOL["start"], label))
                sys.stdout.flush()
                time.sleep(0.1)
                done = callback()
            if not done:  # still running at the end -> leave the spinner visible for the caller
                pass
        finally:
            sys.stdout.write("\r" + " " * 40 + "\r")
            sys.stdout.flush()
        return done

    def stream(self, label: str, read_lines, on_line=None) -> None:
        """Stream subprocess output line-by-line under a spinner for live download progress.

        ``read_lines`` yields decoded text lines as they arrive; ``on_line`` (optional) is called with
        each raw line so callers can react (e.g. highlight a `[download] 95%` bar). Never raises: any
        read error just stops the stream and returns.
        """
        if self.quiet or not callable(read_lines):
            return
        try:
            for line in read_lines():
                if on_line:
                    on_line(line)
                # Route through the injected stream (captured/redirected when set) so live progress
                # is overridable and testable, not hardcoded to sys.stdout.
                self._write(self._c(line.rstrip("\n"), "blue"))
        except Exception as exc:  # noqa: BLE001 - a broken pipe must not crash the run
            self.warn("stream interrupted (%s)" % exc)
