from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
import torch

from jamly import core, demucs_worker


class DemucsContractTests(unittest.TestCase):
    def test_worker_preserves_unscaled_float_estimates(self):
        raw = torch.tensor([[1.4, -1.3, .2], [1.1, -.5, .1]])
        with tempfile.TemporaryDirectory() as directory:
            with patch("demucs.api.Separator") as separator:
                separator.return_value.samplerate = 44100
                separator.return_value.separate_audio_file.return_value = (
                    raw, {name: raw for name in ("drums", "bass", "other", "vocals")}
                )
                demucs_worker.main(["--out", directory, "--device", "cpu", "-n", "htdemucs_ft", "source.wav"])
                self.assertEqual(separator.call_args.kwargs["model"], "htdemucs_ft")
            for name in ("drums", "bass", "other", "vocals"):
                path = Path(directory) / (name + ".wav")
                self.assertEqual(sf.info(path).subtype, "FLOAT")
                samples, rate = sf.read(path, dtype="float32")
                self.assertEqual(rate, 44100)
                np.testing.assert_array_equal(samples, raw.numpy().T)

    def test_runner_uses_isolated_worker_and_explicit_device(self):
        with tempfile.TemporaryDirectory() as directory:
            def execute(command, **_kwargs):
                output = Path(command[command.index("--out") + 1])
                output.mkdir()
                for name in ("drums", "bass", "other", "vocals"):
                    (output / (name + ".wav")).write_bytes(b"placeholder")
                return core.RunResult(command, 0, "", "")
            with patch.object(core, "run", side_effect=execute) as called:
                stems = core.separate_stems("audio.wav", directory, device="cpu",
                                           ffmpeg_dir="/configured/ffmpeg", model="htdemucs_ft")
            command = called.call_args.args[0]
            self.assertIn("jamly.demucs_worker", command)
            self.assertEqual(command[command.index("--device") + 1], "cpu")
            self.assertEqual(command[command.index("-n") + 1], "htdemucs_ft")
            self.assertTrue(called.call_args.kwargs["env"]["PATH"].startswith("/configured/ffmpeg"))
            self.assertEqual(set(stems), {"drums", "bass", "other", "vocals"})

    def test_missing_stem_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(core, "run", return_value=core.RunResult([], 0, "", "")):
                with self.assertRaisesRegex(RuntimeError, "all four stems"):
                    core.separate_stems("audio.wav", directory)
