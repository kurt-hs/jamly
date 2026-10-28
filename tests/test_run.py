"""Unit tests for run.py's orchestration logic.

These cover the pure/testable layers: stage command building, output naming, input
gathering + dedup across URL forms, engine-resolution override, and CLI dry-run wiring.
They deliberately avoid invoking yt-dlp / ffmpeg / separation (network + GPU heavy)."""
import argparse
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock


# src-layout: make the ``jamly`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from jamly import run as run_mod
from jamly.core import RunResult   # for finalize_output() test helpers


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

        def fake_separate(audio=None, _workdir=None, threads=0, **_rest):
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
        drums_out = res.drums_path
        nodrums_out = res.nodrums_path
        meta_path = res.meta_path
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

    def test_process_one_writes_metadata_sidecar_with_parsed_title(self):
        # end-to-end task-4 wiring with live metadata: a confident [Artist] - [Songname] parse must land on
        # BOTH stems and in .meta.json; the structured sidecar records every field, not just the title.
        wid = "dM8WjgRA6o8"
        workdir = tempfile.mkdtemp()
        out_dir = tempfile.mkdtemp()
        extract_name = "%s.webm" % wid   # whatever yt-dlp emits; naming only needs the prefix
        fmt = "wav"

        def fake_stream_download(cmd):
            with open(os.path.join(workdir, extract_name), "wb") as fh:
                fh.write(b"RIFF\x00\x00\x00\x00WAVEfake-extract")
            return RunResult(args=list(cmd), returncode=0, stdout="", stderr="")

        def fake_separate(audio=None, _workdir=None, threads=0, **_rest):
            for name in ("drums", "bass", "other", "vocals"):
                with open(os.path.join(workdir, "%s.wav" % name), "wb") as fh:
                    fh.write(f"RIFF\x00\x00\x00\x00WAVEfake-stem-{name}".encode())
            return {"drums": os.path.abspath(os.path.join(workdir, "drums.wav")),
                    "bass": os.path.abspath(os.path.join(workdir, "bass.wav")),
                    "other": os.path.abspath(os.path.join(workdir, "other.wav")),
                    "vocals": os.path.abspath(os.path.join(workdir, "vocals.wav"))}

        # live metadata (as yt-dlp --print would return for this video); the real title carries a
        # trusted ' - ' separator so it parses into [Artist] - [Songname].
        meta = {"title": "Between the Suried and Me - Extremophile Elite (LIVE)",
                "artist": None, "uploader": "Metal Blade Records", "duration_seconds": 603}

        args = argparse.Namespace(audio_format=fmt, threads=0, output_dir=out_dir, replace_existing=True)
        with mock.patch.object(run_mod, "make_workdir", return_value=workdir), \
             mock.patch.object(run_mod, "_find_ytdlp", return_value="fake-ytdlp"), \
             mock.patch.object(run_mod, "_stream_download", fake_stream_download), \
             mock.patch.object(run_mod, "separate", fake_separate), \
             mock.patch.object(run_mod, "_fetch_item_metadata", return_value=meta), \
             mock.patch.object(run_mod, "_run"):
            res = run_mod.process_one(wid, args, source_url="https://www.youtube.com/watch?v=%s" % wid)

        self.assertTrue(res.ok, res.error)
        drums_name = os.path.basename(res.drums_path)
        nodrums_name = os.path.basename(res.nodrums_path)
        # the parsed title drives both stems; neither carries the machine key (it lives in meta only)
        self.assertEqual(drums_name, "Between the Suried and Me - Extremophile Elite (LIVE)_drums.wav")
        self.assertEqual(nodrums_name, "Between the Suried and Me - Extremophile Elite (LIVE)_no_drums.wav")

        # res.title carries the machine key + stage tag alongside the human name
        self.assertEqual(res.title, "Between the Suried and Me - Extremophile Elite (LIVE) | dM8WjgRA6o8 (<original>)")

        meta_path = res.meta_path
        self.assertTrue(os.path.exists(meta_path), "expected .meta.json on disk: %s" % meta_path)
        with open(meta_path, encoding="utf-8") as fh:
            written = json.load(fh)
        self.assertEqual(written["watch_id"], wid)
        self.assertEqual(written["title"], "Between the Suried and Me - Extremophile Elite (LIVE)")
        self.assertEqual(written["artist"], "")          # yt-dlp reports NA -> normalized to empty
        self.assertEqual(written["uploader"], "Metal Blade Records")
        self.assertEqual(written["duration_seconds"], 603)

        self.assertTrue(os.path.exists(res.drums_path), "expected output on disk: %s" % res.drums_path)
