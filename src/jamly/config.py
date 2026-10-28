"""Shared paths, pipeline settings, and local browser pairing."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import tempfile

from filelock import FileLock
from platformdirs import user_data_path

from .models import DEFAULT_MODEL, MODELS, validate_model


def state_directory(value: str | None = None) -> Path:
    return Path(
        value or os.environ.get("JAMLY_STATE_DIR")
        or os.environ.get("JAMLY_SCOPE") or user_data_path("jamly", appauthor=False)
    ).expanduser().absolute()


def default_output_directory() -> str:
    return str(Path(os.environ.get("JAMLY_OUTPUT_DIR", str(Path.home() / "Music" / "jamly")))
               .expanduser().absolute())


def runtime_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    queue = args.queue or os.environ.get("JAMLY_QUEUE")
    explicit_state = (args.state_dir or os.environ.get("JAMLY_STATE_DIR")
                      or os.environ.get("JAMLY_SCOPE"))
    state = (state_directory(explicit_state) if explicit_state or not queue
             else Path(queue).expanduser().absolute().parent)
    return state, Path(queue).expanduser().absolute() if queue else state / "jamly_queue.json"


def nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def add_pipeline_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", "--scope", dest="state_dir", default=None,
                        help="Jamly state directory (default: per-user application data)")
    parser.add_argument("--queue", default=None, help="Override the queue file")
    parser.add_argument("--ffmpeg-dir", default=None, help="Directory containing ffmpeg and ffprobe")
    parser.add_argument("-o", "--output-dir", default=default_output_directory())
    parser.add_argument("--audio-format", choices=("mp3", "wav"), default="mp3")
    parser.add_argument("--model", choices=tuple(MODELS), default=DEFAULT_MODEL,
                        help="htdemucs: Balanced (default); htdemucs_ft: Higher quality, about 4x slower")
    parser.add_argument("-t", "--threads", type=nonnegative_int, default=0,
                        help="Parallel Demucs jobs; higher values use more memory (default: 0)")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda", "mps"), default="auto")
    parser.add_argument("--replace-existing", action="store_true",
                        help="Replace a completed track/format directory")


def pipeline_options(args: argparse.Namespace) -> dict:
    ffmpeg_dir = getattr(args, "ffmpeg_dir", None)
    return {
        "output_dir": str(Path(args.output_dir).expanduser().absolute()),
        "audio_format": args.audio_format,
        "model": getattr(args, "model", DEFAULT_MODEL),
        "threads": args.threads,
        "device": getattr(args, "device", "auto"),
        "replace_existing": args.replace_existing,
        "ffmpeg_dir": str(Path(ffmpeg_dir).expanduser().absolute()) if ffmpeg_dir else None,
    }


def job_arguments(args: argparse.Namespace, options: dict) -> argparse.Namespace:
    result = argparse.Namespace(**vars(args))
    # Jobs saved before model selection was introduced always used Balanced.
    result.model = options.get("model", DEFAULT_MODEL)
    for key in ("output_dir", "audio_format", "threads", "device", "replace_existing", "ffmpeg_dir"):
        if key in options:
            setattr(result, key, options[key])
    return result


def token_path(state_dir: Path) -> Path:
    return state_dir / "server-token"


def instance_path(state: Path, queue: Path) -> Path:
    identity = hashlib.sha256(str(queue).encode()).hexdigest()[:16]
    return state / ("server-" + identity + ".json")


def pairing_proof(token: str, nonce: str, url: str, queue: Path) -> str:
    context = json.dumps(["jamly-backend-v1", nonce, url, os.path.normcase(str(queue.resolve()))],
                         separators=(",", ":")).encode()
    return hmac.new(token.encode(), context, hashlib.sha256).hexdigest()


def token_command(state_dir: Path) -> str:
    command = ["jamly-service", "token", "--state-dir", str(state_dir)]
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def pairing_token(state_dir: Path, explicit: str | None = None) -> str:
    token = explicit or os.environ.get("JAMLY_SERVER_TOKEN")
    if token:
        if not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("the server token must contain only non-whitespace ASCII characters")
        return token
    state_dir.mkdir(parents=True, exist_ok=True)
    path = token_path(state_dir)
    with FileLock(str(path) + ".lock", timeout=5):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            token = path.read_text(encoding="utf-8").strip()
        else:
            token = secrets.token_urlsafe(32)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(token + "\n")
    if not token or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("invalid pairing token file: %s" % path)
    return token


def validate_preferences(data: dict) -> dict:
    if not isinstance(data, dict) or set(data) - {"model", "audio_format", "output_dir"}:
        raise ValueError("Settings accept only model, audio_format and output_dir")
    result = dict(data)
    if "model" in data:
        validate_model(data["model"])
    if "audio_format" in data and data["audio_format"] not in ("mp3", "wav"):
        raise ValueError("audio_format must be mp3 or wav")
    if "output_dir" in data:
        value = data["output_dir"]
        if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("Enter an absolute output directory")
        try:
            path = Path(value).expanduser()
        except RuntimeError as exc:
            raise ValueError("Cannot expand the output directory") from exc
        if not path.is_absolute():
            raise ValueError("Enter an absolute output directory")
        if path.exists() and not path.is_dir():
            raise ValueError("The output location is a file, not a directory")
        result["output_dir"] = str(path)
    return result


def write_private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + "-", delete=False) as file:
            temporary = Path(file.name)
            json.dump(data, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parse_configured_args(parser: argparse.ArgumentParser, argv=None, *,
                          preferences: bool = False) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    preliminary, _ = parser.parse_known_args(argv)
    path = getattr(preliminary, "config", None)
    config_args = []
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        allowed = {action.dest for action in parser._actions} - {"help", "config"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("unsupported server configuration fields")
        # Feed values back through argparse so types and choices are validated too.
        for action in parser._actions:
            if action.dest not in data:
                continue
            value = data[action.dest]
            option = next((name for name in action.option_strings if name.startswith("--")), None)
            if not option:
                continue
            if isinstance(action, argparse._StoreTrueAction):
                if not isinstance(value, bool):
                    raise ValueError("%s must be a boolean" % action.dest)
                if value:
                    config_args.append(option)
            elif isinstance(action, argparse._AppendAction):
                if not isinstance(value, list):
                    raise ValueError("%s must be a list" % action.dest)
                for entry in value:
                    config_args.extend((option, str(entry)))
            elif value is not None:
                config_args.extend((option, str(value)))
    parsed = parser.parse_args(config_args + argv)
    if preferences:
        state, _queue = runtime_paths(parsed)
        path = state / "preferences.json"
        if path.exists():
            try:
                data = validate_preferences(json.loads(path.read_text(encoding="utf-8")))
            except ValueError as exc:
                raise ValueError("Invalid companion settings in %s: %s" % (path, exc)) from exc
            saved_args = [part for key, value in data.items()
                          for part in ("--" + key.replace("_", "-"), value)]
            parsed = parser.parse_args(config_args + saved_args + argv)
    return parsed
