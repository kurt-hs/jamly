from pathlib import Path
import shutil
import tempfile
import unittest

import numpy as np
import soundfile as sf

from jamly.audio import mix_pair, render_pair


class PairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stems = {}
        self.wave = np.sin(np.arange(44100) * 2 * np.pi * 440 / 44100)
        for name, gain in (("drums", .3), ("bass", .55), ("other", .55), ("vocals", .55)):
            path = self.root / (name + ".wav")
            sf.write(path, np.stack((self.wave, self.wave), axis=1) * gain, 44100, subtype="FLOAT")
            self.stems[name] = str(path)

    def test_wav_pair_is_aligned_float_and_uses_one_gain(self):
        output = {name: self.root / ("out-" + name + ".wav") for name in ("drums", "no_drums")}
        info = render_pair(self.stems, self.root, output, "wav", "")
        self.assertLess(info["gain"], 1)
        self.assertLessEqual(info["peak"], 0.999001)
        drums, rate = sf.read(output["drums"], always_2d=True)
        backing, other_rate = sf.read(output["no_drums"], always_2d=True)
        self.assertEqual((rate, drums.shape), (other_rate, backing.shape))
        for path in output.values():
            self.assertEqual(sf.info(path).subtype, "FLOAT")
        np.testing.assert_allclose(backing, drums * 5.5, atol=2e-7)

    def test_mp3_pair_meets_decoded_peak_target(self):
        ffmpeg = shutil.which("ffmpeg")
        self.assertIsNotNone(ffmpeg, "ffmpeg is required for the audio contract tests")
        output = {name: self.root / ("out-" + name + ".mp3") for name in ("drums", "no_drums")}
        info = render_pair(self.stems, self.root, output, "mp3", ffmpeg)
        self.assertLessEqual(info["peak"], 10 ** (-1 / 20) * 1.00001)
        self.assertTrue(all(path.stat().st_size for path in output.values()))

    def test_misaligned_and_nonfinite_stems_are_rejected(self):
        sf.write(self.stems["bass"], np.zeros((10, 2)), 44100, subtype="FLOAT")
        with self.assertRaisesRegex(ValueError, "different lengths"):
            mix_pair(self.stems, self.root)
        sf.write(self.stems["bass"], np.full((44100, 2), np.nan), 44100, subtype="FLOAT")
        with self.assertRaisesRegex(ValueError, "non-finite"):
            mix_pair(self.stems, self.root)

    def test_all_non_drum_sources_are_mixed(self):
        piano = self.root / "piano.wav"
        sf.write(piano, np.stack((self.wave, self.wave), axis=1) * .1, 44100, subtype="FLOAT")
        self.stems["piano"] = str(piano)
        paths, _info = mix_pair(self.stems, self.root)
        mixed, _rate = sf.read(paths["no_drums"], always_2d=True)
        np.testing.assert_allclose(mixed[:, 0], self.wave * 1.75, atol=2e-7)
