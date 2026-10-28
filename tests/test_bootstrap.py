"""Unit tests for bootstrap.py dependency detection (no network / no heavy installs).

Uses throwaway temp dirs so it is portable and deterministic. On Windows the fake
binaries are named with .exe; on POSIX they use the bare name (detect_* handles both).
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout


# src-layout: make the ``jamly`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from jamly import bootstrap as b


def _touch(path):
    """Create parent dirs and write an empty file at `path` (robust on Windows)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()


def _make_bin_dir(path):
    """Create a dir containing BOTH ffmpeg + ffprobe (or the bare names on POSIX).

    `path` must already exist (callers pass an existing temp dir); we only write files into it.
    """
    if b.PLATFORM_WINDOWS:
        for n in ("ffmpeg.exe", "ffprobe.exe"):
            _touch(os.path.join(path, n))
    else:
        for n in ("ffmpeg", "ffprobe"):
            os.chmod(path, 0o755)


def _yt_dlp_exe_path(base):
    if b.PLATFORM_WINDOWS:
        return os.path.join(base, "win_x86", "yt-dlp.exe")
    return os.path.join(base, "linux", "yt-dlp")


class SystemPythonTests(unittest.TestCase):
    def test_running_interpreter_found(self):
        s = b.detect_system_python()
        self.assertTrue(s.ok)
        self.assertEqual(s.path, sys.executable)

    def test_report_line_mentions_ok(self):
        line = b.detect_system_python().report()
        self.assertIn("python", line.lower())


class FfmpegDetectionTests(unittest.TestCase):
    def test_both_binaries_are_ok(self):
        with tempfile.TemporaryDirectory() as d:
            _make_bin_dir(d)
            s = b.detect_ffmpeg(ffmpeg_dirs=[d])
            self.assertTrue(s.ok)
            self.assertEqual(os.path.abspath(s.path), os.path.abspath(d))

    def test_missing_ffprobe_is_warn(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "ffmpeg.exe") if b.PLATFORM_WINDOWS else os.path.join(d, "ffmpeg"))
            s = b.detect_ffmpeg(ffmpeg_dirs=[d])
            self.assertFalse(s.ok)   # WARN status

    def test_first_good_dir_wins(self):
        with tempfile.TemporaryDirectory() as good, tempfile.TemporaryDirectory() as bad:
            _make_bin_dir(good)
            _touch(os.path.join(bad, "ffmpeg.exe") if b.PLATFORM_WINDOWS else os.path.join(bad, "ffmpeg"))
            s = b.detect_ffmpeg(ffmpeg_dirs=[good, bad])   # good first -> wins
            self.assertTrue(s.ok)
            self.assertEqual(os.path.abspath(s.path), os.path.abspath(good))


class YtdlpDetectionTests(unittest.TestCase):
    def test_portable_scope_exe(self):
        with tempfile.TemporaryDirectory() as scope:
            port = os.path.join(scope, ".portable", "yt-dlp")
            _touch(_yt_dlp_exe_path(port))
            s = b.find_ytdlp(scope_dir=scope)
            self.assertTrue(s.ok)

    def test_appdata_reuse_branch(self):
        with tempfile.TemporaryDirectory() as appdata:
            os.environ["APPDATA"] = appdata
            try:
                # find_ytdlp checks a direct child of yt-dlp_win_x86 (no extra subdir)
                _touch(os.path.join(appdata, "yt-dlp_win_x86", "yt-dlp.exe"))
                s = b.find_ytdlp()
                self.assertTrue(s.ok)
                self.assertIn("yt-dlp", os.path.basename(s.path))
            finally:
                del os.environ["APPDATA"]

    def test_path_fallback_finds_ytdlp_only_on_path(self):
        # yt-dlp absent from the portable scope and %APPDATA%\yt-dlp_win_x86 but present on PATH
        # must still be found (the review's "yt-dlp isn't found" bug). PATH is isolated to the fake
        # bin dir so no host yt-dlp shadows it -> deterministic and portable across machines.
        with tempfile.TemporaryDirectory() as bindir:
            fake = os.path.join(bindir, "yt-dlp.exe" if b.PLATFORM_WINDOWS else "yt-dlp")
            _touch(fake)
            import unittest.mock as mock
            with mock.patch.dict(os.environ, {"PATH": bindir}):
                s = b.find_ytdlp()
            self.assertTrue(s.ok)
            self.assertIn("yt-dlp", os.path.basename(s.path))


