"""Cross-platform orchestration helpers shared by run.py / server.py / bootstrap.py.

Design notes:
  * Every subprocess call is made with an **argument list** (never a shell string),
    so paths containing spaces (e.g. `C:\\Users\\Kurt\\AppData\\...`) work on both native
    Windows and this git-bash box without any shlex quoting.
  * Tool binaries are passed as full absolute paths; resolution lives in bootstrap/run,
    not here, so these builders stay pure and unit-testable.
  * No third-party imports -> import-safe anywhere Python 3 runs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List, Optional

PLATFORM_WINDOWS = sys.platform == "win32"


# --------------------------------------------------------------------------- #
# path / platform helpers
# --------------------------------------------------------------------------- #
def _abs(path: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def _exists_exe(path: str) -> bool:
    p = _abs(path)
    if not os.path.isfile(p):
        return False
    if PLATFORM_WINDOWS:
        # require an executable extension on Windows; the OS resolves it.
        exts = (".exe", ".cmd", ".bat")
        return os.path.splitext(p)[1].lower() in exts or os.access(p, os.X_OK)
    return bool(os.access(p, os.X_OK))


def which(program: str) -> Optional[str]:
    """Return the first path on PATH that matches `program`, else None.

    On Windows this also accepts names with a trailing .exe/.bat (e.g. "ffmpeg").
    """
    if shutil.which is not None:
        found = shutil.which(program, mode=os.X_OK)
        if found:
            return found
    env_path = os.environ.get("PATH", "")
    for d in env_path.split(os.pathsep):
        if not d:
            continue
        cand = os.path.join(d, program)
        if _exists_exe(cand):
            return cand
    # Windows fallback: search PATH with common extensions.
    if PLATFORM_WINDOWS and not found:
        for ext in (".exe", ".bat", ".cmd"):
            f = shutil.which(program + ext)
            if f:
                return f
    return None


# --------------------------------------------------------------------------- #
# process runner
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    args: List[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(args: List[str], check: bool = False, capture: bool = True) -> RunResult:
    """Run `args` as a list (no shell). Returns the completed process.

    On failure prints the last ~20 lines of stderr for debugging but does not raise,
    so callers can inspect returncode and continue (e.g. across a queue).
    """
    cmd = [str(a) for a in args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
        )
    except FileNotFoundError as exc:  # tool path is wrong / not found
        return RunResult(args=cmd, returncode=-1, stdout="", stderr="NOT FOUND: %s" % " ".join(cmd[:3]))
    if check and proc.returncode != 0:
        _print_tail(proc.stderr)
        raise RuntimeError("command failed (exit %d): %s" % (proc.returncode, " ".join(cmd[:4])))
    return RunResult(args=cmd, returncode=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")


def _print_tail(text: str, limit: int = 20) -> None:
    if not text.strip():
        return
    lines = [l for l in text.splitlines() if l.strip()]
    sys.stderr.write("\n".join(lines[-limit:]) + "\n")


# --------------------------------------------------------------------------- #
# command builders (pure; inputs already resolved to absolute paths)
# --------------------------------------------------------------------------- #
def build_ytdlp_extract_cmd(
    ytdlp_exe: str,
    url: str,
    output_template: str = "%(title)s.%(ext)s",
    ffmpeg_dir: str = "",
) -> List[str]:
    """yt-dlp best-audio extraction; container decided natively by the stream.

    NOTE: this box ships a custom yt-dlp build that rejects `--no-subs`, so it is NOT passed.
    `--ffmpeg-location` must point at a dir containing BOTH ffmpeg.exe and ffprobe.exe, else the
    mux step is intermittent (see PLAN.md section 7).
    """
    cmd = [str(ytdlp_exe), str(url)]
    if ffmpeg_dir:
        cmd += ["--extract-audio", "--audio-format", "best", "--audio-quality", "0",
                "--ffmpeg-location", str(ffmpeg_dir)]
    else:
        # no custom muxer -> let yt-dlp's bundled ffmpeg handle it (may need ffprobe too)
        cmd += ["--extract-audio", "--audio-format", "best", "--audio-quality", "0"]
    cmd += ["-o", str(output_template)]
    return cmd


def build_demucs_four_stems_cmd(
    python_exe: str,
    input_audio: str,
    workdir: str,
    repo: str,
    weights_name: str = "955717e8-8726e21a",
    device: Optional[str] = None,
    threads: int = 0,
) -> List[str]:
    """audio-separator Demucs v4 four-stems (reuses UVR5's bundled `.th` weights).

    Emits <workdir>/separated/<model>/<track>/{drums,bass,other,vocals}.wav.
    `device=None` -> let audio-separator auto-detect; "cuda"/"directml" override.
    """
    cmd = [str(python_exe), "-m", "audio_separator.demucs.separate"]
    cmd += ["--model", "htdemucs", "--name", str(weights_name)]
    if repo:
        cmd += ["--repo", str(repo)]
    cmd += ["--four-stems", "--input", str(input_audio), "-o", str(workdir)]
    if device is not None:
        cmd += ["--device", str(device)]
    if threads and threads > 0:
        cmd += ["--threads", str(threads)]
    return cmd


def build_ffmpeg_mix_cmd(
    ffmpeg_exe: str,
    bass: str,
    other: str,
    vocals: str,
    output: str,
) -> List[str]:
    """Mix non-drum stems (bass+other+vocals) into the 2-channel no-drums file."""
    return [str(ffmpeg_exe), "-nostdin", "-y", "-i", _abs(bass), "-i", _abs(other),
            "-i", _abs(vocals), "-shortest", "-map", "0?", "-mixer_channels", "2", str(output)]


# --------------------------------------------------------------------------- #
# naming / output-dir helpers
# --------------------------------------------------------------------------- #
def sanitize_filename(name: str, allowed: Optional[str] = None) -> str:
    """Return a path-safe version of `name` for use in filenames/labels.

    Strips characters that break Windows paths and normalizes separators; keeps the
    rest verbatim (including unicode). `allowed` optionally restricts to an explicit set.
    """
    if allowed is not None:
        return "".join(c for c in name.lower() if c in allowed)
    unsafe = r"\/:*?\"<>|"
    out = [c if c != "/" else "-" for c in name]  # replace path separators with hyphen
    out = [c if c not in unsafe else "" for c in out]
    return "".join(out).strip().strip(".")


def ensure_output_dir(path: str) -> str:
    os.makedirs(_abs(path), exist_ok=True)
    return _abs(path)
