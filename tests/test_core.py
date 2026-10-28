import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

from jamly import core


class CommandTests(unittest.TestCase):
    def test_download_keeps_flags_before_leading_dash_source(self):
        with patch("jamly.bootstrap.ytdlp_options", return_value=["--ignore-config"]):
            command = core.build_ytdlp_extract_cmd(
                [sys.executable, "-m", "yt_dlp"], "-abcdefghij",
                "audio.%(ext)s", "tools",
            )
        self.assertEqual(command[:3], [sys.executable, "-m", "yt_dlp"])
        self.assertEqual(command[-2:], ["--", "-abcdefghij"])
        self.assertEqual(command[command.index("--ffmpeg-location") + 1], "tools")
        self.assertEqual(command[command.index("-f") + 1], "bestaudio/best")
        self.assertLess(command.index("-o"), command.index("--"))

    def test_encoder_uses_same_bitrate_for_each_output(self):
        for stem in ("drums", "no_drums"):
            command = core.build_stem_encode_cmd("ffmpeg", stem + ".wav", stem + ".mp3")
            self.assertEqual(command[command.index("-c:a") + 1], "libmp3lame")
            self.assertEqual(command[command.index("-b:a") + 1], "320k")
        with self.assertRaises(ValueError):
            core.build_stem_encode_cmd("ffmpeg", "audio.wav", "audio.bad", "bad")


class ProcessTests(unittest.TestCase):
    def test_capture_and_stream(self):
        result = core.run([sys.executable, "-c", "print('captured')"])
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout.strip(), "captured")
        lines = []
        result = core.run([sys.executable, "-c", "print('streamed')"], stream=True,
                          on_line=lines.append)
        self.assertTrue(result.ok)
        self.assertEqual(lines, ["streamed\n"])
        self.assertEqual(result.stdout, "")

    def test_failure_retains_diagnostic(self):
        with self.assertRaisesRegex(RuntimeError, "failure-detail"):
            core.run([sys.executable, "-c",
                      "import sys; print('failure-detail',file=sys.stderr);sys.exit(3)"], check=True)
        self.assertFalse(core.run(["/no/such/jamly-tool"]).ok)

    def test_cancellation_reaps_the_command(self):
        stop = threading.Event()
        timer = threading.Timer(0.2, stop.set)
        timer.start()
        start = time.monotonic()
        try:
            with self.assertRaises(InterruptedError):
                core.run([sys.executable, "-c", "import time;time.sleep(60)"], cancel_event=stop)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - start, 5)

    def test_timeout_reaps_the_command(self):
        with self.assertRaises(TimeoutError):
            core.run([sys.executable, "-c", "import time;time.sleep(60)"], timeout=0.2)


class FilenameTests(unittest.TestCase):
    def test_unsafe_names_and_reserved_devices(self):
        self.assertEqual(core.sanitize_filename('My Title/<year>: "test".mp4'), "My Title-year test.mp4")
        self.assertEqual(core.sanitize_filename("CON"), "_CON")
        self.assertEqual(core.sanitize_filename("a\0b"), "ab")
        self.assertNotIn(os.sep, core.sanitize_filename("a/b\\c"))

    def test_unicode_byte_budget_and_empty_name(self):
        self.assertLessEqual(len(core.sanitize_filename("\U0001f941" * 100).encode()), 180)
        self.assertEqual(core.sanitize_filename("..."), "untitled")
