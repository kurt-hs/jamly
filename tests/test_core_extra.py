"""Unit tests for core.py's separation-engine builders and runner scaffolding.

These exercise the pure command-building + result-handling logic without invoking real
inference (no torch / audio-separator / onnxruntime). Heavy branches are driven by
monkeypatching availability so they pass deterministically on any machine."""
import os
import sys
import tempfile
import unittest


# src-layout: make the ``auto_convert`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
import sys as _shim_sys  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import auto_convert.core as c


class HtdemucsBuilderTests(unittest.TestCase):
    def test_flags_present_and_ordered(self):
        cmd = c.build_htdemucs_four_stems_cmd("python", "in.wav", "/work", threads=2)
        # python_exe is element 0; the CLI module + subcommand follow
        self.assertEqual(cmd[1:4], ["-m", "demucs", "separate"])
        # model selection uses -n/--name (not --model); htdemucs selects the 4-source model
        self.assertIn("-n", cmd) and self.assertEqual(cmd[cmd.index("-n") + 1], "htdemucs")
        self.assertIn("--audio", cmd) and self.assertIn("in.wav", cmd)
        self.assertIn("--out", cmd) and self.assertIn("/work", cmd)
        # -j is the parallelism flag (not --threads); source count comes from the model
        self.assertEqual(cmd[cmd.index("-j") + 1], "2")

    def test_no_threads_flag_by_default(self):
        cmd = c.build_htdemucs_four_stems_cmd("python", "in.wav", "/work")
        # no per-core flag by default; demucs auto-selects device (cuda/mps/cpu)
        self.assertNotIn("-j", cmd)


class Uvr5BuilderTests(unittest.TestCase):
    def _cmd(self, repo=""):
        return c.build_demucs_four_stems_cmd("python", "song.wav", "/w", repo=repo, threads=3)

    def test_repo_flag_included_when_given(self):
        cmd = self._cmd(repo="/models/Demucs_Models/v3_v4_repo")
        idx = cmd.index("--repo")
        self.assertEqual(cmd[idx + 1], "/models/Demucs_Models/v3_v4_repo")

    def test_no_repo_flag_when_empty(self):
        cmd = self._cmd(repo="")
        self.assertNotIn("--repo", cmd)

    def test_four_stems_and_output_present(self):
        cmd = self._cmd()
        self.assertIn("--four-stems", cmd)
        self.assertIn("song.wav", cmd)
        self.assertEqual(cmd[cmd.index("-o") + 1], "/w")


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
        self._write(["separated", "htdemucs", "drums.wav"])
        self._write(["separated", "htdemucs", "bass.wav"])
        self._write(["separated", "htdemucs", "other.wav"])
        self._write(["separated", "htdemucs", "vocals.wav"])
        # decoys that must be ignored
        self._write(["separated", "htdemucs", "extra.wav"], data=b"y")
        self._write(["separated", "htdemucs", "hihat.wav"])
        self._write(["separated", "htdemucs", "drums.flac"])  # not .wav

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
        fake = [os.path.abspath(os.path.join("separated", "htdemucs", "%s.wav" % s))
                for s in ("drums", "bass", "other", "vocals")]
        with mock.patch("auto_convert.core._dir_has_any", return_value=True), \
             mock.patch("auto_convert.core.find_python", return_value="python"), \
             mock.patch.object(c, "_locate_stems", return_value=fake), \
             mock.patch.object(c, "run") as run_mock:
            res = c.separate_stems("song.wav", engine=c.ENGINE_HTDEMUCS, uvr_root="")
        # module is `demucs`, subcommand `separate` -- NOT the removed htdemucs four-stems entry point
        self.assertEqual(run_mock.call_args.args[0][1:4], ["-m", "demucs", "separate"])
        idx = run_mock.call_args.args[0].index("-n")
        self.assertEqual(run_mock.call_args.args[0][idx + 1], "htdemucs")   # -n/--name selects the model
        self.assertNotIn("--four-stems", run_mock.call_args.args[0])       # source count comes from the model

    def test_htdemucs_engine_missing_package_raises(self):
        # availability gate fails first (mocked as if no demucs dir) -> clear RuntimeError naming package
        import unittest.mock as mock
        with self.assertRaises(RuntimeError) as ctx, \
             mock.patch("auto_convert.core._dir_has_any", return_value=False), \
             mock.patch("auto_convert.core.find_python", return_value="python"):
            c.separate_stems("song.wav", engine=c.ENGINE_HTDEMUCS, uvr_root="")
        self.assertIn("demucs package is not installed", str(ctx.exception))


class SeparateStemsFailureTests(unittest.TestCase):
    def test_unknown_engine_raises_valueerror_before_any_run(self):
        # real env: a python interpreter exists; bogus engine hits the else branch
        with self.assertRaises(ValueError):
            c.separate_stems("song.wav", engine="bogus")

    def test_no_python_raises_runtimeerror(self):
        import unittest.mock as m
        with m.patch("auto_convert.core.find_python", return_value=None), \
             m.patch("auto_convert.core._dir_has_any", return_value=True):
            with self.assertRaises(RuntimeError) as ctx:
                c.separate_stems("song.wav", engine=c.ENGINE_UVR5, uvr_root="/x")
        self.assertIn("no python interpreter", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
