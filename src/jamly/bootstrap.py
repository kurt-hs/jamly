"""Dependency detection for the installed pipeline.

Python dependencies are declared by the package. ffmpeg and ffprobe remain system tools and are
resolved from explicit directories, the project scope, or PATH.
Environment knobs (all optional; sensible defaults used otherwise):
    JAMLY_SCOPE      directory used for legacy tool lookups (default: current working dir)
Run directly to print a human-readable tool report and an exit code reflecting whether
the pipeline is runnable *as-is* on this machine.
"""
from __future__ import annotations
import os
import sys
from dataclasses import dataclass
from .core import (_abs, ENGINE_HTDEMUCS, PLATFORM_WINDOWS, _os_family, detect_engine, which)  # noqa: E402
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
# yt-dlp
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
    # 1. Reuse a legacy project-local standalone, probing both the flat
    #    <scope>/.portable/<f> direct child and (on Windows) the <os_family>/ subdir so a
    #    downloaded build is found regardless of layout; without both forms we'd miss it.
    names = ("yt-dlp.exe", "yt-dlp_x86.exe", "yt-dlp") if PLATFORM_WINDOWS else ("yt-dlp",)
    candidates = [os.path.join(portable, f) for f in names]
    if PLATFORM_WINDOWS:
        for sub in ("win_x86", "win32"):
            for f in names:
                candidates.append(os.path.join(portable, sub, f))
    else:
        for sub in dict.fromkeys((_os_family(), "linux", "macos")):
            for f in names:
                candidates.append(os.path.join(portable, sub, f))
    for c in candidates:
        s = _try(c)
        if s.ok:
            return s
    # 2. this box's existing %APPDATA%\yt-dlp_win_x86 (reuse, no re-download)
    # portable fallback: derive %APPDATA% from the user profile, never a hard-coded path
    appdata = os.environ.get("APPDATA", "") or os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Roaming")
    for cand in [os.path.join(appdata, "yt-dlp_win_x86", n)
                 for n in ("yt-dlp.exe", "yt-dlp_x86.exe", "yt-dlp")]:
        s = _try(cand)
        if s.ok:
            return s
    # 3. PATH fallback: a system / standalone yt-dlp reachable via `which`. Without this a host that
    #    has yt-dlp only on PATH (no portable build, no %APPDATA%\yt-dlp_win_x86) reports "missing"
    #    even though it is usable -- the review's "yt-dlp isn't found" bug. The pinned portable build
    #    and the box's known install still win (checked above); PATH is the last resort.
    on_path = which("yt-dlp")
    if on_path:
        s = _try(on_path)
        if s.ok:
            return s
    return ToolStatus("yt-dlp")
# --------------------------------------------------------------------------- #
# ffmpeg + ffprobe (static build ships BOTH; this custom yt-dlp needs both)
# --------------------------------------------------------------------------- #
def detect_ffmpeg(ffmpeg_dirs: list = None, scope_dir: str = "", additional_dirs=()) -> ToolStatus:
    """Return the first dir that contains BOTH ffmpeg.exe and ffprobe.exe.
    A directory with only ffmpeg is reported as WARN because yt-dlp metadata and media probing
    require ffprobe as well.
    """
    if PLATFORM_WINDOWS:
        names = ("ffmpeg.exe", "ffprobe.exe")
    else:
        names = ("ffmpeg", "ffprobe")
    dirs = list(ffmpeg_dirs or []) + list(additional_dirs)
    ffmpeg_on_path = which("ffmpeg")
    ffprobe_on_path = which("ffprobe")
    if ffmpeg_on_path and ffprobe_on_path:
        ff_dir = os.path.dirname(os.path.abspath(ffmpeg_on_path))
        fp_dir = os.path.dirname(os.path.abspath(ffprobe_on_path))
        if ff_dir == fp_dir:
            dirs.append(ff_dir)
    scope = os.path.abspath(os.path.expanduser(scope_dir)) if scope_dir else os.getcwd()
    portable_ffmpeg = os.path.join(scope, ".portable", "ffmpeg")
    dirs.append(os.path.join(portable_ffmpeg, _os_family()))
    for d in dirs:
        if not os.path.isdir(d):
            continue
        # _dir_has_both is the single definition of "this dir holds BOTH ffmpeg + ffprobe"; every
        # reusable-dir check routes through it so that notion never drifts into a second place.
        if _dir_has_both(d):
            return ToolStatus("ffmpeg", path=d, ok=True, note="has ffmpeg + ffprobe")
        if os.path.isfile(os.path.join(d, names[0])):
            return ToolStatus("ffmpeg", path=d, ok=False,
                              note="ffmpeg only (ffprobe missing)")
    # scoped fallback: binaries may be nested under .portable/<os>/ffmpeg/* in any subfolder
    port_root = os.path.join(scope, ".portable", _os_family(), "ffmpeg")
    if os.path.isdir(port_root):
        def _scoped(name):
            for dp, _dirs, filenames in os.walk(port_root):
                fn = filenames
                if name in fn and os.path.isfile(os.path.join(dp, name)):
                    return os.path.join(dp, name)
            return ""
        ff_path = _scoped(names[0])
        fp_path = _scoped(names[1])
        dff = os.path.dirname(ff_path) if ff_path else ""
        dfp = os.path.dirname(fp_path) if fp_path else ""
        if ff_path and fp_path and dff == dfp:
            return ToolStatus("ffmpeg", path=dff, ok=True,
                              note="has ffmpeg + ffprobe (scoped)")
    return ToolStatus("ffmpeg")
