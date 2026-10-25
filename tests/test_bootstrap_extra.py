"""Unit tests for curated cross-machine install-root discovery and config persistence (§11 / DESIGN.md §10).

Discovery logic is exercised via :func:`splitfy.bootstrap._scan_known_roots` (the pure probe)
against throwaway temp dirs -- fast, deterministic, and independent of this host's real installs.
The cache-aware :func:`splitfy.bootstrap.find_known_roots` is tested separately with an
isolated config path so it never writes to the repo root during tests.

On Windows fake binaries are named ``ffmpeg.exe`` / ``yt-dlp.exe``; on POSIX the bare name -- both
are accepted by find_*.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock


# src-layout: make the ``splitfy`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from splitfy import bootstrap as b  # noqa: E402
from splitfy.config_state import ConfigStore  # noqa: E402


def _touch(path):
    """Create parent dirs and write an empty file at `path` (robust on Windows)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()


def _bin_dir_with_both(path):
    ext = ".exe" if b.PLATFORM_WINDOWS else ""
    for n in ("ffmpeg%s" % ext, "ffprobe%s" % ext):
        _touch(os.path.join(path, n))


def _bin_dir_with_ffmpeg_only(path):
    ext = ".exe" if b.PLATFORM_WINDOWS else ""
    _touch(os.path.join(path, "ffmpeg%s" % ext))


def _yt_dlp_exe_path(base):
    if b.PLATFORM_WINDOWS:
        return os.path.join(base, "win_x86", "yt-dlp.exe")
    return os.path.join(base, "linux", "yt-dlp")


class ScanKnownRootsTests(unittest.TestCase):
    """Probes the pure filesystem probe (_scan_known_roots) against temp dirs."""

    def test_ffmpeg_needs_both(self):
        only = tempfile.mkdtemp()
        _bin_dir_with_ffmpeg_only(only)
        r = b._scan_known_roots(additional_dirs=[only], uvr_scan=False)
        self.assertNotIn(only, r["ffmpeg"])  # ffmpeg-only dir is never reported

    def test_both_binaries_are_reported(self):
        good = tempfile.mkdtemp()
        _bin_dir_with_both(good)
        r = b._scan_known_roots(additional_dirs=[good], uvr_scan=False)
        self.assertIn(good, r["ffmpeg"])

    def test_first_good_additional_dir_wins_for_ffmpeg(self):
        good = tempfile.mkdtemp()
        _bin_dir_with_both(good)
        partial = tempfile.mkdtemp()
        _bin_dir_with_ffmpeg_only(partial)  # only ffmpeg -> not reusable
        r = b._scan_known_roots(additional_dirs=[partial, good], uvr_scan=False)  # isolate from real-box installs
        self.assertIn(good, r["ffmpeg"])          # a dir shipping BOTH binaries is reported as reusable
        self.assertNotIn(partial, r["ffmpeg"])    # a dir with only ffmpeg is NOT

    def test_ytdlp_standalone_in_scope(self):
        scope = tempfile.mkdtemp()
        port = os.path.join(scope, ".portable", "yt-dlp")
        exe = _yt_dlp_exe_path(port)
        _touch(exe)
        r = b._scan_known_roots(scope_dir=scope, uvr_scan=False)
        # the standalone yt-dlp shipped under scope/.portable/yt-dlp is reported as a target
        self.assertTrue(any("yt-dlp" in os.path.basename(x) for x in r["yt-dlp"]))

    def test_ytdlp_appdata_reuse(self):
        old = dict(os.environ)  # restore env after mutating APPDATA -- never delete it permanently
        appdata = tempfile.mkdtemp()  # standalone yt-dlp ships under AppData\yt-dlp_win_x86
        os.environ["APPDATA"] = appdata
        try:
            _touch(os.path.join(appdata, "yt-dlp_win_x86", "yt-dlp.exe"))
            r = b._scan_known_roots(uvr_scan=False)  # isolate the standalone probe from real-box installs
            self.assertTrue(any("yt-dlp" in os.path.basename(p) for p in r["yt-dlp"]))
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_curated_app_bundles_both_binaries(self):
        lp = tempfile.mkdtemp()
        _bin_dir_with_both(lp)  # a curated app dir shipping BOTH binaries -> reusable
        with mock.patch.object(b, "_curated_app_roots", return_value=[lp]), \
             mock.patch.object(b, "which", return_value=None):   # isolate PATH-first probe too
            r = b._scan_known_roots(uvr_scan=False)  # isolate the curated probe from real-box installs
        self.assertEqual(r["ffmpeg"], [lp])  # a dir shipping BOTH binaries is reported as reusable
        self.assertTrue(os.path.isdir(lp))

    def test_curated_app_ffmpeg_only_is_not_reported(self):
        lp = tempfile.mkdtemp()
        _bin_dir_with_ffmpeg_only(lp)  # ffmpeg only -> not reusable for our yt-dlp build
        with mock.patch.object(b, "_curated_app_roots", return_value=[lp]), \
             mock.patch.object(b, "which", return_value=None):   # isolate PATH-first probe too
            r = b._scan_known_roots(uvr_scan=False)  # isolate the curated probe from real-box installs
        self.assertEqual(r["ffmpeg"], [])  # a dir with only ffmpeg is NOT reported as reusable

    def test_uvr_root_discovered(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "models", "Demucs_Models", "v3_v4_repo")
            for n in ("htdemucs.yaml", "955717e8-8726e21a.th"):
                _touch(os.path.join(repo, n))
            with mock.patch.object(b, "_scan_uvr_roots", return_value=[root]):
                r = b._scan_known_roots(uvr_scan=True)
            self.assertIn(root, r["uvr_root"])

    def test_result_keys(self):
        with tempfile.TemporaryDirectory() as scope:
            port = os.path.join(scope, ".portable", "yt-dlp")
            _touch(_yt_dlp_exe_path(port))
            r = b._scan_known_roots(scope_dir=scope, uvr_scan=False)
            self.assertEqual(set(r.keys()), {"ffmpeg", "yt-dlp", "uvr_root"})


