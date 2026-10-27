"""Cross-platform orchestration helpers shared by run.py / server.py / bootstrap.py.

Design notes:
  * Every subprocess call is made with an **argument list** (never a shell string),
    so paths containing spaces (e.g. a Windows user path with folders) work on both native
    Windows and this git-bash box without any shlex quoting.
  * Tool binaries are passed as full absolute paths; resolution lives in bootstrap/run,
    not here, so these builders stay pure and unit-testable.
  * No third-party imports -> import-safe anywhere Python 3 runs.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional

PLATFORM_WINDOWS = sys.platform == "win32"


def _os_family() -> str:
    """Coarse OS family for portable-scope layout and asset naming (win/linux/macos).

    Single source of truth for portable-scope layout; shared by bootstrap + run so the .portable/<os>
    tree is built consistently. Returns one of ``"win"``, ``"linux"`` or ``"macos"`` (or linux-arm).
    """
    if PLATFORM_WINDOWS:
        return "win"
    machine = platform.machine().lower()
    if any(a in machine for a in ("arm64", "aarch64")):
        return "macos" if sys.platform == "darwin" else "linux-arm"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


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


# Friendly labels for where a tool binary was found -- used by run.py to report install type.
# Pure path inspection (no subprocess), so it stays unit-testable. kind: portable|app|path.
_APP_DIRS = (
    "Shotcut", "Blender", "DaVinci Resolve", "OBS Studio", "Kdenlive", "Avidemux",
    "HandBrake", "PotPlayer", "VLC media player", "Audacity", "FFmpeg-Builds",
)
_PATH_LOC_HINTS = {
    r"AppData\Local\Microsoft\Winget": "winget",
    r"Program Files\ffmpeg": "install",
    r"yt-dlp_win_x86": "AppData",
}


def _classify_path(path: str) -> dict:
    """Return {kind, label} for a tool binary path.

    ``kind`` is one of ``portable`` (under the scope's ``.portable/``), ``app`` (inside a known
    editor/muxer app dir that bundles ffmpeg/yt-dlp), or ``path`` (PATH / winget / npm). ``label``
    is a short human tag: an app name, ``portable``, or the PATH/winget/AppData location.
    """
    ap = _abs(os.path.normpath(path))
    parts = [p.lower() for p in ap.split(os.sep) if p]

    # portable scope: anything under <scope>/.portable/...
    if ".portable" in parts:
        return {"kind": "portable", "label": "portable"}

    # known app install dirs (ffmpeg-carrying editors / muxers)
    for name in _APP_DIRS:
        if any(name.lower() == p or name.lower().replace(" ", "_") == p for p in parts):
            return {"kind": "app", "label": name}

    # path-based installs: winget, npm, AppData tool folders, etc.
    label = "PATH"
    for hint, tag in _PATH_LOC_HINTS.items():
        if any(hint.lower() == p or hint.replace(" ", "_").lower() == p for p in parts):
            return {"kind": "path", "label": tag}
        if any(hint.lower() in p for p in parts):
            label = tag
    if "npm" in parts:
        return {"kind": "path", "label": "AppData"}  # AppData\Roaming\npm (winget/npm)
    return {"kind": "path", "label": label}


def tool_source(path: str) -> dict:
    """Public wrapper over :func:`_classify_path` for a resolved tool binary path."""
    return _classify_path(path or "")


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


def run(args: List[str], check: bool = False, capture: bool = True,
        stream: bool = False, on_line=None) -> RunResult:
    """Run `args` as a list (no shell). Returns the completed process.

    Default behaviour is unchanged. ``stream=True`` writes subprocess stdout/stderr live to the
    terminal so yt-dlp's own ``[download] 95% ...`` progress shows in real time; when streaming, output
    is NOT captured (RunResult.stdout / .stderr are empty -- inspect returncode instead). The optional
    ``on_line(line)`` callback fires with each decoded line during streaming so callers can react
    (e.g. render a download bar); it never raises.
    """
    cmd = [str(a) for a in args]
    try:
        if stream:
            # Live mode: pump stdout/stderr to the terminal as they arrive, then wait.
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            def _pump():
                while True:
                    line = proc.stdout.readline()
                    if line == "":  # EOF
                        break
                    sys.stdout.write(line.rstrip("\n") + "\n")
                    sys.stdout.flush()
                    if on_line:
                        on_line(line)

            _pump()
            returncode = proc.wait()
            if check and returncode != 0:
                raise RuntimeError("command failed (exit %d): %s" % (returncode, " ".join(cmd[:4])))
            # Live mode captured nothing into text buffers -- report empty strings (never the live
            # pipe handle) so RunResult stays a plain string view for callers.
            return RunResult(args=cmd, returncode=returncode, stdout="", stderr="")
        else:
            proc = subprocess.run(cmd, capture_output=capture, text=True)
    except FileNotFoundError:  # tool path is wrong / not found
        return RunResult(args=cmd, returncode=-1, stdout="", stderr="NOT FOUND: %s" % " ".join(cmd[:3]))
    if check and proc.returncode != 0:
        # Streamed output already went to the terminal live; only tail captured stderr.
        _print_tail(proc.stderr)
        raise RuntimeError("command failed (exit %d): %s" % (proc.returncode, " ".join(cmd[:4])))
    return RunResult(args=cmd, returncode=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")


def _print_tail(text: str, limit: int = 20) -> None:
    if not text.strip():
        return
    lines = [line for line in text.splitlines() if line.strip()]
    sys.stderr.write("\n".join(lines[-limit:]) + "\n")


# --------------------------------------------------------------------------- #
# command builders (pure; inputs already resolved to absolute paths)
# --------------------------------------------------------------------------- #
def build_ytdlp_extract_cmd(
    ytdlp_exe: str,
    url: str,
    output_template: str = "%(title)s.%(ext)s",
    ffmpeg_dir: str = "",
    suppress_subs: bool = True,
) -> List[str]:
    """yt-dlp best-audio extraction; container decided natively by the stream.

    ``suppress_subs`` is an explicit knob (not a hidden per-host assumption): when True the tool
    asks yt-dlp to skip subtitle downloads for this lossless-audio pipeline. It defaults True so
    every host behaves identically; pass False only if your specific build rejects the flag -- see
    DESIGN.md section 7.
    """
    cmd = [str(ytdlp_exe), str(url)]
    if suppress_subs:
        cmd += ["--no-subs"]
    if ffmpeg_dir:
        # a dir with only ffmpeg.exe works for some yt-dlp builds but not this one; pass a dir
        # that ships BOTH ffmpeg.exe and ffprobe.exe (see DESIGN.md section 7).
        cmd += ["--extract-audio", "--audio-format", "best", "--audio-quality", "0",
                "--ffmpeg-location", str(ffmpeg_dir)]
    else:
        # no custom muxer -> let yt-dlp's bundled ffmpeg handle it (may need ffprobe too)
        cmd += ["--extract-audio", "--audio-format", "best", "--audio-quality", "0"]
    cmd += ["-o", str(output_template)]
    return cmd





# --------------------------------------------------------------------------- #
# output audio format
# --------------------------------------------------------------------------- #
# Final per-stem encoding. WAV is lossless/raw (the stem is copied verbatim); MP3 is a
# reasonably-encoded lossy default. The table maps each lossy format to its ffmpeg codec +
# constant-bitrate; an empty entry means "no re-encode" (raw).
FORMAT_WAV = "wav"        # lossless / raw -- no re-encode, stem copied verbatim
FORMAT_MP3 = "mp3"         # lossy default -- libmp3lame V0 320kbps (broadly playable)

# All formats the CLI accepts. ORDER matters: it becomes argparse's choice order.
AUDIO_FORMATS = (FORMAT_WAV, FORMAT_MP3)
DEFAULT_OUTPUT_FORMAT = FORMAT_MP3   # lossy is the default -- smaller files out of the box

# Per-format ffmpeg encoder settings for lossy targets. V0 320kbps MP3 keeps near-CD quality while
# staying far smaller than WAV and playing in Windows media libraries, generic players, and drag-and-drop.
_LOSSY_ENCODERS = {
    FORMAT_MP3: {"codec": "libmp3lame", "bitrate": "320k"},
}


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


def build_stem_encode_cmd(
    ffmpeg_exe: str,
    input_wav: str,
    output_path: str,
    fmt: str = FORMAT_MP3,
) -> Optional[List[str]]:
    """Build the ffmpeg command that re-encodes one stem WAV into `fmt`.

    Returns ``None`` for raw (lossless) targets -- no re-encode is needed, so the caller copies the
    source verbatim instead. Pure command builder (no subprocess), so it stays unit-testable and the
    encode-or-copy decision lives in one place shared by run.py / server.py.
    """
    enc = _LOSSY_ENCODERS.get(fmt)
    if not enc:
        return None   # raw WAV target -> caller copies verbatim, no ffmpeg call
    cmd = [str(ffmpeg_exe), "-nostdin", "-y", "-i", _abs(input_wav),
           "-c:a", enc["codec"], "-b:a", enc["bitrate"], str(output_path)]
    return cmd


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


# --------------------------------------------------------------------------- #
# stem separation. Portable-only: there is a single path -- the torch-based `demucs` PyPI CLI
# (`python -m demucs separate`). No UVR5/audio-separator reuse, no reused `.th` weights, and no
# onnxruntime model detection.
# --------------------------------------------------------------------------- #
ENGINE_AUTO = "auto"
ENGINE_HTDEMUCS = "htdemucs"  # the only separation engine; four-source via the official `demucs` CLI


def _site_packages() -> str:
    """Return a directory that contains installed packages (no import side effects)."""
    for p in sys.path:
        if not p or p == "-":
            continue
        cand = os.path.join(_abs(p), "site-packages") if PLATFORM_WINDOWS else \
               os.path.join(_abs(p))
        if os.path.isdir(cand):
            return cand
    return ""





def detect_engine(scope_dir: str = "") -> Optional[str]:
    """Availability check for the single separation engine (no heavy imports).

    Returns ``ENGINE_HTDEMUCS`` when the torch-based `demucs` PyPI CLI is importable from site-packages,
    else ``None``. Portable-only: there is no local UVR5/audio-separator reuse and no onnxruntime model
    detection -- a missing package means separation is not possible rather than falling back to another engine.
    """
    if _dir_has_any(_site_packages(), ("demucs",)):
        return ENGINE_HTDEMUCS
    return None


def _dir_has_any(root: str, names) -> bool:
    try:
        return any(n in os.listdir(_abs(root)) for n in names)
    except OSError:
        return False


def find_python() -> Optional[str]:
    """Return a usable python interpreter path (shutil.which + sys.executable)."""
    if sys.executable and _exists_exe(sys.executable):
        return sys.executable
    for name in ("python3", "python"):
        found = which(name)
        if found:
            return found
    return None


def build_htdemucs_four_stems_cmd(
    python_exe: str,
    input_audio: str,
    workdir: str,
    model_name: str = "htdemucs",
    threads: int = 0,
) -> List[str]:
    """Official Demucs PyPI CLI separation (torch-based; NOT the onnxruntime variant).

    Mirrors ``python -m demucs separate``: ``-n/--name htdemucs`` selects the 4-source model,
    and ``--out`` places results in <workdir>/separated/<model>/<track>/{drums,bass,other,vocals}.wav
    (exactly what :func:`_locate_stems` walks). No ``--four-stems`` flag exists -- source count comes
    from the model. Requires ``pip install demucs``.
    """
    cmd = [str(python_exe), "-m", "demucs", "separate", "--audio", str(input_audio),
           "--out", str(workdir), "-n", str(model_name)]
    if threads and threads > 0:
        cmd += ["-j", str(threads)]
    return cmd


def _locate_stems(workdir: str) -> List[str]:
    """Return the absolute paths of {drums,bass,other,vocals}.wav under workdir/separated/*/."""
    found = []
    for root, _dirs, files in os.walk(os.path.join(_abs(workdir), "separated")):
        for f in sorted(files):
            if f.lower().endswith(".wav"):
                stem = os.path.splitext(f)[0]
                if stem in ("drums", "bass", "other", "vocals"):
                    found.append((stem, _abs(os.path.join(root, f))))
    return [p for _, p in sorted(found)]


def separate_stems(
    audio: str,
    engine: str = ENGINE_AUTO,
    workdir: str = "",
    threads: int = 0,
    python_exe: Optional[str] = None,
) -> dict:
    """Separate `audio` into four stems; return {track: abs_path} for the WAVs found.

    Portable-only: always runs the torch-based `demucs` PyPI CLI (`python -m demucs separate`). No UVR5/
    audio-separator reuse and no local `.th` weights -- separation fetches its own weights on demand. Raises
    RuntimeError (with last stderr lines) if separation fails or the demucs package is not installed. Heavy
    imports are lazy so importing this module never pulls torch.
    """
    py = python_exe or find_python()
    if not py:
        raise RuntimeError("no python interpreter found for stem separation")
    workdir = _abs(workdir) if workdir else os.path.join(_abs(os.getcwd()), "stems", _sanitize(audio))
    os.makedirs(workdir, exist_ok=True)

    # The maintained Demucs v4 CLI lives in the `demucs` PyPI package (torch-based); detect it by its
    # site-packages directory. Lightweight and import-free so importing this module never pulls torch.
    if not (_dir_has_any(_site_packages(), ("demucs",))):
        raise RuntimeError("the demucs package is not installed; run `pip install demucs` to enable four-stem separation")

    cmd = build_htdemucs_four_stems_cmd(py, audio, workdir, threads=threads)
    res = run(cmd)
    if not res.ok:
        _print_tail(res.stderr)
        raise RuntimeError("demucs four-stem separation failed (exit %d): %s" % (res.returncode, " ".join(cmd[:4])))

    stems = _locate_stems(workdir)
    if len(stems) < 4:
        raise RuntimeError("expected 4 stems but found %d under %s" % (len(stems), workdir))
    return {os.path.splitext(os.path.basename(p))[0]: p for p in stems}


def _sanitize(audio: str) -> str:
    # reuse core's own path-safety helper (no cross-module import needed)
    return sanitize_filename(audio or "audio")