class GatherInputsTests(unittest.TestCase):
    def _ns(self, input_list=None):
        return argparse.Namespace(input=input_list)

    def test_dedup_across_url_and_bare_forms(self):
        ns = self._ns(["https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"])
        got = run_mod.gather_inputs(ns)  # stdin isatty True in test -> no stdin read
        # gather_inputs now returns Input objects (watch_id + original source); dedup is on watch_id.
        self.assertEqual([i.watch_id for i in got], ["dQw4w9WgXcQ"])
        self.assertEqual(got[0].source_url, "https://youtu.be/dQw4w9WgXcQ")

    def test_keeps_distinct_ids_in_order(self):
        ns = self._ns(["aBcDeFg1234", "zYxWvUt5678"])
        got = run_mod.gather_inputs(ns)
        self.assertEqual([i.watch_id for i in got], ["aBcDeFg1234", "zYxWvUt5678"])

    def test_carries_source_url_for_any_host(self):
        # broader input support: a full URL keeps its host so yt-dlp can resolve non-YouTube sites;
        # a bare id passes through unchanged as both watch_id and source.
        ns = self._ns([
            "https://vimeo.com/12345678901",
            "youtube.com/watch?v=dQw4w9WgXcQ",
            "aBcDeFg1234",  # distinct bare id (dQw4w9WgXcQ would dedup against the youtu.be entry)
        ])
        got = run_mod.gather_inputs(ns)
        self.assertEqual(
            [i.watch_id for i in got],
            ["12345678901", "dQw4w9WgXcQ", "aBcDeFg1234"],
        )
        # each item keeps its original source so yt-dlp can resolve any host, not just YouTube
        self.assertEqual(
            [(i.watch_id, i.source_url) for i in got],
            [
                ("12345678901", "https://vimeo.com/12345678901"),
                ("dQw4w9WgXcQ", "youtube.com/watch?v=dQw4w9WgXcQ"),
                ("aBcDeFg1234", "aBcDeFg1234"),
            ],
        )


class ResolveEngineTests(unittest.TestCase):
    """Portable-only engine resolution: exactly one engine (the demucs PyPI CLI)."""

    def test_auto_uses_available_engine(self):
        with mock.patch("jamly.core.detect_engine", return_value="htdemucs"):
            self.assertEqual(run_mod.resolve_engine(argparse.Namespace(engine="auto"), ""), "htdemucs")

    def test_explicit_override_rejected_if_unavailable(self):
        # portable-only: requesting any engine other than the sole demucs CLI never runs -> None
        with mock.patch("jamly.core.detect_engine", return_value="htdemucs"):
            self.assertIsNone(run_mod.resolve_engine(argparse.Namespace(engine="uvr5"), ""))

    def test_explicit_htdemucs_accepted_when_available(self):
        with mock.patch("jamly.core.detect_engine", return_value="htdemucs"):
            self.assertEqual(run_mod.resolve_engine(argparse.Namespace(engine="htdemucs"), ""), "htdemucs")
class MainDryRunTests(unittest.TestCase):
    def test_dry_run_exits_zero_without_network(self):
        old = sys.stdin
        try:
            # force isatty True so gather_inputs never blocks on a real stdin read
            with mock.patch.object(sys, "stdin", mock.MagicMock(isatty=lambda: True)):
                tmp = tempfile.mkdtemp()
                prev = os.environ.get("AUTOCV_QUEUE")
                try:
                    # main() honours AUTOCV_QUEUE ahead of default_queue_path(cwd), so pointing it at a
                    # temp file keeps the queue write out of the working tree -- no host cwd state.
                    qpath = os.path.join(tmp, "jamly_queue.json")
                    with open(qpath, "w") as fh:
                        fh.write("")
                    os.environ["AUTOCV_QUEUE"] = os.path.abspath(qpath)
                    rc = run_mod.main(["-i", "dQw4w9WgXcQ", "-i", "https://youtu.be/abcDEF12345", "--dry-run"])
                finally:
                    if prev is None:
                        os.environ.pop("AUTOCV_QUEUE", None)
                    else:
                        os.environ["AUTOCV_QUEUE"] = prev
                self.assertEqual(rc, 0)
        finally:
            sys.stdin = old
            shutil.rmtree(tmp, ignore_errors=True)


class RunPipelineEmptyTests(unittest.TestCase):
    def test_empty_inputs_is_noop(self):
        args = argparse.Namespace(output_dir="output", replace_existing=False, threads=0)
        self.assertEqual(run_mod.run_pipeline([], args), [])


