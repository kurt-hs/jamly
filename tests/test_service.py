import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from jamly import config, server, service


class ServiceDefinitionsTests(unittest.TestCase):
    def test_browser_export_excludes_test_scripts(self):
        with tempfile.TemporaryDirectory() as root:
            destination = service.export_browser_assets(Path(root), "extension")
            manifest = json.loads((destination / "manifest.json").read_text())
            self.assertEqual(manifest["manifest_version"], 3)
            self.assertTrue((destination / "jamly_client.js").is_file())
            self.assertFalse(any(path.name.endswith("_test.js") for path in destination.iterdir()))

    def test_all_platforms_use_the_selected_interpreter_and_config(self):
        python = "/installed path/python"
        state = PurePosixPath("/state path")
        settings = state / "server.json"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/configuration"}):
            path, unit = service.render_service("linux", python, settings, state, home=Path("/home"))
        self.assertIn('ExecStart="/installed path/python" "-m" "jamly.server"', unit.decode())
        self.assertIn(str(settings), unit.decode())
        self.assertNotIn("PYTHONPATH", unit.decode())
        self.assertEqual(path.name, "jamly.service")

        _, plist = service.render_service("darwin", python, settings, state, home=Path("/home"))
        self.assertEqual(plistlib.loads(plist)["ProgramArguments"],
                         [python, "-m", "jamly.server", "--config", str(settings)])

        _, task = service.render_service("win32", "C:\\Jamly Env\\python.exe",
                                         Path("C:/Jamly State/server.json"), Path("C:/Jamly State"),
                                         username="EXAMPLE\\User")
        root = ET.fromstring(task)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find(".//t:Command", ns).text, "C:\\Jamly Env\\python.exe")
        self.assertIn("-m jamly.server", root.find(".//t:Arguments", ns).text)
        self.assertIsNotNone(root.find(".//t:LogonTrigger", ns))
        self.assertEqual(root.find(".//t:RunLevel", ns).text, "LeastPrivilege")

    def test_systemd_escaping_keeps_paths_literal(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/configuration"}):
            _, unit = service.render_service(
                "linux", '/path/with % and $/python', Path('/config/"quoted".json'), Path("/state")
            )
        self.assertIn("%% and $$", unit.decode())
        self.assertIn('\\"quoted\\"', unit.decode())
        with self.assertRaises(ValueError):
            service.render_service("linux", "/bad\npath", Path("/config"), Path("/state"))

    def test_install_dry_run_never_registers_a_service(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "new-state")
            with patch.object(service.subprocess, "run") as call:
                result = service.main(["install", "--state-dir", path, "--dry-run"])
            self.assertEqual(result, 0)
            call.assert_not_called()
            self.assertFalse(Path(path).exists())


class SharedConfigTests(unittest.TestCase):
    def test_explicit_queue_and_state_resolve_consistently(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"JAMLY_STATE_DIR": "", "JAMLY_SCOPE": "", "JAMLY_QUEUE": ""}
        ):
            queue = Path(root) / "custom.json"
            args = server.build_parser().parse_args(["--queue", str(queue)])
            self.assertEqual(config.runtime_paths(args), (Path(root), queue))
            args.state_dir = str(Path(root) / "separate-state")
            self.assertEqual(config.runtime_paths(args), (Path(args.state_dir), queue))

    def test_config_uses_absolute_paths_and_cli_overrides(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "server.json"
            path.write_text(json.dumps({
                "output_dir": str(Path(root) / "audio"), "port": 8765, "audio_format": "wav",
            }))
            args = config.parse_configured_args(server.build_parser(),
                                                ["--config", str(path), "--port", "9876"])
            self.assertEqual(args.port, 9876)
            self.assertEqual(args.audio_format, "wav")
            self.assertEqual(args.output_dir, str(Path(root) / "audio"))

    def test_invalid_configuration_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "server.json"
            path.write_text('{"unknown": true}')
            with self.assertRaises(ValueError):
                config.parse_configured_args(server.build_parser(), ["--config", str(path)])

    def test_saved_defaults_override_install_config_but_not_explicit_cli_flags(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            settings = state / "server.json"
            settings.write_text(json.dumps({"state_dir": root, "model": "htdemucs"}))
            config.write_private_json(state / "preferences.json", {"model": "htdemucs_ft"})
            args = config.parse_configured_args(server.build_parser(),
                                                ["--config", str(settings)], preferences=True)
            self.assertEqual(args.model, "htdemucs_ft")
            args = config.parse_configured_args(server.build_parser(), [
                "--config", str(settings), "--model", "htdemucs",
            ], preferences=True)
            self.assertEqual(args.model, "htdemucs")

    def test_legacy_job_keeps_balanced_instead_of_inheriting_new_default(self):
        args = server.build_parser().parse_args(["--model", "htdemucs_ft"])
        self.assertEqual(config.job_arguments(args, {"audio_format": "wav"}).model, "htdemucs")

    def test_pairing_token_is_persistent_and_private(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"JAMLY_SERVER_TOKEN": ""}):
            state = Path(root)
            first = config.pairing_token(state)
            self.assertGreaterEqual(len(first), 32)
            self.assertEqual(config.pairing_token(state), first)
            if sys.platform != "win32":
                self.assertEqual((state / "server-token").stat().st_mode & 0o077, 0)
            (state / "server-token").write_text("")
            with self.assertRaises(ValueError):
                config.pairing_token(state)
