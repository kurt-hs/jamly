"""Unit tests for run.py's orchestration logic.

These cover the pure/testable layers: stage command building, output naming, input
gathering + dedup across URL forms, engine-resolution override, and CLI dry-run wiring.
They deliberately avoid invoking yt-dlp / ffmpeg / separation (network + GPU heavy)."""
import argparse
import os
import sys
import tempfile
import unittest
import unittest.mock as mock


# src-layout: make the ``auto_convert`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
import sys as _shim_sys  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from auto_convert import run as run_mod
from auto_convert.core import RunResult   # for finalize_output() test helpers
import auto_convert.provision as p
from auto_convert.config_state import ConfigStore as cs


class DownloadCmdTests(unittest.TestCase):
    def test_ytdlp_extract_flags_and_order(self):
        cmd = run_mod.build_download_cmd("yt-dlp.exe", "dQw4w9WgXcQ", "/tmp/work")
        self.assertEqual(cmd[0], "yt-dlp.exe")
        self.assertEqual(cmd[1], "dQw4w9WgXcQ")
        self.assertIn("--extract-audio", cmd)
        self.assertIn("--audio-format", cmd) and self.assertEqual(cmd[cmd.index("--audio-format") + 1], "best")
        # the extract shares the watch-id prefix (not a literal "audio"), so it matches the stems
        idx = cmd.index("-o")
        self.assertEqual(cmd[idx:idx + 2], ["-o", os.path.join("/tmp/work", "dQw4w9WgXcQ")])


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


