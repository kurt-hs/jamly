from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from jamly import outputs, run
from jamly.bootstrap import ToolStatus
from jamly.core import RunResult
from jamly.normalize import Input
from jamly.watch_queue import WatchQueue


class MetadataTests(unittest.TestCase):
    def test_metadata_fields_and_option_terminator(self):
        data = {"title": "Band - Song", "artist": "Band", "duration": 12.5}
        with patch.object(run, "_run", return_value=RunResult([], 0, json.dumps(data), "")) as called:
            result = run._fetch_item_metadata("yt-dlp", "-abcdefghij")
        self.assertEqual(result["title"], "Band - Song")
        self.assertEqual(result["duration_seconds"], 12)
        self.assertEqual(called.call_args.args[0][-2:], ["--", "-abcdefghij"])

    def test_collections_and_live_media_are_rejected(self):
        cases = [
            {"_type": "playlist", "entries": [{"url": "first"}, {"url": "second"}]},
            {"_type": "multi_video", "entries": []},
            {"is_live": True},
            {"is_upcoming": True},
        ]
        for data in cases:
            with self.subTest(data=data), patch.object(
                run, "_run", return_value=RunResult([], 0, json.dumps(data), "")
            ):
                with self.assertRaises(ValueError):
                    run._fetch_item_metadata("yt-dlp", "https://example.test/media")

    def test_metadata_errors_are_not_silently_discarded(self):
        with patch.object(run, "_run", return_value=RunResult([], 1, "", "network failed")):
            with self.assertRaisesRegex(RuntimeError, "network failed"):
                run._fetch_item_metadata("yt-dlp", "https://example.test/media")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.wid = "dQw4w9WgXcQ"
        self.args = run.build_parser().parse_args([
            "-o", str(self.root / "output"), "--audio-format", "wav",
            "--state-dir", str(self.root / "state"),
        ])
        self.drum_level = .3
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for target, replacement in (
            ("detect_ffmpeg", lambda **_kwargs: ToolStatus("ffmpeg", "/tools", True)),
            ("detect_engine", lambda: "htdemucs"),
            ("_fetch_item_metadata", lambda *_args: {
                "title": "Band - Song", "artist": "Band", "duration_seconds": 1,
            }),
            ("download_audio", self.download),
            ("separate_stems", self.separate),
        ):
            self.patches.enter_context(patch.object(run, target, side_effect=replacement))

    def download(self, _command, workdir, _cancel):
        path = workdir / "source.wav"
        sf.write(path, np.zeros((100, 2)), 44100, subtype="FLOAT")
        return path

    def separate(self, _audio, workdir, *_args, **_kwargs):
        result = {}
        for name, gain in (("drums", self.drum_level), ("bass", .2), ("vocals", .2), ("other", .2)):
            path = Path(workdir) / (name + ".wav")
            sf.write(path, np.full((100, 2), gain), 44100, subtype="FLOAT")
            result[name] = str(path)
        return result

    def process(self, **kwargs):
        return run.process_one(self.wid, self.args, source_url="https://youtu.be/" + self.wid, **kwargs)

    def test_complete_pair_has_manifest_and_live_stages(self):
        stages = []
        result = self.process(progress=lambda _id, stage, *_args: stages.append(stage))
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(stages, ["metadata", "download", "separate", "mix", "publish"])
        directory = Path(result.meta_path).parent
        self.assertEqual(directory, self.root / "output" / self.wid / "htdemucs" / "wav")
        manifest = outputs.read_manifest(directory)
        self.assertEqual(manifest["source_url"], "https://youtu.be/" + self.wid)
        self.assertEqual(manifest["generation"], result.generation)
        self.assertEqual(sf.info(result.drums_path).subtype, "FLOAT")
        self.assertEqual(sf.info(result.nodrums_path).subtype, "FLOAT")
        self.assertFalse(any(path.name.startswith(".wav-") for path in directory.parent.iterdir()))

    def test_valid_existing_pair_is_reusable_without_network_or_dependencies(self):
        first = self.process()
        with patch.object(run, "ytdlp_command", side_effect=AssertionError("network not needed")):
            repeated = self.process()
        self.assertEqual(repeated.status, "skipped")
        self.assertEqual(repeated.generation, first.generation)

    def test_quality_selection_reaches_worker_and_preserves_both_cached_pairs(self):
        balanced = self.process()
        self.args.model = "htdemucs_ft"
        with patch.object(run, "separate_stems", side_effect=self.separate) as separate:
            quality = self.process()
        self.assertEqual(quality.status, "done", quality.error)
        self.assertEqual(separate.call_args.kwargs["model"], "htdemucs_ft")
        self.assertNotEqual(balanced.drums_path, quality.drums_path)
        self.assertEqual(outputs.read_manifest(Path(quality.meta_path).parent)["model"], "htdemucs_ft")
        self.assertEqual(self.process().generation, quality.generation)
        self.args.model = "htdemucs"
        self.assertEqual(self.process().generation, balanced.generation)

    def test_mismatched_model_manifest_is_not_reused(self):
        first = self.process()
        path = Path(first.meta_path)
        manifest = json.loads(path.read_text())
        manifest["model"] = "htdemucs_ft"
        path.write_text(json.dumps(manifest))
        repeated = self.process()
        self.assertEqual(repeated.status, "error")
        self.assertIn("identity", repeated.error)

    def test_read_only_audio_is_reusable_without_write_access(self):
        first = self.process()
        self.assertEqual(first.status, "done", first.error)
        paths = [Path(first.drums_path), Path(first.nodrums_path)]
        try:
            for path in paths:
                path.chmod(0o444)
            self.assertEqual(self.process().status, "skipped")
        finally:
            for path in paths:
                path.chmod(0o644)

    def test_deleted_outputs_are_regenerated(self):
        first = self.process()
        import shutil
        shutil.rmtree(Path(first.meta_path).parent)
        repeated = self.process()
        self.assertEqual(repeated.status, "done", repeated.error)
        self.assertNotEqual(first.generation, repeated.generation)

    def test_partial_or_modified_output_requires_replace(self):
        result = self.process()
        Path(result.drums_path).write_bytes(b"modified")
        repeated = self.process()
        self.assertEqual(repeated.status, "error")
        self.assertIn("--replace-existing", repeated.error)
        self.args.replace_existing = True
        replaced = self.process()
        self.assertEqual(replaced.status, "done", replaced.error)
        outputs.read_manifest(Path(replaced.meta_path).parent)

    def test_failed_directory_swap_restores_old_generation(self):
        first = self.process()
        directory = Path(first.meta_path).parent
        old = {path.name: path.read_bytes() for path in directory.iterdir()}
        self.args.replace_existing = True
        self.drum_level = .7
        real_replace = os.replace

        def fail_publish(source, target):
            if Path(target) == directory and Path(source) != outputs.previous_directory(directory):
                raise PermissionError("locked destination")
            return real_replace(source, target)

        with patch.object(outputs.os, "replace", side_effect=fail_publish):
            failed = self.process()
        self.assertEqual(failed.status, "error")
        self.assertEqual(old, {path.name: path.read_bytes() for path in directory.iterdir()})
        outputs.read_manifest(directory)

    def test_interrupted_directory_swap_is_recovered(self):
        first = self.process()
        directory = Path(first.meta_path).parent
        os.replace(directory, outputs.previous_directory(directory))
        recovered = self.process()
        self.assertEqual(recovered.status, "skipped")
        self.assertEqual(recovered.generation, first.generation)

    def test_failed_inference_keeps_existing_pair(self):
        first = self.process()
        self.args.replace_existing = True
        with patch.object(run, "separate_stems", side_effect=RuntimeError("inference failed")):
            failed = self.process()
        self.assertEqual(failed.status, "error")
        self.assertEqual(outputs.read_manifest(Path(first.meta_path).parent)["generation"], first.generation)