class FindKnownRootsCacheTests(unittest.TestCase):
    """find_known_roots returns cached paths fast; rescans only when forced/empty.

    The slow UVR-weight probe is stubbed so caching behaviour is exercised against the fast PATH /
    curated probes without paying this host's full-tree scan each time.
    """

    def _isolated_config(self, scope):
        return os.path.join(scope, "splitfy.json")

    def setUp(self):
        self._uvr = mock.patch.object(b, "_scan_uvr_roots", return_value=[])
        self._uvr.start()

    def tearDown(self):
        self._uvr.stop()

    def test_first_call_scans_then_caches(self):
        with tempfile.TemporaryDirectory() as scope:
            cfg = self._isolated_config(scope)
            good = tempfile.mkdtemp()
            _bin_dir_with_both(good)
            r1 = b.find_known_roots(config_path=cfg, additional_dirs=[good])  # cache miss -> scan
            self.assertIn(good, r1["ffmpeg"])
            self.assertTrue(os.path.isfile(cfg))  # results persisted for reuse

    def test_second_call_returns_cached_without_rescanning(self):
        with tempfile.TemporaryDirectory() as scope:
            cfg = self._isolated_config(scope)
            good = tempfile.mkdtemp()
            _bin_dir_with_both(good)
            b.find_known_roots(config_path=cfg, additional_dirs=[good])  # prime cache
            r3 = b.find_known_roots(config_path=cfg)  # cache hit -> no scan of `good` needed
            self.assertIn(good, r3["ffmpeg"])

    def test_force_flag_rescans_even_with_cache(self):
        with tempfile.TemporaryDirectory() as scope:
            cfg = self._isolated_config(scope)
            good = tempfile.mkdtemp()
            _bin_dir_with_both(good)
            b.find_known_roots(config_path=cfg, additional_dirs=[good])  # prime cache
            r4 = b.find_known_roots(config_path=cfg, force=True, additional_dirs=[good])
            self.assertIn(good, r4["ffmpeg"])

    def test_empty_cache_triggers_scan(self):
        with tempfile.TemporaryDirectory() as scope:
            cfg = self._isolated_config(scope)  # no file yet -> empty cache
            good = tempfile.mkdtemp()
            _bin_dir_with_both(good)
            r5 = b.find_known_roots(config_path=cfg, additional_dirs=[good])
            self.assertIn(good, r5["ffmpeg"])
class ConfigPersistenceTests(unittest.TestCase):
    """splitfy.json round-trips discovered paths and is reused on later runs."""

    def _config_path(self, scope):
        return os.path.join(scope, "splitfy.json")

    def test_missing_file_loads_empty(self):
        cfg = self._config_path(tempfile.mkdtemp())
        self.assertFalse(os.path.isfile(cfg))
        store = ConfigStore.from_path(cfg)
        self.assertTrue(store.empty)  # empty when absent/invalid (no crash)

    def test_round_trip_record_and_reuse(self):
        scope = tempfile.mkdtemp()
        found = [tempfile.mkdtemp(), tempfile.mkdtemp()]
        store = ConfigStore.create(self._config_path(scope))
        store.record("ffmpeg", found, preferred=True)
        store.save()  # persist so a later run can reuse these paths without rescanning

        # a fresh load from disk returns the recorded paths (reuse without rescanning)
        reloaded = ConfigStore.from_path(self._config_path(scope))
        self.assertFalse(reloaded.empty)
        self.assertEqual(reloaded.get_paths("ffmpeg"), found)
        self.assertTrue(reloaded.get_field("ffmpeg", "preferred"))

    def test_invalid_json_loads_empty(self):
        scope = tempfile.mkdtemp()
        with open(self._config_path(scope), "w", encoding="utf-8") as fh:
            fh.write("{ not valid json ")
        store = ConfigStore.from_path(self._config_path(scope))
        self.assertTrue(store.empty)  # empty, no crash


if __name__ == "__main__":
    unittest.main(verbosity=2)
