"""Unit tests for core.py's separation-engine builders and runner scaffolding.

These exercise the pure command-building + result-handling logic without invoking real
inference (no torch / audio-separator / onnxruntime). Heavy branches are driven by
monkeypatching availability so they pass deterministically on any machine."""
import os
import sys
import tempfile
import unittest


# src-layout: make the ``jamly`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import jamly.core as c


class HtdemucsBuilderTests(unittest.TestCase):
    def test_flags_present_and_ordered(self):
        cmd = c.build_htdemucs_four_stems_cmd("python", "in.wav", "/work", threads=2)
        self.assertEqual(cmd[1:3], ["-m", "demucs"])
        self.assertNotIn("separate", cmd)
        # model selection uses -n/--name (not --model); htdemucs selects the 4-source model
        self.assertIn("-n", cmd) and self.assertEqual(cmd[cmd.index("-n") + 1], "htdemucs")
        # the audio file is a POSITIONAL argument: demucs takes `tracks` (nargs='*') and has
        # no --audio option (an invalid flag -> exit 2, nothing separates)
        self.assertNotIn("--audio", cmd) and self.assertIn("in.wav", cmd)
        self.assertIn("--out", cmd) and self.assertIn("/work", cmd)
        # -j is the parallelism flag (not --threads); source count comes from the model
        self.assertEqual(cmd[cmd.index("-j") + 1], "2")

    def test_no_threads_flag_by_default(self):
        cmd = c.build_htdemucs_four_stems_cmd("python", "in.wav", "/work")
        # no per-core flag by default; demucs auto-selects device (cuda/mps/cpu)
        self.assertNotIn("-j", cmd)


class LocateStemsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, relpath, data=b"x"):
        p = os.path.join(self._tmp, *relpath)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_finds_only_the_four_known_stems(self):
        self._write(["htdemucs", "track", "drums.wav"])
        self._write(["htdemucs", "track", "bass.wav"])
        self._write(["htdemucs", "track", "other.wav"])
        self._write(["htdemucs", "track", "vocals.wav"])
        # decoys that must be ignored
        self._write(["htdemucs", "track", "extra.wav"], data=b"y")
        self._write(["htdemucs", "track", "hihat.wav"])
        self._write(["htdemucs", "track", "drums.flac"])  # not .wav

        locs = c._locate_stems(self._tmp)
        keys = sorted(os.path.splitext(os.path.basename(p))[0] for p in locs)
        self.assertEqual(keys, ["bass", "drums", "other", "vocals"])

    def test_missing_dirs_is_safe(self):
        self.assertEqual(c._locate_stems(self._tmp), [])


class HtdemucsRunnerTests(unittest.TestCase):
    def test_htdemucs_engine_builds_official_demucs_cli(self):
        # engine selection -> availability gate (mocked present) -> official demucs CLI shape.
        import os
        import unittest.mock as mock
        fake = [os.path.abspath(os.path.join("htdemucs", "track", "%s.wav" % s))
                for s in ("drums", "bass", "other", "vocals")]
        with mock.patch("jamly.core._module_available", return_value=True), \
             mock.patch("jamly.core.find_python", return_value="python"), \
             mock.patch.object(c, "_locate_stems", return_value=fake), \
             mock.patch.object(c, "run", return_value=c.RunResult([], 0, "", "")) as run_mock:
            c.separate_stems("song.wav", engine=c.ENGINE_HTDEMUCS)
        self.assertEqual(run_mock.call_args.args[0][1:3], ["-m", "demucs"])
        self.assertNotIn("separate", run_mock.call_args.args[0])
        idx = run_mock.call_args.args[0].index("-n")
        self.assertEqual(run_mock.call_args.args[0][idx + 1], "htdemucs")   # -n/--name selects the model
        self.assertNotIn("--four-stems", run_mock.call_args.args[0])       # source count comes from the model

    def test_htdemucs_engine_missing_package_raises(self):
        # availability gate fails first (mocked as if no demucs dir) -> clear RuntimeError naming package
        import unittest.mock as mock
        with self.assertRaises(RuntimeError) as ctx, \
             mock.patch("jamly.core._module_available", return_value=False), \
             mock.patch("jamly.core.find_python", return_value="python"):
            c.separate_stems("song.wav", engine=c.ENGINE_HTDEMUCS)
        self.assertIn("demucs package is not installed", str(ctx.exception))


class SeparateStemsFailureTests(unittest.TestCase):
    """Portable-only failure paths: always the demucs PyPI CLI, no engine dispatch."""

    def test_no_python_raises_runtimeerror(self):
        import unittest.mock as m
        with m.patch("jamly.core.find_python", return_value=None), \
                m.patch("jamly.core._module_available", return_value=True):
            with self.assertRaises(RuntimeError) as ctx:
                c.separate_stems("song.wav")
        self.assertIn("no python interpreter", str(ctx.exception))

    def test_missing_package_raises_runtimeerror(self):
        # availability gate fails first (mocked no demucs dir) -> RuntimeError names the package
        import unittest.mock as m
        with m.patch("jamly.core._module_available", return_value=False), \
                m.patch("jamly.core.find_python", return_value="python"):
            with self.assertRaises(RuntimeError) as ctx:
                c.separate_stems("song.wav")
        self.assertIn("demucs package is not installed", str(ctx.exception))

if __name__ == "__main__":
    unittest.main(verbosity=2)
