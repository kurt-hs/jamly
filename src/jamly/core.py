"""Subprocess and filename helpers shared by the pipeline and service."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Sequence

from .models import DEFAULT_MODEL, validate_model
from .runtime import module_command

PLATFORM_WINDOWS = os.name == "nt"
ENGINE_AUTO = "auto"
ENGINE_HTDEMUCS = "htdemucs"
FORMAT_WAV = "wav"
FORMAT_MP3 = "mp3"
AUDIO_FORMATS = (FORMAT_WAV, FORMAT_MP3)
DEFAULT_OUTPUT_FORMAT = FORMAT_MP3


def _abs(path: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def which(program: str) -> str | None:
    return shutil.which(program)


def _exists_exe(path: str) -> bool:
    return os.path.isfile(path) and (PLATFORM_WINDOWS or os.access(path, os.X_OK))


def ensure_output_dir(path: str) -> str:
    os.makedirs(_abs(path), exist_ok=True)
    return _abs(path)


def command_prefix(command: str | Sequence[str]) -> list[str]:
    return [command] if isinstance(command, str) else list(command)


@dataclass
class RunResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def check_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Processing was cancelled")


def _terminate(process: subprocess.Popen) -> None:
    if PLATFORM_WINDOWS:
        taskkill = str(Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe")
        subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if PLATFORM_WINDOWS:
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()


def run(args: Sequence[str], check: bool = False, capture: bool = True,
        stream: bool = False, on_line=None, cancel_event=None,
        timeout: float | None = None, env=None) -> RunResult:
    """Run an argument list; cancellation terminates only this command's process tree."""
    cmd = [str(arg) for arg in args]
    check_cancelled(cancel_event)
    try:
        process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture or stream else None,
            stderr=subprocess.STDOUT if stream else subprocess.PIPE if capture else None,
            text=True, encoding="utf-8", errors="replace",
            env=env,
            start_new_session=not PLATFORM_WINDOWS,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
                          if PLATFORM_WINDOWS else 0,
        )
    except FileNotFoundError:
        return RunResult(cmd, -1, "", "NOT FOUND: %s" % cmd[0])
    start = time.monotonic()
    errors = []
    tail = deque(maxlen=30)
    reader = None

    def pump():
        try:
            for line in process.stdout:
                tail.append(line)
                sys.stdout.write(line)
                sys.stdout.flush()
                if on_line is not None:
                    on_line(line)
        except Exception as exc:
            errors.append(exc)

    try:
        if stream:
            reader = threading.Thread(target=pump, daemon=True)
            reader.start()
        while True:
            check_cancelled(cancel_event)
            if timeout is not None and time.monotonic() - start > timeout:
                raise TimeoutError("Command exceeded %.0f seconds: %s" % (timeout, cmd[0]))
            if errors:
                raise errors[0]
            try:
                if stream:
                    process.wait(timeout=0.1)
                    reader.join(timeout=3)
                    if reader.is_alive():
                        raise RuntimeError("Command output did not close")
                    if errors:
                        raise errors[0]
                    stdout, stderr = "", "".join(tail) if process.returncode else ""
                else:
                    stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        result = RunResult(cmd, process.returncode, stdout or "", stderr or "")
        if check and not result.ok:
            raise RuntimeError("command failed (exit %d): %s" % (
                result.returncode, result.stderr.strip() or cmd[0]))
        return result
    except BaseException:
        _terminate(process)
        raise
    finally:
        if reader is not None:
            reader.join(timeout=3)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


def build_ytdlp_extract_cmd(
    ytdlp_exe: str | Sequence[str],
    url: str,
    output_template: str = "%(title)s.%(ext)s",
    ffmpeg_dir: str = "",
) -> list[str]:
    from .bootstrap import ytdlp_options
    cmd = command_prefix(ytdlp_exe) + ytdlp_options()
    cmd += ["-f", "bestaudio/best", "--extract-audio", "--audio-format", "best",
            "--audio-quality", "0", "--newline", "--no-write-thumbnail",
            "--no-write-info-json", "--max-downloads", "1"]
    if ffmpeg_dir:
        cmd += ["--ffmpeg-location", ffmpeg_dir]
    return cmd + ["-o", output_template, "--", url]


def build_stem_encode_cmd(ffmpeg_exe: str, input_wav: str,
                          output_path: str, fmt: str = FORMAT_MP3) -> list[str] | None:
    if fmt == FORMAT_WAV:
        return None
    if fmt != FORMAT_MP3:
        raise ValueError("Unsupported audio format: %s" % fmt)
    return [ffmpeg_exe, "-nostdin", "-y", "-i", _abs(input_wav),
            "-c:a", "libmp3lame", "-b:a", "320k", output_path]


def sanitize_filename(name: str, allowed: str | None = None) -> str:
    if allowed is not None:
        return "".join(char for char in name.lower() if char in allowed)
    name = str(name).replace("/", "-").replace("\\", "-")
    cleaned = " ".join("".join(
        char for char in name if ord(char) >= 32 and char not in ':*?"<>|'
    ).split()).strip(" .")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        "%s%d" % (prefix, number)
        for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    if cleaned.split(".", 1)[0].upper() in reserved:
        cleaned = "_" + cleaned
    return cleaned.encode("utf-8")[:180].decode("utf-8", "ignore").rstrip(" .") or "untitled"


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def detect_engine(scope_dir: str = "") -> str | None:
    return ENGINE_HTDEMUCS if _module_available("demucs") else None


def find_python() -> str:
    return sys.executable


def build_htdemucs_four_stems_cmd(python_exe: str, input_audio: str, workdir: str,
                                  model_name: str = ENGINE_HTDEMUCS, threads: int = 0,
                                  device: str = "auto") -> list[str]:
    cmd = module_command("jamly.demucs_worker", "--out", workdir, "-n", model_name,
                         executable=None if python_exe == sys.executable else python_exe)
    if threads:
        cmd += ["-j", str(threads)]
    return cmd + ["--device", device, input_audio]


def separate_stems(audio: str, workdir: str, threads: int = 0,
                   device: str = "auto", cancel_event=None, ffmpeg_dir=None,
                   model: str = DEFAULT_MODEL) -> dict[str, str]:
    validate_model(model)
    if not detect_engine():
        raise RuntimeError("demucs is missing; reinstall Jamly")
    output = Path(workdir) / "stems"
    cmd = build_htdemucs_four_stems_cmd(find_python(), audio, str(output),
                                      model_name=model, threads=threads, device=device)
    environment = dict(os.environ)
    environment["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    environment["HF_HUB_DISABLE_TELEMETRY"] = "1"
    if ffmpeg_dir:
        environment["PATH"] = str(ffmpeg_dir) + os.pathsep + environment.get("PATH", "")
    result = run(cmd, cancel_event=cancel_event, env=environment)
    if not result.ok:
        raise RuntimeError("Demucs failed: %s" % result.stderr.strip())
    paths = {name: str(output / (name + ".wav"))
             for name in ("drums", "bass", "other", "vocals")}
    if not all(Path(path).is_file() for path in paths.values()):
        raise RuntimeError("Demucs did not produce all four stems")
    return paths
