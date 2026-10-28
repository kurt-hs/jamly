import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jamly import bootstrap


class InstalledRuntimeTests(unittest.TestCase):
    def test_ytdlp_uses_own_interpreter_without_global_path(self):
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertTrue(bootstrap.find_ytdlp().ok)
            self.assertEqual(bootstrap.ytdlp_command(), [sys.executable, "-m", "yt_dlp"])

    def test_missing_package_is_not_substituted_from_path(self):
        with patch.object(bootstrap.importlib.util, "find_spec", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "yt-dlp is missing"):
                bootstrap.ytdlp_command()

    def test_deno_is_found_in_installed_environment(self):
        with patch.dict(os.environ, {"PATH": ""}):
            deno = bootstrap.find_deno()
            self.assertTrue(deno.ok)
            self.assertIn("deno:" + deno.path, bootstrap.ytdlp_options())

    def test_doctor_is_not_ready_without_javascript(self):
        with patch.object(bootstrap, "find_deno", return_value=bootstrap.ToolStatus("deno")):
            self.assertFalse(bootstrap.summarize().ready)

    def test_ffmpeg_requires_both_executables(self):
        with tempfile.TemporaryDirectory() as root:
            suffix = ".exe" if os.name == "nt" else ""
            for name in ("ffmpeg", "ffprobe"):
                path = Path(root) / (name + suffix)
                path.write_bytes(b"fake")
                path.chmod(0o755)
            with patch.dict(os.environ, {"PATH": ""}):
                self.assertTrue(bootstrap.detect_ffmpeg([root]).ok)
                (Path(root) / ("ffprobe" + suffix)).unlink()
                self.assertFalse(bootstrap.detect_ffmpeg([root]).ok)