class NamingConsistencyTests(unittest.TestCase):
    """All three outputs for one item share the watch-id prefix; stems carry indicative suffixes."""

    def test_stem_output_paths_share_watch_id_prefix(self):
        drums, nodrums, meta = run_mod.stem_output_paths("/out", "dQw4w9WgXcQ", "mp3")
        # same common prefix on every output; stems get an indicative suffix; meta mirrors the id
        self.assertEqual(os.path.basename(drums), "dQw4w9WgXcQ_drums.mp3")
        self.assertEqual(os.path.basename(nodrums), "dQw4w9WgXcQ_no_drums.mp3")
        self.assertEqual(os.path.basename(meta), "dQw4w9WgXcQ.meta.json")
        stem = os.path.splitext(drums)[0]
        self.assertIn("_drums", stem) and self.assertTrue(stem.startswith("dQw4w9WgXcQ"))
        self.assertTrue(nodrums.startswith(os.path.join("/out", "dQw4w9WgXcQ")))

    def test_process_one_writes_consistent_names(self):
        # end-to-end naming through process_one: the yt-dlp extract and both stems must share the
        # watch-id prefix, with <_drums / _no_drums> suffixes on the stems. Uses fmt=wav so the
        # finalizer copies verbatim (no encoder) -- deterministic and subprocess-free.
        wid = "dQw4w9WgXcQ"
        workdir = tempfile.mkdtemp()
        out_dir = tempfile.mkdtemp()
        extract_name = "%s.webm" % wid   # whatever yt-dlp emits; naming only needs the prefix
        fmt = "wav"

        def fake_stream_download(cmd):
            # the download step lands <wid>.<ext> in workdir (same dir process_one created)
            with open(os.path.join(workdir, extract_name), "wb") as fh:
                fh.write(b"RIFF\x00\x00\x00\x00WAVEfake-extract")
            return RunResult(args=list(cmd), returncode=0, stdout="", stderr="")

        def fake_separate(_audio, engine, _workdir, threads, uvr_root=""):
            for name in ("drums", "bass", "other", "vocals"):
                p = os.path.join(workdir, "%s.wav" % name)
                with open(p, "wb") as fh:
                    fh.write(f"RIFF\x00\x00\x00\x00WAVEfake-stem-{name}".encode())
            return {"drums": os.path.abspath(os.path.join(workdir, "drums.wav")),
                    "bass": os.path.abspath(os.path.join(workdir, "bass.wav")),
                    "other": os.path.abspath(os.path.join(workdir, "other.wav")),
                    "vocals": os.path.abspath(os.path.join(workdir, "vocals.wav"))}

        args = argparse.Namespace(audio_format=fmt, threads=0, output_dir=out_dir, replace_existing=True)
        mix_cmd = None
        with mock.patch.object(run_mod, "make_workdir", return_value=workdir), \
             mock.patch.object(run_mod, "_find_ytdlp", return_value="fake-ytdlp"), \
             mock.patch.object(run_mod, "_stream_download", fake_stream_download), \
             mock.patch.object(run_mod, "separate", fake_separate), \
             mock.patch.object(run_mod, "_run") as _run_mock:
            res = run_mod.process_one(wid, args)
        # capture the no-drums mix command while the _run mock is still in place (restored on exit)
        if _run_mock.call_count == 1:
            mix_cmd = list(_run_mock.call_args.args[0])

        self.assertTrue(res.ok, res.error)
        # the download step lands <wid>.<ext> in workdir; both stems + the meta share that prefix
        self.assertEqual(os.path.basename(extract_name), extract_name)  # <wid>.webm
        drums_out = res.drums_path; nodrums_out = res.nodrums_path; meta_path = res.meta_path
        for path, expected in [(drums_out, "%s_drums.wav" % wid),
                               (nodrums_out, "%s_no_drums.wav" % wid),
                               (meta_path, "%s.meta.json" % wid)]:
            self.assertEqual(os.path.basename(path), expected)

        # fmt=wav -> the finalizer copies the drums stem verbatim to disk; assert that file exists,
        # and that its basename carries the <_drums> suffix.
        self.assertTrue(os.path.exists(drums_out), "expected output on disk: %s" % drums_out)

        # the no-drums mix was dispatched by _run (mocked here). Capture it to confirm the mix is
        # built from bass/other/vocals into exactly nodrums_out -- and that 'drums' is NOT mixed in.
        self.assertIsNotNone(mix_cmd, "_run must have been called with the no-drums mix command")
        stem_inputs = [mix_cmd[i + 1] for i, tok in enumerate(mix_cmd)
                       if tok == "-i" and mix_cmd[i + 1] not in ("-nostdin",)]
        self.assertEqual(len(stem_inputs), 3)                     # exactly three inputs: bass/other/vocals
        self.assertEqual({os.path.basename(s) for s in stem_inputs}, {"bass.wav", "other.wav", "vocals.wav"})
        self.assertNotIn("drums.wav", (os.path.basename(s) for s in stem_inputs))  # drum stem must never be mixed in
        self.assertEqual(mix_cmd[-1], nodrums_out)                # the no-drums path is the mix's output
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


class ResolveRunEnvReuseTests(unittest.TestCase):
    """_resolve_run_env prefers a ConfigStore-recorded portable ffmpeg over rescanning."""

    def _seed_recorded_ffmpeg(self, scope):
        names = ("ffmpeg.exe", "ffprobe.exe") if p.PLATFORM_WINDOWS else ("ffmpeg", "ffprobe")
        bin_dir = os.path.join(p.ffmpeg_bin_dir(scope), names[0])
        os.makedirs(os.path.dirname(bin_dir), exist_ok=True)
        open(bin_dir, "w").close()
        rel = os.path.relpath(bin_dir, scope)
        store = cs.create()
        store.record("ffmpeg", paths=[rel], preferred=True)
        return bin_dir, rel

    def test_prefers_recorded_path_over_find_ffmpeg(self):
        with tempfile.TemporaryDirectory() as scope:
            bin_dir, rel = self._seed_recorded_ffmpeg(scope)
            recorded_abs = os.path.abspath(bin_dir)
            # resolve_recorded_tools() itself is unit-tested in tests/test_provision.py; here we
            # assert _resolve_run_env consumes a ConfigStore-recorded ffmpeg and skips the scan.
            with mock.patch.object(run_mod, "resolve_recorded_tools", return_value={"ffmpeg": recorded_abs}), \
                 mock.patch.object(run_mod, "find_ffmpeg", side_effect=RuntimeError("must not scan when reused")), \
                 mock.patch.object(run_mod, "resolve_engine", return_value="uvr5"):
                _s, _e, ffmpeg_dir, _u = run_mod._resolve_run_env(argparse.Namespace(engine="auto", threads=0))
            self.assertEqual(os.path.normcase(os.path.abspath(ffmpeg_dir)),
                             os.path.normcase(recorded_abs))

    def test_falls_back_to_find_ffmpeg_when_no_record(self):
        with tempfile.TemporaryDirectory() as scope:
            with mock.patch.dict(os.environ, {"AUTOCV_SCOPE": scope}), \
                 mock.patch.object(run_mod, "find_ffmpeg", return_value="/nonexistent/ffmpeg") as ff, \
                 mock.patch.object(run_mod, "resolve_engine", return_value="uvr5"):
                _s, _e, ffmpeg_dir, _u = run_mod._resolve_run_env(argparse.Namespace(engine="auto", threads=0))
            self.assertEqual(ffmpeg_dir, "/nonexistent/ffmpeg")
            ff.assert_called_once()   # no recorded path -> normal scan runs


