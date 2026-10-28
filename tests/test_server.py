import http.client
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlencode, urlsplit

from jamly import companion, config, core, server
from jamly.outputs import PROCESSING_VERSION, checksum
from jamly.run import ItemResult
from jamly.watch_queue import QueueError, WatchQueue


class ServerTests(unittest.TestCase):
    wid = "dQw4w9WgXcQ"
    source = "https://youtu.be/dQw4w9WgXcQ"

    def start(self, process=None):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        args = server.build_parser().parse_args([
            "--port", "0", "--queue", str(root / "queue.json"),
            "--state-dir", str(root),
            "--output-dir", str(root / "output"), "--server-token", "test-token",
        ])
        self.enterContext(patch.object(
            server, "process_one", side_effect=process or
            (lambda wid, args, **kwargs: ItemResult(wid, status="done"))
        ))
        app = server.ItemServer(args)
        thread = threading.Thread(target=app.start, daemon=True)
        thread.start()

        def cleanup():
            app.stop()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(cleanup)
        self.app, self.root, self.thread = app, root, thread
        return app

    def request(self, method, path, body=None, *, auth=True, headers=None, raw=False):
        headers = dict(headers or {})
        if auth:
            headers["Authorization"] = "Bearer test-token"
        data = None
        if method == "POST":
            headers.setdefault("Content-Type", "application/json")
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.app._server.server_address[1], timeout=3
        )
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            content = response.read()
            return response.status, content if raw else json.loads(content) if content else {}, dict(response.getheaders())
        finally:
            connection.close()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, state, _ = self.request("GET", "/status")
            if predicate(state):
                return state
            time.sleep(.02)
        self.fail("Timed out waiting for job state")

    def test_pairing_is_required_for_status_and_queue_mutation(self):
        self.start()
        self.assertEqual(self.request("GET", "/healthz", auth=False)[0], 200)
        self.assertEqual(self.request("GET", "/status", auth=False)[0], 401)
        self.assertEqual(self.request("POST", "/add", {"inputs": [self.source]}, auth=False)[0], 401)
        self.assertEqual(self.app.queue.items, [])

    def test_companion_assets_are_public_but_contain_no_pairing_secrets(self):
        self.start()
        code, page, headers = self.request("GET", "/app/", auth=False, raw=True)
        self.assertEqual(code, 200)
        self.assertIn(b"Separation preset", page)
        self.assertNotIn(self.app._token.encode(), page)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("GET", "/app/../../server-token", auth=False)[0], 404)
        self.assertEqual(self.request("POST", "/app/launch", {}, auth=False)[0], 401)

    def test_launcher_verifies_backend_and_queue_before_sending_credentials(self):
        self.start()
        received = []
        original = server.QueueStatusHandler._allowed_request

        def capture(handler, *args, **kwargs):
            received.append(handler.headers.get("Authorization"))
            return original(handler, *args, **kwargs)

        with patch.object(server.QueueStatusHandler, "_allowed_request", capture):
            with self.assertRaisesRegex(RuntimeError, "identity"):
                companion._connect(self.app.base_url, "test-token", self.root / "different-queue.json")
            self.assertEqual(received, [None])
            received.clear()
            self.assertTrue(companion._connect(self.app.base_url, "test-token", Path(self.app.queue_path)))
            self.assertEqual(received, [None])
            companion.request(self.app.base_url, "test-token", "/app/launch", {})
            self.assertEqual(received, [None, "Bearer test-token"])

    def test_launcher_pairs_through_a_private_single_use_file_not_a_url(self):
        self.start()
        code, launch, _ = self.request("POST", "/app/launch", {})
        self.assertEqual(code, 200)
        url = urlsplit(launch["launch_file"])
        self.assertEqual(url.query, "")
        self.assertEqual(url.fragment, "")
        path = Path(unquote(url.path))
        if sys.platform == "win32":
            path = Path(unquote(url.path).lstrip("/"))
        ticket = re.search(r'name="ticket" value="([^"]+)"', path.read_text()).group(1)
        if sys.platform != "win32":
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        body = urlencode({"ticket": ticket}).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "null"}
        code, page, response_headers = self.request("POST", "/app/session", body, auth=False,
                                                   headers=headers, raw=True)
        self.assertEqual(code, 200)
        self.assertIn("nonce-", response_headers["Content-Security-Policy"])
        self.assertFalse(path.exists())
        token = re.search(rb'setItem\("jamly-token", "([^"]+)"\)', page).group(1).decode()
        self.assertNotEqual(token, self.app._token)
        self.assertEqual(self.request("GET", "/status", auth=False, headers={
            "Authorization": "Bearer " + token, "Origin": self.app.base_url,
        })[0], 200)
        self.assertEqual(self.request("POST", "/app/session", body, auth=False, headers=headers)[0], 400)
        with patch("jamly.companion_ui.time.monotonic", return_value=time.monotonic() + 86401):
            self.assertEqual(self.request("GET", "/status", auth=False, headers={
                "Authorization": "Bearer " + token,
            })[0], 401)

    def test_settings_are_persistent_validated_and_do_not_change_queued_options(self):
        self.start()
        self.request("POST", "/add", {"inputs": [self.source]})
        self.wait_for(lambda state: state["done"] == 1)
        options = dict(self.app.queue.get(self.wid)["options"])
        settings = {"model": "htdemucs_ft", "audio_format": "wav", "output_dir": str(self.root / "other")}
        code, saved, _ = self.request("POST", "/settings", settings)
        self.assertEqual((code, saved), (200, settings))
        self.assertEqual(self.app.queue.get(self.wid)["options"], options)
        args = config.parse_configured_args(server.build_parser(),
                                            ["--state-dir", str(self.root)], preferences=True)
        self.assertEqual(args.model, "htdemucs_ft")
        self.assertEqual(args.audio_format, "wav")
        for invalid in ({"output_dir": "../relative"}, {"model": "other"}, {"audio_format": None},
                        {"server_token": "do-not-change"}, {"output_dir": False}):
            self.assertEqual(self.request("POST", "/settings", invalid)[0], 400)
        with patch.object(server, "write_private_json", side_effect=OSError("disk full")):
            self.assertEqual(self.request("POST", "/settings", {"model": "htdemucs"})[0], 503)
        self.assertEqual(self.app.args.model, "htdemucs_ft")

    def test_unrelated_origin_or_host_is_rejected_even_with_token(self):
        self.start()
        for headers in ({"Origin": "https://unrelated.example"}, {"Host": "unrelated.example"},
                        {"Origin": "null"}, {"Host": "localhost:invalid"},
                        {"Origin": "http://127.0.0.2:%d" % self.app._server.server_address[1]},
                        {"Origin": "chrome-extension://[invalid"}):
            code, _, _ = self.request("POST", "/add", {"inputs": [self.source]}, headers=headers)
            self.assertEqual(code, 403)
        self.assertEqual(self.app.queue.items, [])

    def test_extension_preflight_allows_authorization_without_wildcard(self):
        self.start()
        code, _, headers = self.request("OPTIONS", "/add", auth=False, headers={
            "Origin": "chrome-extension://example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        })
        self.assertEqual(code, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "chrome-extension://example")
        self.assertIn("Authorization", headers["Access-Control-Allow-Headers"])

    def test_invalid_payloads_return_client_errors(self):
        self.start()
        for body in (None, [], {"inputs": "url"}, {"inputs": [3]}, {"inputs": []},
                     {"inputs": [self.source], "titles": []},
                     {"inputs": [self.source], "model": "untrusted"},
                     {"inputs": [self.source], "model": None},
                     {"inputs": [self.source], "model": []},
                     {"inputs": [self.source], "audio_format": "flac"},
                     {"inputs": [self.source], "replace_existing": "true"}, b"{invalid"):
            with self.subTest(body=body):
                code, payload, _ = self.request("POST", "/add", body)
                self.assertEqual(code, 400)
                self.assertIn("error", payload)
        self.assertEqual(self.app.queue.items, [])

    def test_selected_model_and_format_are_persisted_and_used_on_retry(self):
        seen = []

        def process(wid, args, **_kwargs):
            seen.append((args.model, args.audio_format))
            return ItemResult(wid, status="error", error="retry this job")

        self.start(process)
        code, _, _ = self.request("POST", "/add", {
            "inputs": [self.source], "model": "htdemucs_ft", "audio_format": "wav",
        })
        self.assertEqual(code, 202)
        self.wait_for(lambda state: state["failed"] == 1)
        self.assertEqual(WatchQueue.from_path(self.app.queue_path).get(self.wid)["options"]["model"], "htdemucs_ft")
        self.request("POST", "/retry/" + self.wid, {})
        state = self.wait_for(lambda state: state["failed"] == 1 and len(seen) == 2)
        self.assertEqual(seen, [("htdemucs_ft", "wav"), ("htdemucs_ft", "wav")])
        self.assertEqual(state["jobs"][0]["model"], "htdemucs_ft")

    def test_stage_progress_and_original_source_reach_worker(self):
        started, release = threading.Event(), threading.Event()
        seen = []

        def process(wid, args, *, progress, source_url, cancel_event):
            seen.append(source_url)
            progress(wid, "separate", 0, 0)
            started.set()
            release.wait(timeout=3)
            return ItemResult(wid, status="done")
        self.start(process)
        self.request("POST", "/add", {"inputs": [self.source]})
        try:
            self.assertTrue(started.wait(3))
            state = self.request("GET", "/status")[1]
            self.assertEqual(state["current"]["stage"], "separate")
            self.assertEqual(state["current"]["status"], "running")
        finally:
            release.set()
        state = self.wait_for(lambda state: state["done"] == 1)
        self.assertIsNone(state["current"])
        self.assertEqual(seen, [self.source])

    def test_error_survives_reload_and_explicit_retry_succeeds(self):
        attempts = []

        def process(wid, _args, **_kwargs):
            attempts.append(wid)
            return ItemResult(wid, status="error" if len(attempts) == 1 else "done",
                              error="temporary network failure" if len(attempts) == 1 else None)
        self.start(process)
        self.request("POST", "/add", {"inputs": [self.source]})
        state = self.wait_for(lambda state: state["failed"] == 1)
        self.assertEqual(state["done"], 0)
        loaded = WatchQueue.from_path(self.app.queue_path)
        self.assertEqual(loaded.get(self.wid)["status"], "error")
        self.assertEqual(loaded.pending_ids(), [])
        self.request("POST", "/retry/" + self.wid, {})
        self.wait_for(lambda state: state["done"] == 1)
        self.assertEqual(len(attempts), 2)

    def test_failed_persistence_does_not_enqueue_a_rejected_request(self):
        process = Mock(return_value=ItemResult(self.wid, status="done"))
        self.start(process)
        with patch.object(WatchQueue, "save", side_effect=OSError("disk full")):
            code, payload, _ = self.request("POST", "/add", {"inputs": [self.source]})
        self.assertEqual(code, 503)
        self.assertIn("disk full", payload["error"])
        self.assertEqual(self.app.queue.items, [])
        process.assert_not_called()

    def test_stop_cancels_worker_and_releases_queue_ownership(self):
        started = threading.Event()

        def process(wid, _args, *, cancel_event, **_kwargs):
            try:
                core.run([sys.executable, "-c",
                          "import time;print('ready',flush=True);time.sleep(60)"],
                         stream=True, on_line=lambda _line: started.set(), cancel_event=cancel_event)
            except InterruptedError:
                return ItemResult(wid, status="cancelled", error="stopped")
            return ItemResult(wid, status="done")
        self.start(process)
        self.request("POST", "/add", {"inputs": [self.source]})
        self.assertTrue(started.wait(3))
        with self.assertRaises(QueueError):
            server.ItemServer(self.app.args)
        self.app.stop()
        self.assertFalse(self.app._worker.is_alive())
        self.assertEqual(WatchQueue.from_path(self.app.queue_path).pending_ids(), [self.wid])
        replacement = server.ItemServer(self.app.args)
        replacement.stop()

    def test_artifacts_require_auth_and_matching_generation(self):
        def process(wid, args, **_kwargs):
            directory = Path(args.output_dir) / wid / args.audio_format
            directory.mkdir(parents=True)
            paths = {}
            for kind in ("drums", "no_drums"):
                path = directory / (kind + ".mp3")
                path.write_bytes(kind.encode())
                paths[kind] = path
            metadata = {
                "processing_version": PROCESSING_VERSION, "generation": "generation-one",
                "outputs": {kind: {"file": path.name, "sha256": checksum(path)}
                            for kind, path in paths.items()},
            }
            meta_path = directory / "metadata.json"
            meta_path.write_text(json.dumps(metadata))
            return ItemResult(wid, status="done", drums_path=str(paths["drums"]),
                              nodrums_path=str(paths["no_drums"]), meta_path=str(meta_path),
                              generation="generation-one")
        self.start(process)
        self.request("POST", "/add", {"inputs": [self.source]})
        state = self.wait_for(lambda state: state["done"] == 1)
        output = next(item for item in state["jobs"][0]["outputs"] if item["kind"] == "drums")
        self.assertEqual(self.request("GET", output["url"], auth=False)[0], 401)
        code, body, _ = self.request("GET", output["url"], raw=True)
        self.assertEqual((code, body), (200, b"drums"))
        self.assertEqual(self.request("GET", output["url"].replace("generation-one", "stale"))[0], 409)
        self.assertEqual(self.request("GET", "/outputs/" + self.wid + "/../server-token")[0], 404)
        form_headers = {"Content-Type": "application/x-www-form-urlencoded", "Origin": self.app.base_url}
        download_path = output["url"].replace("/outputs/", "/app/download/", 1)
        code, downloaded, _ = self.request("POST", download_path,
                                           urlencode({"token": "test-token"}).encode(),
                                           auth=False, headers=form_headers, raw=True)
        self.assertEqual((code, downloaded), (200, b"drums"))
        self.assertEqual(self.request("POST", download_path, b"token=wrong",
                                     auth=False, headers=form_headers)[0], 401)


class HostTests(unittest.TestCase):
    def test_only_real_loopback_hosts_are_trusted(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertTrue(server._is_loopback_host(host))
        for host in ("", "0.0.0.0", "evil.example", "127.0.0.1.evil.example"):
            self.assertFalse(server._is_loopback_host(host))
