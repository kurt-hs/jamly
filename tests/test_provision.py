"""Unit tests for provision.py. No real network: fetch is faked via a bytes opener."""
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootstrap as b  # noqa: E402
import provision as p


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in entries:
            zf.writestr(name, b"")
    return buf.getvalue()


class AssetNameTests(unittest.TestCase):
    def test_windows_name_matches_btbN(self):
        self.assertEqual(p.build_asset_name("win", "win64"), "ffmpeg-master-latest-win64-gpl.zip")

    def test_linux_name_is_tarball(self):
        self.assertTrue(p.build_asset_name("linux", "linux").endswith("-gpl.tar.xz"))


class ExtractTests(unittest.TestCase):
    @staticmethod
    def _write_zip(entries, tmp):
        zp = os.path.join(tmp, "build.zip")
        open(zp, "wb").write(_zip_bytes(entries))
        return zp

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_places_both_binaries_in_bin_dir(self):
        dest = os.path.join(self._tmp, "build")
        zp = self._write_zip(["bin/ffmpeg.exe", "bin/ffprobe.exe"], self._tmp)
        res = p.extract_build(zp, dest)
        self.assertEqual(sorted(os.listdir(os.path.dirname(res["ffmpeg"]))),
                         sorted(["ffmpeg.exe", "ffprobe.exe"]))

    def test_missing_binary_raises(self):
        dest = os.path.join(self._tmp, "build")
        zp = self._write_zip(["bin/only-ffmpeg.exe"], self._tmp)
        with self.assertRaises(p.FetchError):
            p.extract_build(zp, dest)


class ProvisionTests(unittest.TestCase):
    def test_fake_download_populates_scope(self):
        data = _zip_bytes(["bin/ffmpeg.exe", "bin/ffprobe.exe"])

        # inject a fake opener that returns the crafted zip bytes (no real network)
        p.fetch_to_bytes = lambda url, opener=None, timeout=30: data
        with tempfile.TemporaryDirectory() as scope:
            res = p.provision(scope)
            self.assertTrue(res["downloaded"])
            d = p.ffmpeg_bin_dir(scope)
            names = ("ffmpeg.exe", "ffprobe.exe") if b.PLATFORM_WINDOWS else ("ffmpeg", "ffprobe")
            for n in names:
                self.assertTrue(os.path.isfile(os.path.join(d, n)), n)

    def test_reuse_when_already_present(self):
        with tempfile.TemporaryDirectory() as scope:
            d = p.ffmpeg_bin_dir(scope)
            os.makedirs(d, exist_ok=True)
            names = ("ffmpeg.exe", "ffprobe.exe") if b.PLATFORM_WINDOWS else ("ffmpeg", "ffprobe")
            for n in names:
                open(os.path.join(d, n), "w").close()
            res = p.provision(scope)  # reused before any fetch -> no opener needed
            self.assertTrue(res["provisioned"])
            self.assertFalse(res["downloaded"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