def find_ffmpeg(scope_dir: str = ""):
    """Return an absolute path to a dir holding BOTH ffmpeg+ffprobe, else None.
    Thin wrapper over :func:`detect_ffmpeg` used by run.py / server mode so the caller
    can pass that exact directory to yt-dlp's ``--ffmpeg-location`` and to the mix step.
    """
    ts = detect_ffmpeg(scope_dir=scope_dir)
    return ts.path if ts.ok else None
def _dir_has_both(root: str) -> bool:
    """True if `root` holds BOTH ffmpeg+ffprobe (bare names on POSIX, .exe on Windows)."""
    try:
        ext = ".exe" if PLATFORM_WINDOWS else ""
        ff = os.path.join(_abs(root), "ffmpeg%s" % ext)
        fp = os.path.join(_abs(root), "ffprobe%s" % ext)
        return os.path.isfile(ff) and os.path.isfile(fp)
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# orchestration helpers for the CLI report
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RunReport:
    """The single source of truth for a run's dependency status. Produced by summarize only;
    main derives its readiness gates from this instead of re-running detectors."""
    python: ToolStatus
    ytdlp: ToolStatus
    ffmpeg: ToolStatus
    engine: ToolStatus

    @property
    def ready(self) -> bool:
        return all(entry.ok for entry in (
            self.python, self.ytdlp, self.ffmpeg, self.engine))

def summarize(scope_dir: str = "") -> RunReport:
    """Build one dependency status report without importing heavy packages."""
    py = detect_system_python()
    ytdlp = find_ytdlp(scope_dir)
    ffmpeg = detect_ffmpeg(scope_dir=scope_dir)
    engine = detect_engine(scope_dir=scope_dir)
    return RunReport(py, ytdlp, ffmpeg, ToolStatus(
        "htdemucs",
        path=engine,
        ok=engine == ENGINE_HTDEMUCS,
        note="demucs CLI (%s)" % ("available" if engine == ENGINE_HTDEMUCS else "not available"),
    ))
def main(argv=None) -> int:

    scope = getattr(os, "environ", {}).get("JAMLY_SCOPE") or os.getcwd()

    print("jamly dependency detection")
    print("=" * 60)

    report = summarize(scope_dir=scope)          # runs each detector exactly once; single source of truth
    for entry in (report.python, report.ytdlp, report.ffmpeg, report.engine):
        print(entry.report())

    print("=" * 60)
    if report.ready:
        print("STATUS: ready to run")
        return 0
    reasons = []
    if not report.python.ok:
        reasons.append("no system python interpreter found")
    if not report.ffmpeg.ok:
        reasons.append("ffmpeg+ffprobe directory not found (need BOTH binaries)")
    if not report.ytdlp.ok:
        reasons.append("yt-dlp is not installed or not on PATH")
    if not report.engine.ok:
        reasons.append("demucs is not installed")
    print("STATUS: missing dependencies -> %s" % "; ".join(reasons))
    return 3


if __name__ == "__main__":
    sys.exit(main())