class QueueRunnerTests(unittest.TestCase):
    def test_completed_source_is_reprocessed_with_new_destination_and_format(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "queue.json")
            item = Input("dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ")
            with patch.object(run, "gather_inputs", return_value=[item]), patch.object(
                run, "process_one", return_value=run.ItemResult(item.watch_id, status="done")
            ) as process:
                self.assertEqual(run.main(["--queue", path, "-o", root + "/first"]), 0)
                self.assertEqual(run.main(["--queue", path, "-o", root + "/second",
                                           "--audio-format", "wav", "--replace-existing"]), 0)
            self.assertEqual(process.call_count, 2)
            effective = process.call_args.args[1]
            self.assertEqual(effective.output_dir, str(Path(root) / "second"))
            self.assertEqual(effective.audio_format, "wav")
            self.assertTrue(effective.replace_existing)
            self.assertEqual(process.call_args.kwargs["source_url"], item.source_url)

    def test_cli_retry_is_bounded_to_one_attempt_per_invocation(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "queue.json")
            item = Input("dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ")
            with patch.object(run, "gather_inputs", return_value=[item]), patch.object(
                run, "process_one", return_value=run.ItemResult(item.watch_id, status="error", error="failed")
            ) as process:
                self.assertEqual(run.main(["--queue", path]), 1)
            self.assertEqual(process.call_count, 1)
            self.assertEqual(WatchQueue.from_path(path).get(item.watch_id)["status"], "error")
            with patch.object(run, "gather_inputs", return_value=[]), patch.object(
                run, "process_one", return_value=run.ItemResult(item.watch_id, status="done")
            ) as process:
                self.assertEqual(run.main(["--queue", path]), 0)
            self.assertEqual(process.call_count, 1)

    def test_dry_run_does_not_create_queue_files(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "queue.json")
            with patch.object(run, "gather_inputs", return_value=[Input("test", "https://example.test/audio")]):
                self.assertEqual(run.main(["--queue", path, "--dry-run"]), 0)
            self.assertEqual(list(Path(root).iterdir()), [])
