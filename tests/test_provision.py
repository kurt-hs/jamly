"""Unit tests for provision.py. No real network: fetch is faked via a bytes opener."""
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile


# src-layout: make the ``splitfy`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from splitfy import bootstrap as b
import splitfy.config_state as cs
import splitfy.provision as p

# Captured at import time (before any test monkeypatches module-level fetch_to_bytes) so later tests
# can delegate to the pristine function regardless of cross-test opener pollution.
_real_fetch = p.fetch_to_bytes


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
            self.assertEqual(res["sources"], {"ffmpeg": "reused", "ffprobe": "reused"})

    def test_origin_of_existing_install(self):
        with tempfile.TemporaryDirectory() as d:
            names = ("ffmpeg.exe", "ffprobe.exe") if b.PLATFORM_WINDOWS else ("ffmpeg", "ffprobe")
            for n in names:
                open(os.path.join(d, n), "w").close()
            self.assertEqual(p._origin_of("ffmpeg", [d]), "reused")
            # a dir with only one binary is NOT a reusable install root -> falls through to download
            only = os.path.join(tempfile.mkdtemp(), names[0])
            open(only, "w").close()
            self.assertEqual(p._origin_of("ffmpeg", [os.path.dirname(only)]), "")

    def test_downloaded_origin_when_no_existing(self):
        with tempfile.TemporaryDirectory() as scope:
            # fake opener returns the crafted zip; no existing install -> both marked 'downloaded'
            p.fetch_to_bytes = lambda url, opener=None, timeout=30: _zip_bytes(["bin/ffmpeg.exe", "bin/ffprobe.exe"])
            res = p.provision(scope, existing_dirs=[])
            self.assertTrue(res["provisioned"] and res["downloaded"])
            self.assertEqual(res["sources"], {"ffmpeg": "downloaded", "ffprobe": "downloaded"})

    def test_missing_binary_downgraded_to_missing(self):
        with tempfile.TemporaryDirectory() as scope:
            # a fake opener 'downloads' so we never touch the network; extract_build is a no-op
            # (we staged ffmpeg manually) leaving ffprobe absent -> its origin downgrades to missing
            p.fetch_to_bytes = lambda url, opener=None, timeout=30: _zip_bytes(["bin/ffmpeg.exe"])
            dest = p.ffmpeg_bin_dir(scope)
            os.makedirs(dest, exist_ok=True)
            names = ("ffmpeg.exe", "ffprobe.exe") if b.PLATFORM_WINDOWS else ("ffmpeg", "ffprobe")
            open(os.path.join(dest, names[0]), "w").close()
            real_extract = p.extract_build
            p.extract_build = lambda zip_path, dest_dir: None  # no-op; dir staged manually
            try:
                res = p.provision(scope)
            finally:
                p.extract_build = real_extract
            # ffprobe is absent on disk -> the pair isn't fully provisioned, and its origin is 'missing'
            self.assertFalse(res["provisioned"])
            self.assertEqual(res["sources"]["ffmpeg"], "downloaded")   # present -> keeps download origin
            self.assertEqual(res["sources"]["ffprobe"], "missing")    # absent -> downgraded


