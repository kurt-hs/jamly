"""Explicit real-model check using generated audio; no copyrighted media or external source site."""
import functools
import http.server
import json
from pathlib import Path
import tempfile
import threading
import unittest

import numpy as np
import soundfile as sf

from jamly.normalize import make_input
from jamly.run import build_parser, process_one


class RealPipelineTests(unittest.TestCase):
    def test_download_demucs_and_paired_float_export(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            samples = .2 * np.sin(np.arange(11025) * 2 * np.pi * 440 / 44100)
            sf.write(root / "generated.wav", np.stack((samples, samples), axis=1), 44100, subtype="FLOAT")
            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                source = "http://127.0.0.1:%d/generated.wav" % httpd.server_address[1]
                item = make_input(source)
                args = build_parser().parse_args(["-o", str(root / "outputs"), "--audio-format", "wav",
                                                  "--device", "cpu"])
                result = process_one(item.watch_id, args, source_url=source)
                self.assertEqual(result.status, "done", result.error)
                first, second = sf.info(result.drums_path), sf.info(result.nodrums_path)
                self.assertEqual((first.frames, first.channels, first.samplerate),
                                 (second.frames, second.channels, second.samplerate))
                self.assertEqual((first.subtype, second.subtype), ("FLOAT", "FLOAT"))
                metadata = json.loads(Path(result.meta_path).read_text())
                self.assertLessEqual(metadata["audio"]["peak"], .999001)
                self.assertEqual(process_one(item.watch_id, args, source_url=source).status, "skipped")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join()
