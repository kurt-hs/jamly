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
        if any(h.lower() == p or h.replace(" ", "_").lower() == p for p in parts):
            return {"kind": "path", "label": tag}
        if any(h.lower() in p for p in parts):
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

    Default behaviour is unchanged. Two optional flags add friendly output for long steps:
      * ``stream=True`` writes subprocess stdout/stderr live to the terminal so yt-dlp's own
        ``[download] 95% ...`` progress shows in real time (output is still captured and returned).
      * ``on_line(line)`` is called with each decoded line during streaming; callers can react
        (e.g. render a download bar). Never raises -- tool-not-found and read errors are surfaced as
        the process result so callers can inspect returncode and continue (e.g. across a queue).
    """
    cmd = [str(a) for a in args]
    try:
        if stream and not capture:
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
        else:
            proc = subprocess.run(cmd, capture_output=capture, text=True)
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


# --------------------------------------------------------------------------- #
# separation engines (htdemucs ONNX vs UVR5 audio-separator) + four-stem runner
# --------------------------------------------------------------------------- #
ENGINE_AUTO = "auto"
ENGINE_UVR5 = "uvr5"      # audio-separator using the reused UVR5 `.th` weights
ENGINE_HTDEMUCS = "htdemucs"  # canonical ONNX Runtime HTDemucs (.onnx model)

# Demucs four-stem weight repo shipped inside the UVR5 GUI (audio-separator maps these .th files).
DEMUCS_REPO_SUBDIR = os.path.join("models", "Demucs_Models", "v3_v4_repo")
DEMUCS_WEIGHT_FILE = "955717e8-8726e21a.th"  # HTDemucs weight file inside that repo


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


def _env_roots() -> list:
    """Return explicit install-root overrides from env, if any."""
    out = []
    for key in ("AUTOCV_UVR_ROOT", "UVR5_ROOT", "UVR5_PATH"):
        val = os.environ.get(key)
        if val and os.path.isdir(os.path.expanduser(val)):
            out.append(os.path.abspath(os.path.expanduser(val)))
    return out


def _scan_uvr_roots() -> list:
    """Best-effort discovery of UVR5 / audio-separator install roots (no heavy imports).

    A usable local engine is signalled by the Demucs ``.th`` weight repo shipped inside the
    UVR5 GUI, or an importable ``audio_separator`` package in Python site-packages. Returns a
    de-duplicated, ordered list of candidate directories. Pure file checks at bounded depth.
    """
    markers = DEMUCS_REPO_SUBDIR
    weight_file = os.path.join(markers, DEMUCS_WEIGHT_FILE)
    found = []  # unique abspaths
    seen = set()

    def _add(path):
        ap = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(ap) or ap in seen:
            return
        seen.add(ap)
        found.append(ap)

    uvr_names = ("Ultimate Vocal Remover", "Ultimate-Vocal-Remover")
    home = os.environ.get("HOME", "") or os.path.join(os.Getenv("USERPROFILE"), "")

    def _has_weights(root):
        return bool(
            os.path.join(_abs(root), markers) and
            os.path.isfile(os.path.join(_abs(root), weight_file))
        )

    # 1. explicit env overrides (highest priority)
    for r in _env_roots():
        _add(r)

    # 2. the UVR5 app name as a direct marker under common install roots
    scan_dirs = []
    if os.path.isdir(_abs(home)):
        for d in (
            os.path.join(_abs(home), "AppData", "Local"),
            os.path.join(_abs(home), "AppData", "Roaming"),
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
        ):
            if os.path.isdir(d):
                scan_dirs.append(d)
    prog_apps = os.path.join(_abs(home), "AppData", "Local", "Programs")

    # 3. AppData\Local\Programs subtree (where UVR5 GUI typically installs)
    for name in uvr_names:
        _add(os.path.join(prog_apps, name))

    # 4. bounded walk of scan_dirs (depth <= 2) for the weight repo or package marker
    def _bounded(root):
        try:
            for dp, dn, fn in os.walk(os.path.join(_abs(root)), topdown=True):
                if len(dp.split(os.sep)) > 3:  # depth guard
                    continue
                if _has_weights(dp) or any(n in dn for n in uvr_names):
                    _add(dp)
        except OSError:
            pass
    for d in scan_dirs + ([prog_apps] if os.path.isdir(prog_apps) else []):
        try:
            _bounded(d)
        except OSError:
            pass
    return found


def detect_engine(scope_dir: str = "", uvr_root: str = "") -> Optional[str]:
    """Pure availability check for a usable four-stem engine (no heavy imports).

    Returns "htdemucs", "uvr5", or None. `scope_dir`/`uvr_root` let callers point at an
    already-provisioned onnx model dir and/or the UVR5 install that ships `.th` weights.
    """
    # reusable Demucs weights: explicit root, env roots, or discovered install roots
    weight_file = os.path.join(DEMUCS_REPO_SUBDIR, DEMUCS_WEIGHT_FILE)
    if uvr_root:
        uvr_roots = [os.path.abspath(os.path.expanduser(uvr_root))]
    else:
        uvr_roots = _scan_uvr_roots() or []

    weights_present = any(
        os.path.isfile(os.path.join(_abs(b), weight_file)) for b in uvr_roots
    )
    sep_pkg = _dir_has_any(_site_packages(), ("audio_separator",))

    # uvr5 / audio-separator path: reusable Demucs weights present, or the importable package exists
    if weights_present or sep_pkg:
        return ENGINE_UVR5

    # htdemucs ONNX path: package installed AND a model file provisioned somewhere
    if _dir_has_any(_site_packages(), ("htdemucs", "onnxruntime")):
        model_dir = scope_dir if scope_dir else ""
        for root, _dirs, files in os.walk(model_dir):
            for f in files:
                if f.endswith(".onnx"):
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
    """Canonical ONNX Runtime HTDemucs four-stems CLI (loads the `.onnx` model).

    Emits <workdir>/separated/<model>/<track>.wav. Requires `pip install htdemucs`.
    """
    cmd = [str(python_exe), "-m", "htdemucs", "four-stems", "--model", str(model_name),
           "--audio", str(input_audio), "--out", str(workdir)]
    if threads and threads > 0:
        cmd += ["--threads", str(threads)]
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
    uvr_root: str = "",
) -> dict:
    """Separate `audio` into four stems; return {track: abs_path} for the WAVs found.

    Raises RuntimeError (with last stderr lines) if separation fails or no usable engine
    is available. Heavy imports are lazy so importing this module never pulls torch.
    """
    py = python_exe or find_python()
    if not py:
        raise RuntimeError("no python interpreter found for stem separation")
    workdir = _abs(workdir) if workdir else os.path.join(_abs(os.getcwd()), "stems", _sanitize(audio))
    os.makedirs(workdir, exist_ok=True)

    if engine in (ENGINE_AUTO, ENGINE_HTDEMUCS):
        if not (_dir_has_any(_site_packages(), ("htdemucs", "onnxruntime"))):
            raise RuntimeError("engine 'htdemucs' selected but the htdemucs package is not installed")
        cmd = build_htdemucs_four_stems_cmd(py, audio, workdir, threads=threads)
        res = run(cmd)
        if not res.ok:
            _print_tail(res.stderr); raise RuntimeError("htdemucs four-stems failed (exit %d): %s" % (res.returncode, " ".join(cmd[:4])))

    elif engine in (ENGINE_AUTO, ENGINE_UVR5):
        if not (_dir_has_any(_site_packages(), ("audio_separator",))):
            raise RuntimeError("engine 'uvr5' selected but the audio-separator package is not installed")
        r = os.path.abspath(os.path.expanduser(uvr_root)) if uvr_root else ""
        repo = os.path.join(r, "models", "Demucs_Models", "v3_v4_repo") if r else ""
        cmd = build_demucs_four_stems_cmd(py, audio, workdir, repo=repo, threads=threads)
        res = run(cmd)
        if not res.ok:
            _print_tail(res.stderr); raise RuntimeError("audio-separator four-stems failed (exit %d): %s" % (res.returncode, " ".join(cmd[:4])))
    else:
        raise ValueError("unknown engine: %r" % (engine,))

    stems = _locate_stems(workdir)
    if len(stems) < 4:
        raise RuntimeError("expected 4 stems but found %d under %s" % (len(stems), workdir))
    return {os.path.splitext(os.path.basename(p))[0]: p for p in stems}


def _sanitize(audio: str) -> str:
    # reuse core's own path-safety helper (no cross-module import needed)
    return sanitize_filename(audio or "audio")


# UVR5 install root default is resolved by the caller; expose a helper to build its repo path.
def uvr_repo_path(uvr_root: str = "") -> str:
    root = os.path.abspath(os.path.expanduser(uvr_root)) if uvr_root else ""
    return os.path.join(root, "models", "Demucs_Models", "v3_v4_repo") if root else ""
