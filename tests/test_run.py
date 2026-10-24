"""Unit tests for run.py's orchestration logic.

These cover the pure/testable layers: stage command building, output naming, input
gathering + dedup across URL forms, engine-resolution override, and CLI dry-run wiring.
They deliberately avoid invoking yt-dlp / ffmpeg / separation (network + GPU heavy)."""
import argparse
import os
import sys
import unittest
import unittest.mock as mock


# src-layout: make the ``auto_convert`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
import sys as _shim_sys  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from auto_convert import run as run_mod


class DownloadCmdTests(unittest.TestCase):
    def test_ytdlp_extract_flags_and_order(self):
        cmd = run_mod.build_download_cmd("yt-dlp.exe", "dQw4w9WgXcQ", "/tmp/work")
        self.assertEqual(cmd[0], "yt-dlp.exe")
        self.assertEqual(cmd[1], "dQw4w9WgXcQ")
        self.assertIn("--extract-audio", cmd)
        self.assertIn("--audio-format", cmd) and self.assertEqual(cmd[cmd.index("--audio-format") + 1], "best")
        idx = cmd.index("-o")
        self.assertEqual(cmd[idx:idx + 2], ["-o", os.path.join("/tmp/work", "audio")])


class MixCmdTests(unittest.TestCase):
    def test_two_channel_mix_of_three_stems(self):
        out = "/x/out_no_drums.wav"
        cmd = run_mod.build_mix_cmd("ffmpeg.exe", "bass.wav", "other.wav", "vocals.wav", out)
        self.assertEqual(cmd[0], "ffmpeg.exe")
        # input order must be bass, other, vocals (drums excluded from the mix)
        first_i = cmd.index("-i")
        files = [cmd[first_i + 1 + 2 * i] for i in range(3)]
        self.assertEqual([os.path.basename(f) for f in files], ["bass.wav", "other.wav", "vocals.wav"])
        self.assertIn("-map", cmd) and self.assertIn("-mixer_channels", cmd)
        self.assertEqual(cmd[-1], out)


class TitleHintTests(unittest.TestCase):
    def test_full_url_v_param(self):
        self.assertEqual(run_mod.extract_title_hint("https://www.youtube.com/watch?v=abcDEF12345"), "abcDEF12345")

    def test_bare_id_passthrough(self):
        self.assertEqual(run_mod.extract_title_hint("dQw4w9WgXcQ"), "dQw4w9WgXcQ")


class GatherInputsTests(unittest.TestCase):
    def _ns(self, input_list=None):
        return argparse.Namespace(input=input_list)

    def test_dedup_across_url_and_bare_forms(self):
        ns = self._ns(["https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"])
        got = run_mod.gather_inputs(ns)  # stdin isatty True in test -> no stdin read
        self.assertEqual(got, ["dQw4w9WgXcQ"])

    def test_keeps_distinct_ids_in_order(self):
        ns = self._ns(["aBcDeFg1234", "zYxWvUt5678"])
        self.assertEqual(run_mod.gather_inputs(ns), ["aBcDeFg1234", "zYxWvUt5678"])


class ResolveEngineTests(unittest.TestCase):
    def test_auto_uses_available_engine(self):
        with mock.patch("auto_convert.core.detect_engine", return_value="uvr5"):
            self.assertEqual(run_mod.resolve_engine(argparse.Namespace(engine="auto"), "", ""), "uvr5")

    def test_explicit_override_rejected_if_unavailable(self):
        # request uvr5 but only htdemucs is available -> None (engine unusable)
        with mock.patch("auto_convert.core.detect_engine", return_value="htdemucs"):
            self.assertIsNone(run_mod.resolve_engine(argparse.Namespace(engine="uvr5"), "", ""))

    def test_explicit_htdemucs_accepted_when_available(self):
        with mock.patch("auto_convert.core.detect_engine", return_value="htdemucs"):
            self.assertEqual(run_mod.resolve_engine(argparse.Namespace(engine="htdemucs"), "", ""), "htdemucs")


class MainDryRunTests(unittest.TestCase):
    def test_dry_run_exits_zero_without_network(self):
        old = sys.stdin
        try:
            # force isatty True so gather_inputs never blocks on a real stdin read
            with mock.patch.object(sys, "stdin", mock.MagicMock(isatty=lambda: True)):
                rc = run_mod.main(["-i", "dQw4w9WgXcQ", "-i", "https://youtu.be/abcDEF12345", "--dry-run"])
            self.assertEqual(rc, 0)
        finally:
            sys.stdin = old


class RunPipelineEmptyTests(unittest.TestCase):
    def test_empty_inputs_is_noop(self):
        args = argparse.Namespace(output_dir="output", replace_existing=False, threads=0)
        self.assertEqual(run_mod.run_pipeline([], args), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
