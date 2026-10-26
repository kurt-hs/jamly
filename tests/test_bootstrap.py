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


# src-layout: make the ``splitfy`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
import sys as _shim_sys  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from splitfy import bootstrap as b


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
    _os_family() must live in ONE module (core) and be shared by bootstrap + provision.

    If either module ever re-introduces its own copy, this identity assertion fails -- the whole
    point of D1 was to remove a duplicated OS-detection function. Branch coverage is exercised via
    patched PLATFORM_WINDOWS / platform.machine / sys.platform; only _os_family() runs inside each
    patch so no global state leaks between cases."""

    def test_single_source_of_truth(self):
        import splitfy.core as c
        import splitfy.provision as p
        self.assertIs(b._os_family, c._os_family)
        self.assertIs(p._os_family, c._os_family)

    def _run_with_platform(self, win, machine, platform_name):
        import splitfy.core as c
        import unittest.mock as mock
        with mock.patch("splitfy.core.PLATFORM_WINDOWS", bool(win)), \
                mock.patch.object(c.platform, "machine", lambda m=machine: m or ""), \
                mock.patch("splitfy.core.sys") as fake_sys:
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
