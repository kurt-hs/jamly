"""Unit tests for core.py command builders + pure helpers (no network/subprocess)."""
import os
import sys
import tempfile
import unittest


# src-layout: make the ``splitfy`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
import sys as _shim_sys  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import splitfy.core as core  # noqa: E402 (module alias for core.X attribute access)
from splitfy.core import (  # noqa: E402
    PLATFORM_WINDOWS, RunResult, build_demucs_four_stems_cmd,
    build_ffmpeg_mix_cmd, build_ytdlp_extract_cmd, ensure_output_dir,
    sanitize_filename, run, which, _abs, _exists_exe,
)


class YtdlpCmdTests(unittest.TestCase):
    def test_with_custom_ffmpeg_location(self):
        cmd = build_ytdlp_extract_cmd("yt-dlp.exe", "https://youtu.be/dQw4w9WgXcQ",
                                       ffmpeg_dir=r"C:\f\ffmpeg")
        self.assertEqual(cmd[0], "yt-dlp.exe")
        self.assertIn("--extract-audio", cmd)
        self.assertEqual(cmd[-2:], ["-o", "%(title)s.%(ext)s"])
        # --ffmpeg-location must be present (this box's custom build requires it)
        self.assertIn("C:\\f\\ffmpeg", cmd)

    def test_without_ffmpeg_location(self):
        cmd = build_ytdlp_extract_cmd("yt-dlp.exe", "https://youtu.be/dQw4w9WgXcQ")
        self.assertTrue(cmd[0].endswith("yt-dlp.exe"))
        self.assertIn("--audio-format", cmd)
        self.assertNotIn("--ffmpeg-location", cmd)

    def test_no_subs_never_injected(self):
        # this box's yt-dlp rejects --no-subs; assert it is never added by the builder
        cmd = build_ytdlp_extract_cmd("yt-dlp.exe", "https://youtu.be/dQw4w9WgXcQ")
        self.assertNotIn("--no-subs", cmd)


class DemucsCmdTests(unittest.TestCase):
    def test_four_stems_args(self):
        cmd = build_demucs_four_stems_cmd(
            python_exe="python.exe", input_audio=r"C:\in.wav",
            workdir=r"C:\work\separated", repo=r"C:\repo",
            weights_name="955717e8-8726e21a", device=None, threads=0)
        self.assertIn("htdemucs", cmd)
        self.assertIn("--four-stems", cmd)
        self.assertIn("--input", cmd)
        self.assertIn("C:\\in.wav", cmd)
        self.assertTrue(cmd[-2:], ["-o", "C:\\work\\separated"])
        self.assertNotIn("--device", cmd)          # auto when None

    def test_device_override(self):
        cmd = build_demucs_four_stems_cmd(python_exe="python.exe", input_audio="in.wav",
                                           workdir="w", repo="", weights_name="955717e8-8726e21a",
                                           device="cuda", threads=0)
        self.assertIn("--device", cmd)
        self.assertEqual(cmd[cmd.index("--device") + 1], "cuda")

    def test_threads_override(self):
        cmd = build_demucs_four_stems_cmd(python_exe="python.exe", input_audio="in.wav",
                                           workdir="w", repo="", weights_name="955717e8-8726e21a",
                                           device=None, threads=4)
        self.assertEqual(cmd[cmd.index("--threads") + 1], "4")


class FfmpegCmdTests(unittest.TestCase):
    def test_mix_non_drum_stems(self):
        cmd = build_ffmpeg_mix_cmd("ffmpeg.exe", "bass.wav", "other.wav", "vocals.wav", "out.wav")
        self.assertEqual(cmd[0], "ffmpeg.exe")
        self.assertIn("-i", cmd)
        self.assertTrue(cmd.count("-i"), 3)                 # three inputs
        self.assertIn("-map", cmd)
        self.assertEqual(cmd[cmd.index("-map") + 1], "0?")
        self.assertIn("-mixer_channels", cmd)
        self.assertEqual(cmd[cmd.index("-mixer_channels") + 1], "2")
        self.assertTrue(cmd[-1].endswith("out.wav"))

    def test_shortest_flag(self):
        cmd = build_ffmpeg_mix_cmd("ffmpeg.exe", "bass.wav", "other.wav", "vocals.wav", "out.wav")
        self.assertIn("-shortest", cmd)