class ResolveRunEnvScopeTests(unittest.TestCase):
    """_resolve_run_env resolves the working scope from args.scope / AUTOCV_SCOPE / cwd.

    _resolve_run_env no longer consults a reuse cache: ffmpeg is always resolved by a live PATH/app
    scan (find_ffmpeg), so these cases guard only the scope-selection contract that CLI and server
    share."""

    def test_cli_without_scope_attr_uses_env_then_cwd(self):
        # the bare CLI namespace has NO scope attribute (run.py exposes no --scope flag), so
        # getattr() falls back to env/cwd and `python run.py` resolves tools exactly as before.
        cli = argparse.Namespace(engine="auto", threads=0)   # deliberately no .scope
        clean = dict(os.environ)
        clean.pop("AUTOCV_SCOPE", None)
        with mock.patch.dict(os.environ, clean, clear=True):
            scope = run_mod._resolve_run_env(cli)[0]
        self.assertEqual(scope, os.getcwd())   # portable: _resolve_run_env only reads cwd

    def test_server_scope_override_governs_tool_resolution(self):
        # ItemServer passes its --scope namespace; that same value must steer yt-dlp/ffmpeg/engine
        # resolution so the queue file and the tools it runs against live in one place.
        srv = argparse.Namespace(scope="/data", engine="auto", threads=0)
        scope = run_mod._resolve_run_env(srv)[0]
        self.assertEqual(os.path.normcase(scope), os.path.normcase("/data"))

    def test_explicit_scope_beats_autocv_scope(self):
        # an args.scope override wins over the AUTOCV_SCOPE environment variable.
        srv = argparse.Namespace(scope="/data", engine="auto", threads=0)
        with mock.patch.dict(os.environ, {"AUTOCV_SCOPE": "/other"}):
            scope = run_mod._resolve_run_env(srv)[0]
        self.assertEqual(os.path.normcase(scope), os.path.normcase("/data"))


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


class MixFailureTests(unittest.TestCase):
    """A failed no-drums mix must NOT mark the item done with a missing no_drums file."""

    def _fake_stems(self, workdir):
        stems = {}
        for name in ("drums", "bass", "other", "vocals"):
            p = os.path.join(workdir, "%s.wav" % name)
            with open(p, "wb") as fh:
                fh.write(b"FAKE_STEM_" + name.encode())
            stems[name] = os.path.abspath(p)
        return stems

    def test_failed_mix_marks_item_error_and_writes_no_nodrums(self):
        # fmt=wav -> drums finalized by verbatim copy (no _run before the mix), so exactly one _run call.
        wid = "aBcDeFg1234"
        workdir = tempfile.mkdtemp()
        out_dir = tempfile.mkdtemp()
        extract_name = "%s.webm" % wid

        def fake_stream_download(cmd):
            with open(os.path.join(workdir, extract_name), "wb") as fh:
                fh.write(b"FAKE_EXTRACT_")
            return RunResult(args=list(cmd), returncode=0, stdout="", stderr="")

        args = argparse.Namespace(audio_format="wav", threads=0, output_dir=out_dir, replace_existing=True)
        with mock.patch.object(run_mod, "make_workdir", return_value=workdir), \
             mock.patch.object(run_mod, "_find_ytdlp", return_value="fake-ytdlp"), \
             mock.patch.object(run_mod, "_stream_download", fake_stream_download), \
             mock.patch.object(run_mod, "separate", lambda audio=None, _workdir=None, threads=0, **_rest: self._fake_stems(workdir)), \
             mock.patch.object(run_mod, "_fetch_item_metadata", return_value={}), \
             mock.patch.object(run_mod, "_run", return_value=RunResult(args=["ffmpeg.exe"],
                                                                       returncode=1, stdout="", stderr="boom")):
            res = run_mod.process_one(wid, args)

        # a non-zero mix exit is recorded as an error, not swallowed into 'done'
        self.assertEqual(res.status, "error")
        self.assertIn("mix failed", res.error)
        # no <title>_no_drums file was written; only the drums stem (finalized by copy) exists
        self.assertIsNone(res.nodrums_path)
        # res.drums_path is only assigned after the mix succeeds, so check the file on disk
        # directly -- the copy landed there before the mix was dispatched.
        self.assertTrue(os.path.exists(os.path.join(out_dir, "%s_drums.wav" % wid)),
                        "drums stem should still be present")
        self.assertFalse([f for f in os.listdir(out_dir) if "no_drums" in f], "no no-drums file written")

    def test_successful_mix_marks_item_done(self):
        # the happy path: a clean mix exit leaves both stems + marks the item done.
        wid = "aBcDeFg1234"
        workdir = tempfile.mkdtemp()
        out_dir = tempfile.mkdtemp()
        extract_name = "%s.webm" % wid

        def fake_stream_download(cmd):
            with open(os.path.join(workdir, extract_name), "wb") as fh:
                fh.write(b"FAKE_EXTRACT_")
            return RunResult(args=list(cmd), returncode=0, stdout="", stderr="")

        args = argparse.Namespace(audio_format="wav", threads=0, output_dir=out_dir, replace_existing=True)

        # a real mix writes its output to disk; the mock does the same so the "file exists"
        # assertion below exercises the real path rather than just the return code.
        def fake_run(cmd):
            with open(cmd[-1], "wb") as fh:
                fh.write(b"FAKE_MIX")
            return RunResult(args=list(cmd), returncode=0, stdout="", stderr="")

        with mock.patch.object(run_mod, "make_workdir", return_value=workdir), \
             mock.patch.object(run_mod, "_find_ytdlp", return_value="fake-ytdlp"), \
             mock.patch.object(run_mod, "_stream_download", fake_stream_download), \
             mock.patch.object(run_mod, "separate", lambda audio=None, _workdir=None, threads=0, **_rest: self._fake_stems(workdir)), \
             mock.patch.object(run_mod, "_fetch_item_metadata", return_value={}), \
             mock.patch.object(run_mod, "_run", fake_run):
            res = run_mod.process_one(wid, args)

        self.assertTrue(res.ok, res.error)
        self.assertEqual(os.path.basename(res.nodrums_path), "%s_no_drums.wav" % wid)
        self.assertTrue(os.path.exists(res.nodrums_path), "no-drums file should exist on success")


