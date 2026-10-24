"""Unit tests for the friendly run-time output layer: logging.py + run.py's display helpers.

Covers terse/friendly rendering (steps, tool reports, links), download-progress parsing/bar
rendering, version queries, and how inputs are echoed with dedup results -- all captured through a
swappable logger so no real subprocess or terminal is required."""
import io
import os
import sys
import tempfile
import unittest
import unittest.mock as mock

# src-layout: make the ``auto_convert`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
import sys as _shim_sys  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from auto_convert.logging import Logger  # noqa: E402 (public helpers + Logger)


def _capture(logger=None):
    """Return a non-quiet logger writing to an in-memory StringIO buffer."""
    log = logger or Logger(quiet=False, color=False)
    buf = io.StringIO()
    log.stream = buf
    return log, buf


# --------------------------------------------------------------------------- #
# logging.py -- rendering primitives
# --------------------------------------------------------------------------- #
class RenderTests(unittest.TestCase):
    def test_ok_fail_warn_step_note_emit(self):
        log, buf = _capture()
        log.ok("done"); log.fail("boom"); log.warn("careful")
        log.step("download"); log.note("hello world")
        out = buf.getvalue()
        self.assertIn("\u2713 done", out)    # checkmark ok
        self.assertIn("\u2717 boom", out)    # cross fail
        self.assertIn("\u26a0 careful", out)  # warn triangle
        self.assertIn("download", out) and self.assertTrue(out.startswith("\u25b6"))  # step arrow

    def test_quiet_suppresses_everything(self):
        import io
        log = Logger(quiet=True, color=False)
        buf = io.StringIO(); log.stream = buf
        for fn in (lambda: log.step("x"), lambda: log.ok("y"), lambda: log.note("z")):
            fn()
        self.assertEqual(buf.getvalue(), "")

    def test_color_enabled_controls_ansi(self):
        log_on = Logger(quiet=False, color=True)
        log_off = Logger(quiet=False, color=False)
        self.assertIn("\x1b[32m", log_on._c("hi", "green"))     # ANSI present when enabled
        self.assertNotIn("\x1b[", log_off._c("hi", "green"))    # plain text when disabled

    def test_link_windows_open_hint(self):
        import sys as _s
        log, buf = _capture()
        old = _s.platform
        _s.platform = "win32"
        try:
            log.link("C:\\x\\a_drums.wav")
            out = buf.getvalue()
        finally:
            _s.platform = old
        self.assertIn("a_drums.wav", out) and self.assertIn("click here to open", out)


class ProgressBarTests(unittest.TestCase):
    def test_parse_full_line(self):
        prog = Logger.parse_download_progress("[download]   95% (23.4M/24.8M)     7.1MB/s eta 01:02")
        self.assertEqual(prog["pct"], "95")
        self.assertEqual(prog["dl"], "23.4M") and self.assertEqual(prog["total"], "24.8M")

    def test_parse_no_line(self):
        self.assertEqual(Logger.parse_download_progress(None), {})
        self.assertEqual(Logger.parse_download_progress("[download] starting"), {})

    def test_render_bar_width_and_size(self):
        import io
        log, buf = _capture()
        bar = Logger.render_bar("95", "23.4M", "24.8M")
        blocks = sum(c == "\u2588" for c in bar)     # filled cells == round(16*95/100)==15
        empty = sum(c == "\u2581" for c in bar)      # empty cells fill the rest to a 16-wide run
        self.assertEqual((blocks, empty), (15, 1))
        self.assertIn("95%", bar) and self.assertIn("23.4M", bar) and self.assertIn("24.8M", bar)

    def test_render_bar_no_size(self):
        import io
        log, buf = _capture()
        bar = Logger.render_bar("12")
        blocks = sum(c == "\u2588" for c in bar)     # filled cells == round(16*12/100)==2
        empty = sum(c == "\u2581" for c in bar)
        self.assertEqual((blocks, empty), (2, 14))
        self.assertIn("12%", bar)


