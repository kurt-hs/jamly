"""Dependency detection for the portable pipeline (bootstrap phase).

This module is intentionally **detection-only** and pure standard-library so it can be
unit-tested without any network or heavy installs. The later provisioning steps
(autodownloading yt-dlp / ffmpeg + ffprobe / ONNX model) hook into these same
functions' results, but are not implemented here -- see PLAN.md section 10 for the
provisioning plan and `find_*`/status return values as their contracts.

Environment knobs (all optional; sensible defaults used otherwise):
    AUTOCV_UVR_ROOT   install root of Ultimate Vocal Remover (default: this box's UVR5 path)
    AUTOCV_SCOPE      directory that will hold `.portable/` (default: current working dir)

Run directly to print a human-readable tool report and an exit code reflecting whether
the pipeline is runnable *as-is* on this machine.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import PLATFORM_WINDOWS, which  # noqa: E402


# --------------------------------------------------------------------------- #
# result types
# --------------------------------------------------------------------------- #
class ToolStatus:
    """Whether a tool is usable for our needs (not merely "somewhere on disk")."""

    __slots__ = ("name", "path", "ok", "note")

    def __init__(self, name: str, path=None, ok=False, note=""):
        self.name = name
        self.path = path
        self.ok = ok
        self.note = note

    @property
    def missing(self) -> bool:
        return not self.ok

    def report(self) -> str:
        tag = "OK   " if self.ok else ("WARN " if getattr(self, "path", None) else "MISS ")
        line = "%-18s [%s] %s" % (self.name, tag, self.path or "-")
        if self.note:
            line += "  (%s)" % self.note
        return line


# --------------------------------------------------------------------------- #
# system python interpreter
# --------------------------------------------------------------------------- #
def detect_system_python() -> ToolStatus:
    """Return the first available Python interpreter.

    On Windows we also try the `python`/`py` launchers; on other platforms the running
    interpreter plus PATH lookups are used. Returns the *interpreter*, not a venv.
    """
    candidates = []
    if sys.executable and os.path.isfile(sys.executable):
        candidates.append(sys.executable)  # guaranteed to exist here
    if which("python"):
        candidates.append(which("python"))
    if PLATFORM_WINDOWS and which("py"):
        candidates.append(which("py"))

    for c in candidates:
        try:
            ver = __import__("subprocess").run([c, "--version"],
                                               capture_output=True, text=True).stdout.splitlines()[0]
        except Exception:
            ver = ""
        return ToolStatus("python", path=c, ok=True, note=ver.strip())
    return ToolStatus("python")


# --------------------------------------------------------------------------- #
# yt-dlp (official per-OS standalone; self-provisioned later)
# --------------------------------------------------------------------------- #
def find_ytdlp(scope_dir: str = "") -> ToolStatus:
    """Search for an existing yt-dlp. Returns the first match, ok=True."""
    scope = os.path.abspath(os.path.expanduser(scope_dir)) if scope_dir else os.getcwd()
    portable = os.path.join(scope, ".portable", "yt-dlp")

    def _try(path: str):
        # accept a bare executable name as well as the common .exe / .bat variants
        name = os.path.basename(os.path.normpath(path))
        ok = (os.path.isfile(path) and not path.endswith(".py")
              and any(name in n for n in ("yt-dlp", "yt-dlp.exe", "yt-dlp_x86.exe", "yt-dlp.bat")))
        return ToolStatus("yt-dlp", path=path, ok=ok)

    # 1. official per-OS standalone inside the portable scope
    if PLATFORM_WINDOWS:
        candidates = [os.path.join(portable, "win_x86", "yt-dlp.exe"),
                      os.path.join(portable, "win32", "yt-dlp.exe"),
                      os.path.join(portable, "win_x86", "yt-dlp"),
                      os.path.join(portable, "win32", "yt-dlp")]
    else:
        candidates = [os.path.join(portable, "linux/yt-dlp"),
                      os.path.join(portable, "macos/yt-dlp")]
    for c in candidates:
        s = _try(c)
        if s.ok:
            return s

    # 2. this box's existing %APPDATA%\yt-dlp_win_x86 (reuse, no re-download)
    appdata = os.environ.get("APPDATA", "") or "C:\\Users\\Kurt\\AppData\\Roaming"
    for cand in [os.path.join(appdata, "yt-dlp_win_x86", n)
                 for n in ("yt-dlp.exe", "yt-dlp_x86.exe", "yt-dlp")]:
        s = _try(cand)
        if s.ok:
            return s


# --------------------------------------------------------------------------- #
# ffmpeg + ffprobe (static build ships BOTH; this custom yt-dlp needs both)
# --------------------------------------------------------------------------- #
def detect_ffmpeg(ffmpeg_dirs: list = None, scope_dir: str = "", additional_dirs=()) -> ToolStatus:
    """Return the first dir that contains BOTH ffmpeg.exe and ffprobe.exe.

    The bundled UVR5 ffmpeg.exe has no matching ffprobe.exe, so a directory with only
    ffmpeg is reported as WARN (usable for some yt-dlp builds but not this one). See
    PLAN.md section 2 / blocked item: an ffprobe binary must be sourced separately.
    """
    if PLATFORM_WINDOWS:
        ext = ".exe"
        names = ("ffmpeg.exe", "ffprobe.exe")
    else:
        ext = ""
        names = ("ffmpeg", "ffprobe")

    dirs = list(ffmpeg_dirs or []) + list(additional_dirs)
    scope = os.path.abspath(os.path.expanduser(scope_dir)) if scope_dir else os.getcwd()
    portable_ffmpeg = os.path.join(scope, ".portable", "ffmpeg")
    if PLATFORM_WINDOWS:
        dirs.append(os.path.join(portable_ffmpeg, "win64"))
    else:
        dirs.append(os.path.join(portable_ffmpeg, "linux"))

    for d in dirs:
        if not os.path.isdir(d):
            continue
        ff = os.path.join(d, names[0])
        fp = os.path.join(d, names[1])
        has_both = os.path.isfile(ff) and os.path.isfile(fp)
        if has_both:
            return ToolStatus("ffmpeg", path=d, ok=True, note="has ffmpeg + ffprobe")
        if os.path.isfile(ff):
            return ToolStatus("ffmpeg", path=d, ok=False,
                              note="ffmpeg only (ffprobe missing)")
    return ToolStatus("ffmpeg")


# --------------------------------------------------------------------------- #
# UVR5 presence + Demucs weights
# --------------------------------------------------------------------------- #
def detect_uvr(uvr_root: str = "") -> dict:
    """Report Ultimate Vocal Remover install state and bundled Demucs repo."""
    root = os.path.abspath(os.path.expanduser(uvr_root)) if uvr_root else ""
    repo = os.path.join(root, "models", "Demucs_Models", "v3_v4_repo") if root else ""
    # canonical UVR5 HTDemucs v4 weight (this box's repo ships exactly this file)
    weights_file = os.path.join(repo, "955717e8-8726e21a.th")

    return {
        "uvr_root": root or None,
        "repo": repo or None,
        "uvr_exists": bool(root) and os.path.isdir(root),
        "repo_exists": bool(repo) and os.path.isdir(repo),
        "weights_file_present": bool(weights_file) and os.path.isfile(weights_file),
        "ffmpeg_bundled": _exists_exe(os.path.join(root, "ffmpeg.exe")) if root else False,
    }


def demucs_weights(repo: str = "", model: str = "htdemucs") -> list:
    """Return `.th` weight basenames usable for this Demucs `model`.

    audio-separator picks a bundled UVR5 weight by `--name <basename> --repo <repo>`; the
    repo is valid when it ships `<repo>/<model>.yaml`, and every `.th` there then maps to
    that model. So we return all `.th` files only when the model's YAML is present.
    """
    if not repo or not os.path.isdir(repo):
        return []
    yaml_name = "%s.yaml" % model
    has_yaml = os.path.isfile(os.path.join(repo, yaml_name))
    if not has_yaml:
        return []
    return sorted(f for f in os.listdir(repo)
                  if f.endswith(".th") and os.path.isfile(os.path.join(repo, f)))


def _exists_exe(path: str) -> bool:
    import os as _os
    p = _os.path.abspath(_os.path.expandvars(path))
    if not _os.path.isfile(p):
        return False
    if PLATFORM_WINDOWS:
        # Windows does not enforce execute bits; existence is enough.
        return True
    try:
        return bool(_os.access(p, _os.X_OK))
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# orchestration helpers for the CLI report
# --------------------------------------------------------------------------- #
def uvr5_ffmpeg_path(uvr_root: str = "") -> ToolStatus:
    """The ffmpeg.exe bundled with UVR5 (no ffprobe sibling)."""
    root = os.path.abspath(os.path.expanduser(uvr_root)) if uvr_root else ""
    p = os.path.join(root, "ffmpeg.exe") if PLATFORM_WINDOWS else os.path.join(root, "ffmpeg")
    return ToolStatus("uvr5-ffmpeg", path=p, ok=_exists_exe(p),
                      note="no ffprobe sibling")


def summarize(scope_dir: str = "", uvr_root: str = "") -> list:
    """Build the ordered status report used by `main`.

    `uvr_root` is threaded through so ffmpeg candidates and weight lookups reflect this
    box's UVR5 install even when no env var is set.
    """
    py = detect_system_python()
    ytdlp = find_ytdlp(scope_dir)
    uvr = detect_uvr(uvr_root)
    weights = demucs_weights(uvr["repo"] if uvr["repo_exists"] else "")
    ffmpeg = detect_ffmpeg(
        additional_dirs=(uvr["uvr_root"] if (uvr_root and uvr["uvr_exists"]) else None),
        scope_dir=scope_dir,
    )
    weights = demucs_weights(uvr["repo"] if uvr["repo_exists"] else "")

    return [py, ytdlp, ffmpeg, uvr5_ffmpeg_path(uvr_root), ToolStatus(
        "htdemucs-weights",
        path=next((w for w in weights), None),
        ok=bool(weights),
        note="(%d weight file(s))" % len(weights),
    ), ToolStatus("uvr5", path=(uvr["uvr_root"] if uvr["uvr_exists"] else None),
                 ok=uvr["weights_file_present"], note="repo + htdemucs.weight present")]


def main(argv=None) -> int:
    scope = os.environ.get("AUTOCV_SCOPE") or os.getcwd()
    root = os.environ.get("AUTOCV_UVR_ROOT") or \
        "C:/Users/Kurt/AppData/Local/Programs/Ultimate Vocal Remover"

    print("auto_convert dependency detection")
    print("=" * 60)
    for s in summarize(scope, root):
        print(s.report())
    weights = demucs_weights(os.path.join(root, "models", "Demucs_Models", "v3_v4_repo") if root else "")
    if weights:
        print("weights:", ", ".join(weights))

    # critical gates for a runnable pipeline *as-is*
    ffmpeg_both = detect_ffmpeg(scope_dir=scope,
                                additional_dirs=(root,)
                                if (root and os.path.isdir(root)) else ())
    python_ok = detect_system_python().ok
    ytdlp_ok = find_ytdlp(scope).ok
    uvr_ok = bool(root) and os.path.isfile(os.path.join(root, "ffmpeg.exe"))
    ytdlp_or_uvr = ytdlp_ok or uvr_ok
    weights_ok = bool(weights)

    print("=" * 60)
    if ffmpeg_both.ok and python_ok and (ytdlp_or_uvr) and weights_ok:
        print("STATUS: ready to run")
        return 0
    reasons = []
    if not python_ok:
        reasons.append("no system python interpreter found")
    if not ffmpeg_both.ok:
        reasons.append("ffmpeg+ffprobe directory not found (need BOTH binaries)")
    if not ytdlp_or_uvr:
        reasons.append("yt-dlp and/or UVR5 not detected")
    if not weights_ok:
        reasons.append("htdemucs Demucs v4 weights not found")
    print("STATUS: needs provisioning -> %s" % "; ".join(reasons))
    return 3


if __name__ == "__main__":
    sys.exit(main())