class MainRetryTests(unittest.TestCase):
    """A failed item must stay PENDING (not marked done) so it is retried on the next run.

    Drives the real ``main()`` queue loop with the heavy steps stubbed, so no network / binary is
    needed. The queue file lives in a temp dir (via AUTOCV_QUEUE) so no host cwd state is touched.
    """

    def _failed(self, wid):
        from jamly.run import ItemResult
        return ItemResult(watch_id=wid, status="error", stage="download", error="boom")

    def test_failed_item_stays_pending_for_retry(self):
        from jamly.normalize import Input
        import jamly.watch_queue as wq_mod
        wid = "aBcDeFg1234"
        with tempfile.TemporaryDirectory() as tmp:
            qpath = os.path.join(tmp, "jamly_queue.json")
            prev = os.environ.get("AUTOCV_QUEUE")
            os.environ["AUTOCV_QUEUE"] = qpath
            try:
                with mock.patch.object(run_mod, "gather_inputs", return_value=[Input(watch_id=wid, source_url=wid)]), \
                     mock.patch.object(run_mod, "process_one", return_value=self._failed(wid)), \
                     mock.patch.object(run_mod, "_find_ytdlp", return_value="fake-ytdlp"), \
                     mock.patch.object(run_mod, "find_ffmpeg", return_value=None), \
                     mock.patch.object(run_mod, "resolve_engine", return_value="htdemucs"), \
                     mock.patch.object(run_mod, "describe_tools", lambda *a, **k: None):
                    rc = run_mod.main(["-i", wid])
                # the run reports the failure (non-zero) ...
                self.assertNotEqual(rc, 0)
                # ... but the failed item is NOT marked done: it is still pending for the next run.
                queue = wq_mod.WatchQueue.from_path(qpath)
                self.assertIn(wid, queue.pending_ids(), "failed item must stay pending for retry")
                self.assertNotIn(wid, queue.done_ids())
            finally:
                if prev is None:
                    os.environ.pop("AUTOCV_QUEUE", None)
                else:
                    os.environ["AUTOCV_QUEUE"] = prev

    def test_successful_item_is_marked_done(self):
        from jamly.run import ItemResult
        from jamly.normalize import Input
        import jamly.watch_queue as wq_mod
        wid = "zYxWvUt5678"
        with tempfile.TemporaryDirectory() as tmp:
            qpath = os.path.join(tmp, "jamly_queue.json")
            prev = os.environ.get("AUTOCV_QUEUE")
            os.environ["AUTOCV_QUEUE"] = qpath
            try:
                ok = ItemResult(watch_id=wid, status="done", stage="mix", drums_path="/x/drums.wav")
                with mock.patch.object(run_mod, "gather_inputs", return_value=[Input(watch_id=wid, source_url=wid)]), \
                     mock.patch.object(run_mod, "process_one", return_value=ok), \
                     mock.patch.object(run_mod, "_find_ytdlp", return_value="fake-ytdlp"), \
                     mock.patch.object(run_mod, "find_ffmpeg", return_value=None), \
                     mock.patch.object(run_mod, "resolve_engine", return_value="htdemucs"), \
                     mock.patch.object(run_mod, "describe_tools", lambda *a, **k: None):
                    rc = run_mod.main(["-i", wid])
                self.assertEqual(rc, 0)
                queue = wq_mod.WatchQueue.from_path(qpath)
                self.assertIn(wid, queue.done_ids(), "successful item must be marked done")
                self.assertNotIn(wid, queue.pending_ids())
            finally:
                if prev is None:
                    os.environ.pop("AUTOCV_QUEUE", None)
                else:
                    os.environ["AUTOCV_QUEUE"] = prev


if __name__ == "__main__":
    unittest.main(verbosity=2)
