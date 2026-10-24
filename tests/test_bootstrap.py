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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootstrap as b  # noqa: E402


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


class DemucsWeightsTests(unittest.TestCase):
    def _repo(self, d):
        for n in ("htdemucs.yaml", "955717e8-8726e21a.th", "d12395a8-e57c48e6.th"):
            _touch(os.path.join(d, n))

    def test_returns_all_weights_when_yaml_present(self):
        with tempfile.TemporaryDirectory() as d:
            self._repo(d)
            ws = b.demucs_weights(d)
            self.assertEqual(ws, ["955717e8-8726e21a.th", "d12395a8-e57c48e6.th"])

    def test_empty_without_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "some-other.yaml"))
            self.assertEqual(b.demucs_weights(d), [])  # no htdemucs.yaml -> nothing usable

    def test_empty_for_missing_repo(self):
        self.assertEqual(b.demucs_weights("/no/such/repo/here"), [])


class UvrDetectionTests(unittest.TestCase):
    def _uvr_tree(self, root, with_ffmpeg=True):
        repo = os.path.join(root, "models", "Demucs_Models", "v3_v4_repo")
        for n in ("htdemucs.yaml", "955717e8-8726e21a.th"):
            _touch(os.path.join(repo, n))
        if with_ffmpeg:
            _touch(os.path.join(root, "ffmpeg.exe") if b.PLATFORM_WINDOWS else os.path.join(root, "ffmpeg"))

    def test_detect_uvr_fields(self):
        with tempfile.TemporaryDirectory() as root:
            self._uvr_tree(root)
            r = b.detect_uvr(root)
            self.assertTrue(r["uvr_exists"])
            self.assertTrue(r["repo_exists"])
            self.assertTrue(r["weights_file_present"])
            self.assertTrue(r["ffmpeg_bundled"])   # ffmpeg.exe at root

    def test_missing_install(self):
        # a path that does not exist -> everything reports False/None
        r = b.detect_uvr("/no/such/path/uvr5-install")
        self.assertFalse(r["uvr_exists"])
        self.assertFalse(r["repo_exists"])
        self.assertFalse(r["weights_file_present"])
        self.assertFalse(r["ffmpeg_bundled"])


class MainGateTests(unittest.TestCase):
    """Exercise main()'s critical-gate logic with throwaway temp UVR roots."""

    @staticmethod
    def _provision(root):
        # weights repo + ffmpeg+ffprobe placed directly in root (main scans the UVR root)
        repo = os.path.join(root, "models", "Demucs_Models", "v3_v4_repo")
        for n in ("htdemucs.yaml", "955717e8-8726e21a.th"):
            _touch(os.path.join(repo, n))
        _make_bin_dir(root)

    @staticmethod
    def _weights_only(root):
        repo = os.path.join(root, "models", "Demucs_Models", "v3_v4_repo")
        for n in ("htdemucs.yaml", "955717e8-8726e21a.th"):
            _touch(os.path.join(repo, n))

    def test_ready_when_ffprobe_present(self):
        with tempfile.TemporaryDirectory() as root:
            self._provision(root)
            old = dict(os.environ)
            os.environ["AUTOCV_UVR_ROOT"] = root
            try:
                with redirect_stdout(io.StringIO()):
                    rc = b.main()
            finally:
                os.environ.clear(); os.environ.update(old)
            self.assertEqual(rc, 0)

    def test_not_ready_without_ffprobe(self):
        with tempfile.TemporaryDirectory() as root:
            self._weights_only(root)   # python + weights ok, but NO ffmpeg/ffprobe
            old = dict(os.environ)
            os.environ["AUTOCV_UVR_ROOT"] = root
            os.environ["AUTOCV_SCOPE"] = root   # isolated scope: no portable ffmpeg here
            try:
                with redirect_stdout(io.StringIO()):
                    rc = b.main()
            finally:
                os.environ.clear(); os.environ.update(old)
            self.assertEqual(rc, 3)   # blocked on ffmpeg+ffprobe


if __name__ == "__main__":
    unittest.main(verbosity=2)