class MainGateTests(unittest.TestCase):
    """main()'s portable readiness gate: python + (yt-dlp OR htdemucs repro) + ffmpeg+ffprobe."""

    def _ok(self, ok=True):
        # main() prints each summarize entry via report(); the mock must therefore mimic a ToolStatus.
        reasons = ["reason"] if not ok else []
        return type("T", (), {
            "ok": ok, "name": "", "path": None, "reasons": reasons,
            "report": lambda *a, **k: ("ok" if ok else "") + "; ".join(reasons),
        })()

    def test_ready_when_all_detected(self):
        # python + yt-dlp + ffmpeg all detected -> ready to run (rc 0). argv is ignored by portable main.
        import unittest.mock as mock
        with redirect_stdout(io.StringIO()):
            with mock.patch.object(b, "detect_system_python", return_value=self._ok(True)),                  mock.patch.object(b, "find_ytdlp", return_value=self._ok(True)),                  mock.patch.object(b, "detect_ffmpeg", return_value=self._ok(True)):
                rc = b.main(0)
        self.assertEqual(rc, 0)

    def test_not_ready_without_ffmpeg(self):
        # ffmpeg+ffprobe missing -> blocked (rc 3), even though repro + python are present.
        import unittest.mock as mock
        with mock.patch.object(b, "detect_ffmpeg", return_value=self._ok(False)),              mock.patch.object(b, "detect_system_python", return_value=self._ok(True)),              mock.patch.object(b, "find_ytdlp", return_value=self._ok(True)):
            rc = b.main(0)
        self.assertEqual(rc, 3)

    def test_ready_when_htdemucs_available(self):
        # yt-dlp absent but the demucs CLI is available -> has_repro holds via detect_engine().
        import unittest.mock as mock
        with mock.patch.object(b, "detect_ffmpeg", return_value=self._ok(True)),              mock.patch.object(b, "detect_system_python", return_value=self._ok(True)),              mock.patch.object(b, "find_ytdlp", return_value=self._ok(False)),              mock.patch.object(b, "detect_engine", return_value=b.ENGINE_HTDEMUCS):
            rc = b.main(0)
        self.assertEqual(rc, 0)

class FindFfmpegTests(unittest.TestCase):
    def test_none_for_empty_scope(self):
        self.assertIsNone(b.find_ffmpeg(tempfile.mkdtemp()))

    def test_returns_dir_with_both_binaries_when_present(self):
        d = b.find_ffmpeg()
        if not d:
            return  # no ffmpeg provisioned on this machine (skipped assertion)
        has_ff = os.path.isfile(os.path.join(d, "ffmpeg.exe")) or \
                 os.path.isfile(os.path.join(d, "ffmpeg"))
        has_fp = os.path.isfile(os.path.join(d, "ffprobe.exe")) or \
                 os.path.isfile(os.path.join(d, "ffprobe"))
        self.assertTrue(has_ff and has_fp)


# --------------------------------------------------------------------------- #
# _os_family() single source of truth (D1 entropy dedup)
# --------------------------------------------------------------------------- #
class OsFamilyDedupTests(unittest.TestCase):
    """
    _os_family() must live in ONE module (core); bootstrap imports it rather than copying its own,
    so this identity assertion guards against re-introducing a duplicated OS-detection function.
    Branch coverage is exercised via patched PLATFORM_WINDOWS / platform.machine / sys.platform; only
    _os_family() runs inside each patch so no global state leaks between cases."""

    def test_single_source_of_truth(self):
        import jamly.core as c
        self.assertIs(b._os_family, c._os_family)   # bootstrap uses core's _os_family, not a copy

    def _run_with_platform(self, win, machine, platform_name):
        import jamly.core as c
        import unittest.mock as mock
        with mock.patch("jamly.core.PLATFORM_WINDOWS", bool(win)), \
                mock.patch.object(c.platform, "machine", lambda m=machine: m or ""), \
                mock.patch("jamly.core.sys") as fake_sys:
            fake_sys.platform = platform_name
            return c._os_family()

    def test_families_for_all_branches(self):
        cases = [
            (True, "x86_64", "", "win"),
            (False, "x86_64", "", "linux"),
            (False, "aarch64", "", "linux-arm"),
            (False, "aarch64", "darwin", "macos"),  # darwin + arm -> macos
        ]
        for win, machine, platform_name, expected in cases:
            self.assertEqual(self._run_with_platform(win, machine, platform_name), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