class ConfigStoreReuseTests(unittest.TestCase):
    """ConfigStore reuse-first persistence (record_provision / resolve_recorded_tools)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    @staticmethod
    def _extract_into_scope(scope):
        dest = p.ffmpeg_bin_dir(scope)
        os.makedirs(dest, exist_ok=True)
        open(os.path.join(dest, "ffmpeg"), "w").write("#!/bin/sh\n")
        open(os.path.join(dest, "ffprobe"), "w").write("#!/bin/sh\n")

    def test_record_then_resolve_roundtrip(self):
        with tempfile.TemporaryDirectory() as scope:
            self._extract_into_scope(scope)
            tools = {"ffmpeg": p.ffmpeg_bin_dir(scope) + "/ffmpeg",
                     "ffprobe": p.ffmpeg_bin_dir(scope) + "/ffprobe"}
            store = cs.ConfigStore.create()
            recorded = p.record_provision(store, scope, tools)
            self.assertEqual(sorted(recorded), sorted(["ffmpeg", "ffprobe"]))
            # stored relative to scope, marked preferred/portable
            self.assertTrue(store.has("ffmpeg") and store.has("ffprobe"))
            self.assertTrue(os.path.relpath(tools["ffmpeg"], scope).split(os.sep)[0].startswith("."))
            self.assertTrue(store.get_field("ffmpeg", "preferred") is True)
            self.assertEqual(store.get_field("ffmpeg", "origin"), "portable")

            resolved = p.resolve_recorded_tools(scope, store)
            self.assertEqual(sorted(resolved), sorted(["ffmpeg", "ffprobe"]))
            for tool in resolved:
                self.assertTrue(os.path.isfile(resolved[tool]), tool)
                self.assertTrue(resolved[tool].startswith(os.path.abspath(scope)))

    def test_resolve_missing_returns_empty(self):
        # recorded path that does NOT exist on disk must not be trusted (no false reuse)
        store = cs.ConfigStore.create()
        store.record("ffmpeg", paths=[".portable/win64/ffmpeg/bin/nonexistent"])
        self.assertEqual(p.resolve_recorded_tools(self._tmp, store), {})

    def test_resolve_absolute_entry_when_present(self):
        abs_path = os.path.join(self._tmp, "sys-ffmpeg")
        open(abs_path, "w").close()
        store = cs.ConfigStore.create()
        store.record("ffprobe", paths=[abs_path])
        resolved = p.resolve_recorded_tools(self._tmp, store)
        self.assertEqual(resolved, {"ffprobe": _abs(abs_path)})

    def test_record_only_succeeding_tool(self):
        with tempfile.TemporaryDirectory() as scope:
            # only ffmpeg present; ffprobe missing -> record + resolve must drop it
            dest = p.ffmpeg_bin_dir(scope)
            os.makedirs(dest, exist_ok=True)
            open(os.path.join(dest, "ffmpeg"), "w").close()
            tools = {"ffmpeg": os.path.join(dest, "ffmpeg")}  # ffprobe omitted on purpose
            store = cs.ConfigStore.create()
            recorded = p.record_provision(store, scope, tools)
            self.assertEqual(recorded, ["ffmpeg"])
            resolved = p.resolve_recorded_tools(scope, store)
            self.assertEqual(sorted(resolved), ["ffmpeg"])


def _abs(path):
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))




class IntegrityTests(unittest.TestCase):
    """Download-integrity layer for provisioned builds (S3 audit follow-up)."""

    def test_sha256_hex_known_vector(self):
        # RFC 3174 vector; also exercises the empty-input edge case.
        self.assertEqual(
            p._sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )
        self.assertEqual(len(p._sha256_hex(b"")), 64)   # fixed-width lowercase hex

    def _body(self, payload):
        # read-only response: no _data attr so fetch_to_bytes takes the resp.read() path.
        return type("Body", (), {"read": lambda s: payload})()

    def test_fetch_rejects_wrong_digest(self):
        # other tests monkeypatch module-level fetch_to_bytes (with a fixed body) and leave it set;
        # delegate through a url-passing wrapper so the opener we inject controls what comes back.
        def _delegate(url, opener=None, timeout=30, expected_sha256=None):
            return _real_fetch(url=url, opener=opener, timeout=timeout, expected_sha256=expected_sha256)
        p.fetch_to_bytes = _delegate

        data = b"\x00" * 16
        expected = p._sha256_hex(data)
        # correct digest -> passes through unchanged (resp.read() path; no _data attr)
        self.assertEqual(p.fetch_to_bytes("u", opener=lambda url, timeout=30: self._body(data)), data)
        # wrong digest with expected pinned -> fail-loud FetchError (never returns the body)
        try:
            p.fetch_to_bytes(
                "u",
                opener=lambda url, timeout=30: self._body(b"tampered"),
                expected_sha256=expected,
            )
            self.fail("expected FetchError on digest mismatch")
        except p.FetchError:
            pass

    def test_provision_records_integrity_on_download(self):
        with tempfile.TemporaryDirectory() as scope:
            body = _zip_bytes(["bin/ffmpeg.exe", "bin/ffprobe.exe"])
            # no env pin -> default flow unaffected; integrity is still recorded next to the build
            p.fetch_to_bytes = lambda url, opener=None, timeout=30: body
            res = p.provision(scope)
            self.assertTrue(res["downloaded"])
            rec_path = p._integrity_path(scope, b._os_family())
            self.assertTrue(os.path.isfile(rec_path))
            import json as _json
            recorded = _json.load(open(rec_path))
            self.assertEqual(recorded["sha256"], p._sha256_hex(body))
            # expected asset name is platform-derived (not hard-coded), matching provision()'s own
            # build_asset_name call for this box's os/arch.
            osf = b._os_family()
            exp_arch = {"win": p._win_arch(), "linux": p._linux_arch()}.get(osf)
            self.assertEqual(recorded["asset"], p.build_asset_name(osf, exp_arch))
