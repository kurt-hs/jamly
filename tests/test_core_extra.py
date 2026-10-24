"""Unit tests for core.py's separation-engine builders and runner scaffolding.

These exercise the pure command-building + result-handling logic without invoking real
inference (no torch / audio-separator / onnxruntime). Heavy branches are driven by
monkeypatching availability so they pass deterministically on any machine."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core as c  # noqa: E402


class HtdemucsBuilderTests(unittest.TestCase):
    def test_flags_present_and_ordered(self):
        cmd = c.build_htdemucs_four_stems_cmd("python", "in.wav", "/work", threads=2)
        # python_exe is element 0; the subcommand follows
        self.assertEqual(cmd[1:4], ["-m", "htdemucs", "four-stems"])
        self.assertIn("--model", cmd) and self.assertEqual(cmd[cmd.index("--model") + 1], "htdemucs")
        self.assertIn("--audio", cmd) and self.assertIn("in.wav", cmd)
        self.assertIn("--out", cmd) and self.assertIn("/work", cmd)
        self.assertEqual(cmd[cmd.index("--threads") + 1], "2")

    def test_no_threads_flag_by_default(self):
        cmd = c.build_htdemucs_four_stems_cmd("python", "in.wav", "/work")
        self.assertNotIn("--threads", cmd)


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


class SeparateStemsFailureTests(unittest.TestCase):
    def test_unknown_engine_raises_valueerror_before_any_run(self):
        # real env: a python interpreter exists; bogus engine hits the else branch
        with self.assertRaises(ValueError):
            c.separate_stems("song.wav", engine="bogus")

    def test_no_python_raises_runtimeerror(self):
        import unittest.mock as m
        with m.patch("core.find_python", return_value=None), \
             m.patch("core._dir_has_any", return_value=True):
            with self.assertRaises(RuntimeError) as ctx:
                c.separate_stems("song.wav", engine=c.ENGINE_UVR5, uvr_root="/x")
        self.assertIn("no python interpreter", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
