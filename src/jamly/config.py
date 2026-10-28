"""Shared paths, pipeline settings, and local browser pairing."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess

from filelock import FileLock
from platformdirs import user_data_path


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
        "threads": args.threads,
        "device": getattr(args, "device", "auto"),
        "replace_existing": args.replace_existing,
        "ffmpeg_dir": str(Path(ffmpeg_dir).expanduser().absolute()) if ffmpeg_dir else None,
    }


def job_arguments(args: argparse.Namespace, options: dict) -> argparse.Namespace:
    result = argparse.Namespace(**vars(args))
    for key in ("output_dir", "audio_format", "threads", "device", "replace_existing", "ffmpeg_dir"):
        if key in options:
            setattr(result, key, options[key])
    return result


def token_path(state_dir: Path) -> Path:
    return state_dir / "server-token"


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


def parse_configured_args(parser: argparse.ArgumentParser, argv=None) -> argparse.Namespace:
    preliminary, _ = parser.parse_known_args(argv)
    path = getattr(preliminary, "config", None)
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        allowed = {action.dest for action in parser._actions} - {"help", "config"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("unsupported server configuration fields")
        # Feed values back through argparse so types and choices are validated too.
        config_args = []
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
        import sys
        return parser.parse_args(config_args + list(sys.argv[1:] if argv is None else argv))
    return parser.parse_args(argv)
