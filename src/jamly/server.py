"""Paired local HTTP API with one queue owner and one cancellable worker."""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from filelock import FileLock, Timeout

from . import __version__
from .config import (
    add_pipeline_arguments, job_arguments, pairing_token, parse_configured_args,
    pipeline_options, runtime_paths, token_command, instance_path, validate_preferences,
    write_private_json, pairing_proof,
)
from .companion_ui import CompanionUI, CSP
from .normalize import InputError, make_input
from .models import DEFAULT_MODEL, model_choices, validate_model
from .outputs import read_manifest
from .run import ItemResult, process_one
from .runtime import open_directory
from .watch_queue import QueueError, WatchQueue, queue_owner


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _authorized(header: str, token: str) -> bool:
    parts = header.split(None, 1)
    return (len(parts) == 2 and parts[0].lower() == "bearer"
            and hmac.compare_digest(parts[1].strip().encode(), token.encode()))


def _port(value: str) -> int:
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Jamly local backend")
    add_pipeline_arguments(parser)
    parser.add_argument("--config", help="JSON configuration written by jamly-service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=8765)
    parser.add_argument("--server-token", default=None, help="Override the saved pairing token")
    parser.add_argument("--allow-origin", action="append", default=[],
                        help="Additional exact browser origin; never use '*'")
    parser.add_argument("--allow-host", action="append", default=[],
                        help="Additional HTTP Host name for an explicitly configured proxy/LAN")
    return parser


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class _IPv6Server(_HTTPServer):
    address_family = socket.AF_INET6


class ItemServer:
    def __init__(self, args):
        self.args = args
        explicit_token = args.server_token or os.environ.get("JAMLY_SERVER_TOKEN")
        args.host = args.host.strip()
        if not args.host or not _is_loopback_host(args.host) and not explicit_token:
            raise ValueError("Non-loopback binds require an explicit --server-token")
        if "*" in args.allow_origin:
            raise ValueError("Use exact --allow-origin values, not '*'")
        self.state_dir, queue_path = runtime_paths(args)
        self.queue_path = str(queue_path)
        self._owner = queue_owner(self.queue_path)
        self._owner.__enter__()
        try:
            self.queue = WatchQueue.from_path(self.queue_path)
            self._token = pairing_token(self.state_dir, explicit_token)
            self._status_lock = threading.RLock()
            self._stop = threading.Event()
            self._wake = threading.Event()
            self._stop_lock = threading.Lock()
            self._worker = None
            self._serving = False
            self._serve_ident = None
            self._closed = False
            self._current = None
            self._error = None
            self.ui = CompanionUI(self.state_dir, enabled=_is_loopback_host(args.host))
            server_type = _IPv6Server if ":" in args.host else _HTTPServer
            self._server = server_type((args.host, args.port), QueueStatusHandler)
            self._server.item_server = self
        except BaseException:
            self._owner.__exit__(*sys.exc_info())
            raise

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return "http://%s:%d" % ("[" + host + "]" if ":" in host else host, port)

    def authorized(self, header: str) -> bool:
        return _authorized(header, self._token) or self.ui.authorize(header)

    def save_preferences(self, settings: dict) -> dict:
        settings = validate_preferences(settings)
        with self._status_lock:
            if self._stop.is_set():
                raise QueueError("Server is shutting down")
            current = {key: value for key, value in pipeline_options(self.args).items()
                       if key in ("model", "audio_format", "output_dir")}
            current.update(settings)
            write_private_json(self.state_dir / "preferences.json", current)
            for key, value in current.items():
                setattr(self.args, key, value)
            return current

    def show_folder(self, wid: str | None = None) -> None:
        with self._status_lock:
            if wid is None:
                directory = Path(self.args.output_dir)
                directory.mkdir(parents=True, exist_ok=True)
            else:
                item = self.queue.get(wid)
                if not item or item["status"] not in ("done", "skipped") or not item.get("outputs"):
                    raise ValueError("This job has no completed export")
                directory = Path(item["outputs"]["metadata"]).parent
        open_directory(directory)

    def _commit(self, change):
        candidate = self.queue.copy()
        value = change(candidate)
        candidate.save()
        self.queue = candidate
        return value

    def enqueue(self, inputs, titles=None, replace=False, *, model=None, audio_format=None) -> int:
        normalized = [make_input(source) for source in inputs]
        if model is not None:
            validate_model(model)
        if audio_format is not None and audio_format not in ("mp3", "wav"):
            raise ValueError("audio_format must be mp3 or wav")
        with self._status_lock:
            options = dict(pipeline_options(self.args), replace_existing=replace or self.args.replace_existing)
            if model is not None:
                options["model"] = model
            if audio_format is not None:
                options["audio_format"] = audio_format
            if self._error or self._stop.is_set():
                raise QueueError(self._error or "Server is shutting down")
            added = self._commit(lambda queue: queue.add_all(
                [item.watch_id for item in normalized], titles,
                [item.source_url for item in normalized], retry=True, options=options,
            ))
        self._wake.set()
        return added

    def retry(self, wid: str, replace: bool = False) -> bool:
        with self._status_lock:
            if self._error or self._stop.is_set():
                raise QueueError(self._error or "Server is shutting down")
            item = self.queue.get(wid)
            if item is None:
                raise KeyError(wid)
            options = dict(item.get("options", {}), replace_existing=replace)
            added = self._commit(lambda queue: queue.add(wid, retry=True, options=options))
        self._wake.set()
        return added

    def _process_one(self) -> bool:
        with self._status_lock:
            pending = self.queue.pending_items()
            if not pending or self._stop.is_set():
                return False
            item = pending[0]
            wid = item["watch_id"]
            self._commit(lambda queue: queue.mark_running(wid))
            self._current = {"watch_id": wid, "stage": "metadata", "status": "running",
                             "title": item.get("title"), "error": None}

        def progress(_wid, stage, _index, _total):
            with self._status_lock:
                self._current["stage"] = stage

        try:
            result = process_one(
                wid, job_arguments(self.args, item.get("options", {})),
                source_url=item["source_url"], progress=progress, cancel_event=self._stop,
            )
        except Exception as exc:
            result = ItemResult(wid, status="error", error=str(exc))
        status = "pending" if result.status == "cancelled" and self._stop.is_set() else result.status
        with self._status_lock:
            self._commit(lambda queue: queue.finish(
                wid, status, error=result.error if status != "pending" else None,
                title=result.title, outputs=result.outputs, generation=result.generation,
            ))
            self._current = None
        return True

    def _loop(self):
        while not self._stop.is_set():
            try:
                if self._process_one():
                    continue
            except Exception as exc:
                with self._status_lock:
                    self._error = "Cannot persist queue state; fix storage and restart: %s" % exc
                print(self._error, file=sys.stderr)
                return
            self._wake.wait()
            self._wake.clear()

    def snapshot(self) -> dict:
        with self._status_lock:
            items = self.queue.items
            terminal = [item for item in items if item["status"] not in ("pending", "running")]
            visible = [item for item in items if item["status"] in ("pending", "running")] + terminal[-100:]
            jobs = []
            for item in reversed(visible):
                record = {key: item.get(key) for key in (
                    "watch_id", "title", "source_url", "status", "error", "attempts", "updated_at"
                )}
                record["outputs"] = []
                record["model"] = item.get("options", {}).get("model", DEFAULT_MODEL)
                record["audio_format"] = item.get("options", {}).get("audio_format", "mp3")
                if item["status"] in ("done", "skipped") and item.get("generation"):
                    for kind, path in item.get("outputs", {}).items():
                        if kind in ("drums", "no_drums", "metadata"):
                            record["outputs"].append({
                                "kind": kind, "name": Path(path).name,
                                "url": "/outputs/%s/%s?generation=%s" % (
                                    quote(item["watch_id"], safe=""), kind, quote(item["generation"], safe="")
                                ),
                            })
                jobs.append(record)
            return {
                "api_version": 3, "pending": sum(item["status"] == "pending" for item in items),
                "done": len(self.queue.done_ids()), "failed": sum(item["status"] == "error" for item in items),
                "total": len(items), "current": dict(self._current) if self._current else None,
                "jobs": jobs, "error": self._error, "models": model_choices(),
                "defaults": {key: value for key, value in pipeline_options(self.args).items()
                             if key in ("model", "audio_format", "output_dir")},
            }

    def start(self):
        if self.ui.enabled:
            write_private_json(instance_path(self.state_dir, Path(self.queue_path)), {
                "port": self._server.server_address[1], "host": self._server.server_address[0],
                "pid": os.getpid(),
            })
        self._serving = True
        self._serve_ident = threading.get_ident()
        self._worker = threading.Thread(target=self._loop, name="jamly-worker", daemon=True)
        self._worker.start()
        try:
            self._server.serve_forever(poll_interval=0.1)
        finally:
            self._serving = False

    def stop(self):
        with self._stop_lock:
            if self._closed:
                return
            self._stop.set()
            self._wake.set()
            if self._serving and self._serve_ident != threading.get_ident():
                self._server.shutdown()
            if self._worker is not None:
                self._worker.join(timeout=15)
                if self._worker.is_alive():
                    raise RuntimeError("The worker has not stopped; queue ownership was retained")
            with self._status_lock:
                self._server.server_close()
                if self.ui.enabled:
                    instance_path(self.state_dir, Path(self.queue_path)).unlink(missing_ok=True)
                self.ui.close()
                self._owner.__exit__(None, None, None)
                self._closed = True


class QueueStatusHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> ItemServer:
        return self.server.item_server

    def log_message(self, fmt, *args):
        pass

    def _allowed_origin(self, origin: str) -> bool:
        if origin in self.app.args.allow_origin:
            return True
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        if (self.app.ui.enabled and parsed.scheme == "http" and not parsed.username
                and parsed.path in ("", "/") and not parsed.query and not parsed.fragment):
            try:
                requested = urlsplit("//" + self.headers.get("Host", ""))
                if (parsed.hostname == requested.hostname
                        and _is_loopback_host(parsed.hostname or "")
                        and (parsed.port or 80) == self.app._server.server_address[1]
                        and (requested.port or 80) == self.app._server.server_address[1]):
                    return True
            except ValueError:
                return False
        return (parsed.scheme in ("chrome-extension", "moz-extension")
                and bool(parsed.netloc) and parsed.path in ("", "/")
                and not parsed.username and not parsed.query and not parsed.fragment)

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin and self._allowed_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _json(self, body, status=200):
        self._bytes(json.dumps(body).encode("utf-8"), "application/json", status)

    def _bytes(self, data, content_type, status=200, *, policy=None):
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Native same-origin POST downloads need their Origin header; no-referrer makes it "null".
        self.send_header("Referrer-Policy", "same-origin" if policy else "no-referrer")
        if policy:
            self.send_header("Content-Security-Policy", policy)
            self.send_header("X-Frame-Options", "DENY")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _allowed_request(self, auth=True, *, bootstrap=False):
        allowed = {"localhost", "127.0.0.1", "::1", self.app.args.host.lower(),
                   *[host.lower() for host in self.app.args.allow_host]}
        try:
            host = urlsplit("//" + self.headers.get("Host", ""))
            valid_host = (host.hostname in allowed and not host.username and not host.path
                          and not host.query and not host.fragment and (host.port is None or host.port > 0))
        except ValueError:
            valid_host = False
        origin = self.headers.get("Origin")
        origin_ok = not origin or self._allowed_origin(origin) or (bootstrap and origin == "null")
        if not valid_host or not origin_ok:
            self.close_connection = True
            self._json({"error": "Host or browser origin is not allowed"}, 403)
            return False
        if auth and not self.app.authorized(self.headers.get("Authorization", "")):
            self.close_connection = True
            self._json({"error": "Session expired or client not paired. Reopen Jamly, or pair the extension "
                        "with the token from the companion's extension setup."}, 401)
            return False
        return True

    def do_OPTIONS(self):
        if not self._allowed_request(auth=False):
            return
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urlsplit(self.path)
        public = url.path in ("/", "/app", "/healthz", "/handshake") or url.path.startswith("/app/")
        if not self._allowed_request(auth=not public):
            return
        if url.path in ("/", "/app"):
            self.send_response(303)
            self.send_header("Location", "/app/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif url.path.startswith("/app/"):
            try:
                data, kind = self.app.ui.asset(url.path)
                self._bytes(data, kind, policy=CSP)
            except OSError:
                self._json({"error": "Companion asset is missing; reinstall Jamly or check the URL"}, 404)
        elif url.path == "/handshake" and self.app.ui.enabled:
            try:
                values = parse_qs(url.query, strict_parsing=True, max_num_fields=1)
                nonce = values.get("nonce", [""])[0]
                if set(values) != {"nonce"} or not re.fullmatch(r"[a-f0-9]{64}", nonce):
                    raise ValueError("A 32-byte hexadecimal challenge is required")
                self._json({"api_version": 3, "proof": pairing_proof(
                    self.app._token, nonce, self.app.base_url, Path(self.app.queue_path),
                )})
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
        elif url.path == "/healthz":
            self._json({"ok": not bool(self.app._error), "version": __version__,
                        "api_version": 3, "authentication_required": True})
        elif url.path in ("/status", "/items"):
            self._json(self.app.snapshot())
        elif url.path.startswith("/outputs/"):
            self._output(url)
        else:
            self._json({"error": "not found"}, 404)

    def _output(self, url):
        parts = url.path.split("/")
        if len(parts) != 4 or parts[3] not in ("drums", "no_drums", "metadata"):
            self._json({"error": "unknown output"}, 404)
            return
        with self.app._status_lock:
            item = self.app.queue.get(unquote(parts[2]))
        generation = parse_qs(url.query).get("generation", [None])[0]
        if not item or item["status"] not in ("done", "skipped"):
            self._json({"error": "outputs are not ready"}, 409)
            return
        if not generation or generation != item.get("generation"):
            self._json({"error": "output generation changed; refresh the job"}, 409)
            return
        started = False
        try:
            directory = Path(item["outputs"]["metadata"]).parent
            lock = FileLock(str(directory.parent / ("." + directory.name + ".lock")), timeout=0)
            with lock:
                manifest = read_manifest(directory, verify_hashes=False)
                if manifest["generation"] != generation:
                    self._json({"error": "output generation changed; refresh the job"}, 409)
                    return
                kind = parts[3]
                name = "metadata.json" if kind == "metadata" else manifest["outputs"][kind]["file"]
                path = directory / name
                with path.open("rb") as file:
                    self.send_response(200)
                    self._cors()
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(os.fstat(file.fileno()).st_size))
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    started = True
                    shutil.copyfileobj(file, self.wfile)
        except Timeout:
            self._json({"error": "outputs are being replaced; try again shortly"}, 409)
        except (OSError, ValueError, KeyError) as exc:
            if started:
                self.close_connection = True
                raise
            self._json({"error": "output is unavailable: %s" % exc}, 404)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/app/session" or path.startswith("/app/download/"):
            self._companion_form(path)
            return
        if not self._allowed_request():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > 1024 * 1024 or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._json({"error": "invalid or oversized request body"}, 400)
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self.close_connection = True
            self._json({"error": "Content-Type must be application/json"}, 415)
            return
        try:
            self.connection.settimeout(10)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
            if path == "/app/launch":
                if payload:
                    raise ValueError("Launch takes no options")
                launch = self.app.ui.launch(self.app.base_url)
                self._json({"launch_file": launch.as_uri()})
                return
            if path == "/settings":
                self._json(self.app.save_preferences(payload))
                return
            if path == "/app/extension":
                if payload or not self.app.ui.enabled:
                    raise ValueError("Extension setup takes no options and requires a loopback backend")
                from .service import export_browser_assets
                directory = export_browser_assets(self.app.state_dir, "extension")
                self._json({"directory": str(directory), "token": self.app._token})
                return
            if path in ("/app/open-folder", "/app/stop"):
                if not self.app.ui.enabled:
                    raise ValueError("Desktop actions require a loopback backend")
                if path == "/app/stop":
                    if payload:
                        raise ValueError("Stop takes no options")
                    self._json({"stopping": True})
                    threading.Thread(target=self.app.stop, name="jamly-stop", daemon=True).start()
                else:
                    if set(payload) - {"id"} or ("id" in payload and not isinstance(payload["id"], str)):
                        raise ValueError("Open folder accepts only an optional job id")
                    self.app.show_folder(payload.get("id"))
                    self._json({"opened": True})
                return
            if path == "/add":
                if set(payload) - {"inputs", "titles", "replace_existing", "model", "audio_format"}:
                    raise ValueError("unknown request field")
                inputs = payload.get("inputs")
                if not isinstance(inputs, list) or not inputs or not all(
                    isinstance(source, str) and source.strip() for source in inputs
                ):
                    raise ValueError("inputs must be a non-empty list of non-empty strings")
                titles = payload.get("titles")
                if titles is not None and (not isinstance(titles, list) or len(titles) != len(inputs)
                                           or any(title is not None and not isinstance(title, str) for title in titles)):
                    raise ValueError("titles must match the inputs list")
                replace = payload.get("replace_existing", False)
                if not isinstance(replace, bool):
                    raise ValueError("replace_existing must be a boolean")
                if "model" in payload:
                    validate_model(payload["model"])
                if "audio_format" in payload and payload["audio_format"] not in ("mp3", "wav"):
                    raise ValueError("audio_format must be mp3 or wav")
                added = self.app.enqueue(inputs, titles, replace, model=payload.get("model"),
                                         audio_format=payload.get("audio_format"))
            elif path.startswith("/retry/"):
                if set(payload) - {"replace_existing"} or not isinstance(payload.get("replace_existing", False), bool):
                    raise ValueError("retry accepts only a boolean replace_existing field")
                added = int(self.app.retry(unquote(path.removeprefix("/retry/")),
                                           payload.get("replace_existing", False)))
            else:
                self._json({"error": "not found"}, 404)
                return
            self._json({"newly_added": added, "pending_count": self.app.snapshot()["pending"]}, 202)
        except (ValueError, InputError, UnicodeError) as exc:
            self._json({"error": str(exc)}, 400)
        except KeyError:
            self._json({"error": "unknown job"}, 404)
        except (OSError, QueueError) as exc:
            self._json({"error": "Cannot persist request: %s" % exc}, 503)
        except (RuntimeError, subprocess.SubprocessError) as exc:
            self._json({"error": str(exc)}, 503)

    def _companion_form(self, path: str):
        bootstrap = path == "/app/session"
        if not self._allowed_request(auth=False, bootstrap=bootstrap):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096 or self.headers.get("Transfer-Encoding"):
                raise ValueError("Invalid companion form length")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
                raise ValueError("Invalid companion form content type")
            self.connection.settimeout(10)
            values = parse_qs(self.rfile.read(length).decode("utf-8"),
                              strict_parsing=True, max_num_fields=1)
            field = "ticket" if bootstrap else "token"
            if set(values) != {field} or len(values[field]) != 1:
                raise ValueError("Invalid companion form")
            if bootstrap:
                page, policy = self.app.ui.session_page(values[field][0])
                self._bytes(page, "text/html; charset=utf-8", policy=policy)
            elif self.app.ui.enabled and self.app.authorized("Bearer " + values[field][0]):
                url = urlsplit(self.path.replace("/app/download/", "/outputs/", 1))
                self._output(url)
            else:
                self._json({"error": "Session expired. Open Jamly again."}, 401)
        except (ValueError, UnicodeError) as exc:
            self.close_connection = True
            self._json({"error": str(exc)}, 400)


def main(argv=None) -> int:
    parser = build_parser()
    try:
        args = parse_configured_args(parser, argv, preferences=True)
        server = ItemServer(args)
    except (OSError, ValueError, QueueError) as exc:
        print("jamly-server: %s" % exc, file=sys.stderr)
        return 2
    print("Jamly listening on %s:%d" % (args.host, server._server.server_address[1]), flush=True)
    print("Pair browsers with: " + token_command(server.state_dir), flush=True)
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt
    previous_signal = signal.signal(signal.SIGTERM, interrupt)
    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        signal.signal(signal.SIGTERM, previous_signal)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