class FinalizeOutputTests(unittest.TestCase):
    """encode-or-copy decision for one final stem (lossy re-encode vs raw verbatim copy)."""

    def _write_fake_wav(self, path):
        with open(path, "wb") as fh:
            fh.write(b"RIFF\x00\x00\x00\x00WAVEfake-stem-audio-bytes-for-finalize-test")
        return os.path.abspath(path)

    def test_raw_wav_copies_verbatim_and_never_calls_ffmpeg(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._write_fake_wav(os.path.join(d, "drums.wav"))
            dst = os.path.join(d, "drums.mp3")   # extension is irrelevant for raw output
            called = mock.MagicMock()
            with mock.patch.object(run_mod, "_run", called):
                ok = run_mod.finalize_output("ffmpeg.exe", src, dst, fmt="wav")
            self.assertTrue(ok)
            self.assertFalse(called.called)                 # no encoder needed for raw WAV
            self.assertEqual(open(src, "rb").read(), open(dst, "rb").read())  # verbatim copy

    def test_lossy_mp3_reencodes_via_run(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._write_fake_wav(os.path.join(d, "drums.wav"))
            dst = os.path.join(d, "drums.mp3")
            called = mock.MagicMock(return_value=RunResult(args=["ffmpeg.exe"], returncode=0, stdout="", stderr=""))
            with mock.patch.object(run_mod, "_run", called):
                ok = run_mod.finalize_output("no-such-ffmpeg", src, dst, fmt="mp3")
            self.assertTrue(ok)
            self.assertTrue(called.called)
            cmd = called.call_args[0][0]
            self.assertEqual(cmd[0], "no-such-ffmpeg")
            self.assertIn("libmp3lame", cmd) and self.assertIn("320k", cmd)
            self.assertEqual(cmd[-1], dst)

    def test_lossy_encode_failure_raises(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._write_fake_wav(os.path.join(d, "drums.wav"))
            dst = os.path.join(d, "drums.mp3")
            with mock.patch.object(run_mod, "_run", return_value=RunResult(args=["ffmpeg"], returncode=1, stdout="", stderr="")):
                with self.assertRaises(RuntimeError):
                    run_mod.finalize_output("ffmpeg.exe", src, dst, fmt="mp3")

    def test_lossy_missing_ffmpeg_raises(self):
        # a non-zero exit (e.g. FileNotFoundError -> RunResult.ok False) must not silently pass
        with tempfile.TemporaryDirectory() as d:
            src = self._write_fake_wav(os.path.join(d, "drums.wav"))
            dst = os.path.join(d, "drums.mp3")
            with mock.patch.object(run_mod, "_run", return_value=RunResult(args=["bogus.exe"], returncode=-1, stdout="", stderr="")):
                with self.assertRaises(RuntimeError):
                    run_mod.finalize_output("bogus.exe", src, dst, fmt="mp3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
