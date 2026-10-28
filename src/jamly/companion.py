"""Launch or reconnect to the local exporter without coupling jobs to a browser tab."""
from __future__ import annotations

import argparse
from dataclasses import replace
import html
import hmac
import json
import os
from pathlib import Path
import subprocess
import secrets
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, url2pathname
import webbrowser

from filelock import FileLock

from .bootstrap import check_runtime, detect_ffmpeg, summarize
from .config import (
    instance_path, pairing_token, pairing_proof, parse_configured_args, pipeline_options, runtime_paths,
    state_directory,
)
from .runtime import module_command
from .server import _is_loopback_host, build_parser
from .watch_queue import QueueError, queue_owner


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(base_url: str, token: str, path: str, body=None) -> dict:
    headers = {"Authorization": "Bearer " + token} if token else {}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    call = Request(base_url + path, data=data, headers=headers)
    # Local pairing credentials must never be sent through an environment-configured proxy.
    with build_opener(ProxyHandler({}), _NoRedirect()).open(call, timeout=3) as response:
        payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError("The local backend returned an oversized control response")
        result = json.loads(payload)
    if not isinstance(result, dict):
        raise ValueError("The local backend returned an invalid response")
    return result


def _instance_url(path: Path) -> str | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid companion instance file: %s" % path)
    host, port = data.get("host"), data.get("port")
    if (not isinstance(host, str) or not _is_loopback_host(host)
            or type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("Invalid companion instance file: %s" % path)
    return "http://%s:%d" % ("[" + host + "]" if ":" in host else host, port)


def _connect(url: str, token: str, queue: Path) -> bool:
    try:
        nonce = secrets.token_hex(32)
        handshake = request(url, "", "/handshake?nonce=" + nonce)
        proof = handshake.get("proof")
        expected = pairing_proof(token, nonce, url, queue)
        if not isinstance(proof, str) or not hmac.compare_digest(proof.encode(), expected.encode()):
            raise RuntimeError("The local backend identity does not match this queue. Stop the other "
                               "process before reopening Jamly; no pairing credential was sent.")
    except HTTPError as exc:
        raise RuntimeError("The port belongs to an incompatible or unpaired backend; stop it first") from exc
    except URLError as exc:
        if isinstance(exc.reason, (ConnectionRefusedError, TimeoutError)):
            return False
        raise
    if handshake.get("api_version") != 3:
        raise RuntimeError("Update and restart the backend before opening this companion")
    return True


def _server_arguments(args, state: Path, queue: Path) -> list[str]:
    values = dict(pipeline_options(args), state_dir=str(state), queue=str(queue),
                  host=args.host, port=args.port)
    command = []
    for key, value in values.items():
        option = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                command.append(option)
        elif value is not None:
            command.extend((option, str(value)))
    for origin in args.allow_origin:
        command.extend(("--allow-origin", origin))
    for host in args.allow_host:
        command.extend(("--allow-host", host))
    return command


def ensure_backend(args, state: Path, queue: Path, token: str) -> str:
    instance = instance_path(state, queue)
    deadline = time.monotonic() + 5
    while True:
        existing = _instance_url(instance)
        if existing and _connect(existing, token, queue):
            return existing
        try:
            with queue_owner(str(queue)):
                break
        except QueueError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Another Jamly process owns this queue but its companion is unavailable. "
                                   "Wait for the CLI to finish, or stop the older backend.")
            time.sleep(.1)
    report = summarize()
    if args.ffmpeg_dir:
        report = replace(report, ffmpeg=detect_ffmpeg([args.ffmpeg_dir]))
    report = check_runtime(report)
    if not report.ready:
        raise RuntimeError("Runtime check failed:\n" + "\n".join(item.report() for item in report.entries))
    args.ffmpeg_dir = report.ffmpeg.path
    environment = dict(os.environ, JAMLY_SERVER_TOKEN=token)
    command = module_command("jamly.server", *_server_arguments(args, state, queue))
    log_path = state / "server.log"
    with os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as log:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=environment, cwd=state, start_new_session=os.name != "nt",
            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
                          if os.name == "nt" else 0,
        )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("The backend could not start. See " + str(log_path))
            url = _instance_url(instance)
            if url and _connect(url, token, queue):
                return url
            time.sleep(.1)
        raise RuntimeError("The backend did not become ready. See " + str(log_path))
    except BaseException:
        if process.poll() is None:
            from .core import _terminate
            _terminate(process)
        raise


def show_startup_error(state: Path, message: str) -> None:
    state.mkdir(parents=True, exist_ok=True)
    path = state / "startup-error.html"
    content = (
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Jamly could not start</title>'
        '<h1>Jamly could not start</h1><pre>%s</pre>'
        '<p>Reinstall the companion if bundled dependencies are missing. '
        'For a backend conflict, quit the older Jamly instance before reopening this one.</p></html>'
    ) % html.escape(message)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
        file.write(content)
    if not webbrowser.open(path.as_uri()):
        print("Open this diagnostic page: " + path.as_uri(), file=sys.stderr)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    parser.description = "Open the local Jamly exporter; closing the page does not stop jobs"
    parser.add_argument("--no-browser", action="store_true", help="Print the private launch-file location")
    state = state_directory()
    no_browser = "--no-browser" in argv
    try:
        args = parse_configured_args(parser, argv, preferences=True)
        state, queue = runtime_paths(args)
        if not _is_loopback_host(args.host):
            raise ValueError("The desktop companion only connects to loopback addresses")
        state.mkdir(parents=True, exist_ok=True)
        token = pairing_token(state, args.server_token)
        with FileLock(str(state / "companion-launch.lock"), timeout=60):
            url = ensure_backend(args, state, queue, token)
            overrides = argparse.ArgumentParser(add_help=False)
            for key, flags in (("model", ("--model",)), ("audio_format", ("--audio-format",)),
                               ("output_dir", ("--output-dir", "-o"))):
                overrides.add_argument(*flags, dest=key, default=argparse.SUPPRESS)
            supplied, _rest = overrides.parse_known_args(argv)
            options = pipeline_options(args)
            explicit = {key: options[key] for key in vars(supplied)}
            if explicit:
                request(url, token, "/settings", explicit)
            launch = request(url, token, "/app/launch", {})
        uri = launch.get("launch_file", "")
        parsed = urlsplit(uri)
        if (parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment
                or Path(unquote(parsed.path)).name == ""):
            raise ValueError("The backend returned an invalid launch-file location")
        path = Path(url2pathname(parsed.path))
        if not path.resolve().is_relative_to((state / "browser-sessions").resolve()) or not path.is_file():
            raise ValueError("The backend returned a launch file outside this companion's private state")
        if args.no_browser:
            print(json.dumps({"url": url, "launch_file": uri}))
        elif not webbrowser.open(uri):
            raise RuntimeError("No default browser could be opened. Open " + uri)
        return 0
    except (OSError, ValueError, RuntimeError, QueueError) as exc:
        message = str(exc)
        print("Jamly: " + message, file=sys.stderr)
        if not no_browser:
            show_startup_error(state, message)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
