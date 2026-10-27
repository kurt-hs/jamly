"""Unit tests for core.py command builders + pure helpers (no network/subprocess)."""
import os
import sys
import unittest


# src-layout: make the ``jamly`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import jamly.core as core  # noqa: E402 (module alias for core.X attribute access)
from jamly.core import (  # noqa: E402
    build_htdemucs_four_stems_cmd,
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
        # --ffmpeg-location must be present so the mux step has BOTH ffmpeg + ffprobe
        self.assertIn("C:\\f\\ffmpeg", cmd)
        # portable default suppresses subtitle downloads on every host
        self.assertIn("--no-subs", cmd)

    def test_without_ffmpeg_location(self):
        cmd = build_ytdlp_extract_cmd("yt-dlp.exe", "https://youtu.be/dQw4w9WgXcQ")
        self.assertTrue(cmd[0].endswith("yt-dlp.exe"))
        self.assertIn("--audio-format", cmd)
        self.assertNotIn("--ffmpeg-location", cmd)
        # portable default suppresses subtitle downloads
        self.assertIn("--no-subs", cmd)

    def test_suppress_subs_default_and_override(self):
        # default: skip subtitle downloads for this lossless-audio pipeline on every host
        cmd = build_ytdlp_extract_cmd("yt-dlp.exe", "https://youtu.be/dQw4w9WgXcQ")
        self.assertIn("--no-subs", cmd)
        # a specific build that rejects the flag -> opt out explicitly
        cmd = build_ytdlp_extract_cmd("yt-dlp.exe", "https://youtu.be/dQw4w9WgXcQ", suppress_subs=False)
        self.assertNotIn("--no-subs", cmd)


class DemucsCmdTests(unittest.TestCase):
    """Four-stem separation command for the portable torch-based `demucs` PyPI CLI."""

    def test_four_stems_args(self):
        cmd = build_htdemucs_four_stems_cmd(python_exe="python.exe", input_audio="in.wav", workdir="w")
        self.assertIn("htdemucs", cmd)         # -n/--name selects the model
        self.assertIn("--audio", cmd)          # --audio names the source file
        self.assertEqual(cmd[cmd.index("--out") + 1], "w")   # --out sets the output dir
        # portable-only: no per-device override flag (auto on every host), and NO --four-stems flag
        self.assertNotIn("--device", cmd)
        self.assertNotIn("--four-stems", cmd)

    def test_no_repo_means_official_model(self):
        # portable-only: an empty repo means the official PyPI model, so no --repo flag is emitted
        cmd = build_htdemucs_four_stems_cmd(python_exe="python.exe", input_audio="in.wav", workdir="w")
        self.assertNotIn("--repo", cmd)

    def test_threads_override(self):
        cmd = build_htdemucs_four_stems_cmd(python_exe="python.exe", input_audio="in.wav", workdir="w", threads=4)
        self.assertEqual(cmd[cmd.index("-j") + 1], "4")   # -j/--jobs sets the worker countclass FfmpegCmdTests(unittest.TestCase):
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

    def test_run_streams_live_and_buffers_empty(self):
        # Live mode writes output to the terminal (observed via on_line) and captures nothing into
        # text buffers; RunResult.stdout must stay a plain empty string, never the live pipe handle.
        seen = []
        script = "import sys; print('a'); print('[download] 95% (1/2)')"
        r = run([sys.executable, "-c", script], stream=True, on_line=seen.append)
        self.assertEqual(seen, ["a\n", "[download] 95% (1/2)\n"])   # one call per decoded line
        self.assertEqual(r.returncode, 0)
        self.assertIsInstance(r.stdout, str) and self.assertEqual(r.stdout, "")
        self.assertIsInstance(r.stderr, str) and self.assertEqual(r.stderr, "")

    def test_run_stream_check_raises_on_failure(self):
        # check=True + non-zero exit must raise even in live-stream mode (output already streamed).
        raised = False
        try:
            run([sys.executable, "-c", "import sys; sys.exit(3)"], stream=True, check=True)
        except RuntimeError as exc:
            raised = True
            self.assertIn("exit 3", str(exc))
        self.assertTrue(raised, "streamed failing command with check=True must raise")

    def test_run_stream_on_line_fires_per_line(self):
        seen = []
        run([sys.executable, "-c", "import sys; print('a'); print('b')"],
             stream=True, on_line=seen.append)
        self.assertEqual(seen, ["a\n", "b\n"])   # one call per decoded line

    def test_run_stream_returns_empty_string_buffers(self):
        r = run([sys.executable, "-c", "print('hi')"], stream=True)
        self.assertIsInstance(r.stdout, str) and self.assertEqual(r.stdout, "")
        self.assertIsInstance(r.stderr, str) and self.assertEqual(r.stderr, "")
        self.assertEqual(r.returncode, 0)

    def test_run_non_stream_still_captures(self):
        # Non-streaming runs (the unchanged default contract) capture into buffers.
        r = run([sys.executable, "-c", "import sys; print('captured')"], capture=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("captured\n", r.stdout)

    def test_run_stream_live_script(self):
        # stream=True runs a real interpreter subprocess and pumps its stdout/stderr to the terminal.
        seen = []
        script = "import sys; print('a'); print('[download] 95% (1/2)')"
        r = run([sys.executable, "-c", script], stream=True, on_line=seen.append)
        self.assertEqual(seen, ["a\n", "[download] 95% (1/2)\n"])
        self.assertEqual(r.returncode, 0)
        # live mode captures nothing into text buffers; RunResult stays a plain empty-string view.
        self.assertIsInstance(r.stdout, str) and self.assertEqual(r.stdout, "")
        self.assertIsInstance(r.stderr, str) and self.assertEqual(r.stderr, "")


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
    """detect_engine() availability check for the single separation engine (no heavy imports)."""

    def test_returns_htdemucs_when_demucs_installed(self):
        import unittest.mock as mock
        with mock.patch.object(core, "_dir_has_any", return_value=True) as m:
            self.assertEqual(core.detect_engine(), core.ENGINE_HTDEMUCS)
            args = m.call_args[0][1]
            self.assertEqual(args, ("demucs",))

    def test_returns_none_when_demucs_missing(self):
        import unittest.mock as mock
        with mock.patch.object(core, "_dir_has_any", return_value=False):
            self.assertIsNone(core.detect_engine())

    def test_no_fallback_to_other_engines(self):
        # portable-only: a missing demucs package is not treated as another engine being usable
        import unittest.mock as mock
        with mock.patch.object(core, "_dir_has_any", return_value=False) as m:
            self.assertIsNone(core.detect_engine())
            args = m.call_args[0][1]
            self.assertEqual(args, ("demucs",))
