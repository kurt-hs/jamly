"""Unit tests for core.py command builders + pure helpers (no network/subprocess)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import (  # noqa: E402
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