class PureHelperTests(unittest.TestCase):
    def test_abs_expands_user_and_vars(self):
        a = _abs("~/../foo/../bar")
        self.assertTrue(a.endswith(os.path.join("bar")))

    def test_exists_exe_false_for_missing(self):
        self.assertFalse(_exists_exe("/no/such/path/here"))

    def test_which_returns_none_for_bogus(self):
        # a name guaranteed not on PATH in CI; allow it to be None (not raise)
        r = which("definitely-not-a-real-binary-xyz")
        self.assertIsNone(r)

    def test_sanitize_filename_windows_unsafe_chars(self):
        raw = 'My Title/<year>: "test".mp4'
        s = sanitize_filename(raw)
        for bad in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
            self.assertNotIn(bad, s)
        # slashes removed -> hyphen; ':' and '"' stripped
        # '/' -> '-', ':' and '"' stripped; '<year>' angle-brackets also removed (safe)
        self.assertEqual(s, 'My Title-year test.mp4')

    def test_sanitize_allowed_set(self):
        s = sanitize_filename("a b c", allowed="abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(s, "abc")

    def test_ensure_output_dir_creates_and_returns_abs(self):
        d = ensure_output_dir(os.path.join(os.getcwd(), "_t_out"))
        self.assertTrue(os.path.isdir(d))
        os.rmdir(_abs(d))


class RunTests(unittest.TestCase):
    def _python_ok(self):
        # cross-platform: interpreter running a trivial script always exists here
        return run([sys.executable, "-c", "print(1)"])

    def test_run_success(self):
        r = self._python_ok()
        self.assertEqual(r.returncode, 0)
        self.assertIn("1\n", r.stdout)

    def test_run_file_not_found(self):
        r = run(["/no/such/binary-xyz.exe"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("NOT FOUND", r.stderr)


class AudioFormatCmdTests(unittest.TestCase):
    """build_stem_encode_cmd: lossy re-encode vs raw verbatim-copy decision."""

    def test_mp3_builds_lame_320k_reencode(self):
        cmd = core.build_stem_encode_cmd("ffmpeg.exe", "in.wav", "out.mp3", fmt="mp3")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd[0], "ffmpeg.exe")
        # libmp3lame V0 320kbps -- the lossy default encoder settings
        self.assertIn("-c:a", cmd) and self.assertEqual(cmd[cmd.index("-c:a") + 1], "libmp3lame")
        self.assertIn("-b:a", cmd) and self.assertEqual(cmd[cmd.index("-b:a") + 1], "320k")
        # input is abs-pathed; output path passed through verbatim (last arg)
        self.assertTrue(cmd[cmd.index("-i") + 1].endswith(os.path.abspath("in.wav")))
        self.assertEqual(cmd[-1], "out.mp3")

    def test_wav_returns_none_for_verbatim_copy(self):
        # raw WAV needs no ffmpeg re-encode -> caller copies the stem verbatim instead
        self.assertIsNone(core.build_stem_encode_cmd("ffmpeg.exe", "in.wav", "out.wav", fmt="wav"))

    def test_unknown_format_is_treated_as_raw(self):
        # defensive: an unrecognised format must not invent encoder flags -> None (verbatim copy)
        self.assertIsNone(core.build_stem_encode_cmd("ffmpeg.exe", "in.wav", "out.aac", fmt="aac"))

    def test_default_arg_is_mp3_reencode(self):
        # default parameter is the lossy format, so omitting it still re-encodes
        cmd = core.build_stem_encode_cmd("ffmpeg.exe", "in.wav", "out.mp3")
        self.assertIsNotNone(cmd)
        self.assertIn("libmp3lame", cmd)


class DetectEngineTests(unittest.TestCase):
    """detect_engine() / _scan_uvr_roots() availability checks (no heavy imports)."""

    WEIGHT = "955717e8-8726e21a.th"       # filename only
    MARKER_DIR = os.path.join("models", "Demucs_Models", "v3_v4_repo")

    def _make_weight_root(self):
        base = tempfile.mkdtemp(prefix="uvr5_")
        repo = os.path.join(base, self.MARKER_DIR)
        os.makedirs(repo, exist_ok=True)
        with open(os.path.join(repo, self.WEIGHT), "wb") as fh:
            fh.write(b"fake-weights")
        return base

    def test_env_root_marker_detected_as_uvr5(self):
        import unittest.mock as mock
        root = self._make_weight_root()
        with mock.patch.dict(os.environ, {"AUTOCV_UVR_ROOT": root}, clear=False), \
             mock.patch.object(core, "_scan_uvr_roots", return_value=[root]):
            self.assertEqual(core.detect_engine(), core.ENGINE_UVR5)

    def test_scan_includes_explicit_env_root(self):
        import unittest.mock as mock
        root = self._make_weight_root()
        with mock.patch.dict(os.environ, {"AUTOCV_UVR_ROOT": root}, clear=False), \
             mock.patch.object(core, "_dir_has_any", return_value=False) as dh:
            # _scan must surface the explicit env override regardless of weights
            roots = core._scan_uvr_roots()
            self.assertIn(os.path.abspath(root), roots)

    def test_no_weights_or_pkg_is_not_uv5(self):
        import unittest.mock as mock
        with mock.patch.object(core, "_scan_uvr_roots", return_value=[]), \
             mock.patch.object(core, "_dir_has_any", return_value=False):
            res = core.detect_engine()
            self.assertNotEqual(res, core.ENGINE_UVR5)

    def test_discovery_finds_appdata_programs_root(self):
        # On this box the UVR5 GUI ships at AppData\Local\Programs<name> with weights present.
        import unittest.mock as mock
        home = os.environ.get("HOME") or (os.environ.get("USERPROFILE") or "")
        prog_apps = os.path.join(_abs(home), "AppData", "Local", "Programs")
        if not os.path.isdir(prog_apps):
            self.skipTest(r"no AppData\Local\Programs to scan")
        with mock.patch.dict(os.environ, clear=False):
            roots = core._scan_uvr_roots()
            # explicit env override (if any) is always surfaced; otherwise the bounded walk
            # of AppData\Local\Programs should surface the UVR5 GUI dir when weights are present
            if not os.environ.get("AUTOCV_UVR_ROOT"):
                expected = os.path.join(prog_apps, "Ultimate Vocal Remover")
                self.assertIn(expected, roots)
                self.assertTrue(any(
                    os.path.isfile(os.path.join(_abs(r), core.DEMUCS_REPO_SUBDIR,
                                                core.DEMUCS_WEIGHT_FILE)) for r in roots))