class VersionOfTests(unittest.TestCase):
    def test_quiet_returns_none_without_querying(self):
        log, buf = _capture()
        old = sys.stderr
        captured = mock.MagicMock(); sys.stderr = captured   # would raise if a subprocess was spawned
        try:
            self.assertIsNone(log.version_of("whatever.exe"))  # quiet -> no subprocess
        finally:
            sys.stderr = old

    def test_returns_first_stdout_line_when_available(self):
        log, buf = _capture()
        with mock.patch("auto_convert.core.run", return_value=mock.MagicMock(ok=True, stdout="2024.7.9\n")):
            self.assertEqual(log.version_of("yt-dlp.exe", ("-V",)), "2024.7.9")

    def test_missing_tool_returns_none(self):
        log, buf = _capture()
        with mock.patch("auto_convert.core.run", return_value=mock.MagicMock(ok=False)):
            self.assertIsNone(log.version_of("nope.exe"))


# --------------------------------------------------------------------------- #
# run.py -- input echo + tool report + output links (via swappable module logger)
# --------------------------------------------------------------------------- #
class EchoInputTests(unittest.TestCase):
    def test_dedup_and_flags(self):
        import auto_convert.run as R

        log, buf = _capture()
        with mock.patch.object(R, "_log", log):
            R.echo_inputs(
                ["https://youtu.be/aBcDeFg1234", "aBcDeFg1234", "", "-", "dup"],
                ["aBcDeFg1234", "aBcDeFg1234", "", "", "aBcDeFg1234"],
            )
        out = buf.getvalue()
        self.assertIn("5 raw input(s) -> 2 watch id(s)", out)   # dedup summary line
        self.assertIn("-> aBcDeFg1234", out)                    # kept ids shown
        self.assertIn("(blank)", out) and self.assertIn("(invalid)", out)


class DescribeToolsTests(unittest.TestCase):
    def _fake_ytdlp_path(self):
        with tempfile.TemporaryDirectory() as scope:
            portable = os.path.join(scope, ".portable", "win64")
            os.makedirs(portable)
            ytdlp = os.path.join(portable, "yt-dlp.exe")
            open(ytdlp, "w").close()   # touch so tool_source sees a real file
            return ytdlp

    def test_reports_kind_version_and_location(self):
        import auto_convert.run as R

        log, buf = _capture()
        with mock.patch.object(R, "_log", log), \
                mock.patch("auto_convert.core.run", return_value=mock.MagicMock(ok=True, stdout="2024.9.1\n")):
            ytdlp = self._fake_ytdlp_path()
            R.describe_tools(ytdlp, r"C:\Program Files\Shotcut")
        out = buf.getvalue()
        self.assertIn("[portable]", out)          # yt-dlp install type label
        self.assertIn("2024.9.1", out)            # version line (from mocked -V output)

    def test_missing_tools_warn(self):
        import auto_convert.run as R

        log, buf = _capture()
        with mock.patch.object(R, "_log", log):
            R.describe_tools("", "")
        out = buf.getvalue()
        self.assertIn("no yt-dlp found", out) and self.assertIn("no ffmpeg found", out)


class LinkOutputTests(unittest.TestCase):
    def test_links_all_outputs(self):
        import auto_convert.run as R

        log, buf = _capture()
        with mock.patch.object(R, "_log", log):   # helpers read the module-global logger
            res = mock.MagicMock(ok=True)
            res.drums_path = "C:\\x\\a_drums.wav"
            res.nodrums_path = "C:\\x\\a_no_drums.wav"
            res.meta_path = "C:\\x\\a.meta.json"
            R.link_outputs(res)
        out = buf.getvalue()
        for p in ("a_drums.wav", "a_no_drums.wav", "a.meta.json"):
            self.assertIn(p, out)

    def test_error_result_links_nothing(self):
        import auto_convert.run as R

        log, buf = _capture()
        with mock.patch.object(R, "_log", log):   # non-quiet so a successful result WOULD emit (proves the ok-guard)
            res = mock.MagicMock(ok=False)   # not ok -> no links emitted
            R.link_outputs(res)
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
