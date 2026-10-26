"""Dependency detection for the portable pipeline (bootstrap phase).
This module is intentionally **detection-only** and pure standard-library so it can be
unit-tested without any network or heavy installs. The later provisioning steps
(autodownloading yt-dlp / ffmpeg + ffprobe / ONNX model) hook into these same
functions' results, but are not implemented here -- see DESIGN.md section 10 for the
provisioning plan and `find_*`/status return values as their contracts.
Environment knobs (all optional; sensible defaults used otherwise):
    AUTOCV_SCOPE      directory that will hold `.portable/` (default: current working dir)
Run directly to print a human-readable tool report and an exit code reflecting whether
the pipeline is runnable *as-is* on this machine.
"""
from __future__ import annotations
import os
import sys
from .config_state import (ConfigStore, default_config_path)
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
    # 1. official per-OS standalone inside the portable scope. Probe BOTH the flat
    #    <scope>/.portable/<f> direct child and (on Windows) the <os_family>/ subdir, because
    #    _scan_known_roots records a downloaded build in either layout; without both forms a cached
    #    portable yt-dlp would be re-scanned every launch.
    names = ("yt-dlp.exe", "yt-dlp_x86.exe", "yt-dlp") if PLATFORM_WINDOWS else ("yt-dlp",)
    candidates = [os.path.join(portable, f) for f in names]
    if PLATFORM_WINDOWS:
        for sub in ("win_x86", "win32"):
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
# --------------------------------------------------------------------------- #
# ffmpeg + ffprobe (static build ships BOTH; this custom yt-dlp needs both)
# --------------------------------------------------------------------------- #
def detect_ffmpeg(ffmpeg_dirs: list = None, scope_dir: str = "", additional_dirs=()) -> ToolStatus:
    """Return the first dir that contains BOTH ffmpeg.exe and ffprobe.exe.
    The bundled UVR5 ffmpeg.exe has no matching ffprobe.exe, so a directory with only
    ffmpeg is reported as WARN (usable for some yt-dlp builds but not this one). See
    DESIGN.md section 2 / blocked item: an ffprobe binary must be sourced separately.
    """
    if PLATFORM_WINDOWS:
        names = ("ffmpeg.exe", "ffprobe.exe")
    else:
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
        # _dir_has_both is the single definition of "this dir holds BOTH ffmpeg + ffprobe" --
        # reused by the curated-root scan (_scan_known_roots) so reuse never splits across two checks.
        if _dir_has_both(d):
            return ToolStatus("ffmpeg", path=d, ok=True, note="has ffmpeg + ffprobe")
        if os.path.isfile(os.path.join(d, names[0])):
            return ToolStatus("ffmpeg", path=d, ok=False,
                              note="ffmpeg only (ffprobe missing)")
    # scoped fallback: binaries may be nested under .portable/<os>/ffmpeg/* in any subfolder
    port_root = os.path.join(scope, ".portable", _os_family(), "ffmpeg")
    if os.path.isdir(port_root):
        def _scoped(name):
            for entry in os.walk(port_root):
                # defensive unpack: a malformed host path can make os.walk yield odd-shaped
                # tuples -- we only need dirpath + filenames, so one bad component never aborts.
                try:
                    dp, fn = entry
                except ValueError:
                    dp, fn = entry[0], []
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
# curated cross-machine install-root discovery (§11 / DESIGN.md §10)
# --------------------------------------------------------------------------- #
def _dir_has_both(root: str) -> bool:
    """True if `root` holds BOTH ffmpeg+ffprobe (bare names on POSIX, .exe on Windows)."""
    try:
        ext = ".exe" if PLATFORM_WINDOWS else ""
        ff = os.path.join(_abs(root), "ffmpeg%s" % ext)
        fp = os.path.join(_abs(root), "ffprobe%s" % ext)
        return os.path.isfile(ff) and os.path.isfile(fp)
    except OSError:
        return False
def _curated_app_roots() -> list:
    r"""Well-known app locations that may bundle ffmpeg/ffprobe or ship UVR5.
    Probed AFTER PATH-first so a host's own installs win; this is best-effort coverage of the
    common cases cross-machine (not an exhaustive scan of every copy on one box). Returns a
    de-duplicated list of candidate directories: ``AppData\Local\Programs`` (where UVR5 and most
    editors/muxers install) and ``AppData\Roaming`` (winget/npm installs, standalone yt-dlp).
    Pure file checks -- no heavy imports.
    """
    roots = []
    seen = set()
    def _add(path: str):
        # Only add genuine directories; skip bogus cwd-relative paths that arise when APPDATA /
        # USERPROFILE are unset (e.g. under git-bash) so os.walk never sees a malformed root.
        ap = os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
        if not ap or ap in seen:
            return
        try:
            is_dir = os.path.isdir(ap)
        except OSError:
            is_dir = False
        if not is_dir:
            return
        seen.add(ap)
        roots.append(ap)
    appdata_roaming = os.environ.get("APPDATA", "")
    userprofile = os.environ.get("USERPROFILE", "")
    # AppData\Local\Programs: UVR5 + apps that ship ffmpeg/ffprobe install here as <app>.
    # AppData\Local\Programs + AppData\Roaming\Local\Programs: UVR5 GUI and apps that ship
    # ffmpeg/ffprobe here as <app>\.  Both siblings are required so we don't miss the common case.
    _add(os.path.join(appdata_roaming, "Local", "Programs"))
    _add(os.path.join(userprofile, "AppData", "Local", "Programs"))
    # AppData\Roaming: standalone yt-dlp (yt-dlp_win_x86), winget/npm tool folders, etc.
    _add(os.path.join(appdata_roaming, "Roaming"))
    return roots
def _looks_like_ytdlp(path: str) -> bool:
    """True if `path` is an executable named yt-dlp (accepts .exe/.bat, rejects .py). Pure check."""
    name = os.path.basename(os.path.normpath(path))
    return (os.path.isfile(path) and not path.endswith(".py")
            and any(n in name for n in ("yt-dlp", "yt-dlp.exe", "yt-dlp_x86.exe", "yt-dlp.bat")))
def find_known_roots(scope_dir: str = "", additional_dirs=(), config_path=None, force=False) -> dict:
    """Return reusable install roots for this run.
    Fast path: when a valid cache exists at ``config_path`` (defaults to the scope root's
    ``splitfy.json``), the recorded paths are returned immediately -- no filesystem scan,
    so every run after the first is cheap and can report *reused vs downloaded*. Only when the
    cache is empty/invalid, or ``force=True``, do we call :func:`_scan_known_roots` (the slow,
    exhaustive probe) once and persist its results for next time.
    See :func:`_scan_known_roots` for the semantics of each category. Pure stdlib; no network.
    """
    cfg = ConfigStore.from_path(config_path or default_config_path(scope_dir))
    if not force and not cfg.empty:
        return _load_cached(cfg)
    fresh = _scan_known_roots(scope_dir=scope_dir, additional_dirs=tuple(additional_dirs))
    store = ConfigStore.from_path(config_path or default_config_path(scope_dir))
    # Persist only when this run actually discovered a usable root for the category: either it
    # found >=2 candidates (a real multi-path install) or there was no prior record. A thin scan
    # (<=1 candidate) on an already-known category is treated as noise, so we keep the previously
    # recorded path instead of clobbering a good root with an empty/thin result.
    for cat in ("ffmpeg", "yt-dlp"):
        fresh_paths = list(fresh[cat])
        had_prior = bool(store.get_paths(cat))
        if len(fresh_paths) > 1 or not had_prior:
            store.record(cat, fresh_paths)
    try:
        store.save()
    except OSError:
        pass  # caching is best-effort; discovery still returns the freshly-scanned paths
    return fresh
def _scan_known_roots(scope_dir: str = "", additional_dirs=(), uvr_scan=True) -> dict:
    """Exhaustive filesystem probe of reusable install roots (PATH-first, then curated).
    Returns::
        {"ffmpeg": [dir...], "yt-dlp": [path...]}
    This is the *slow* path -- invoked only once per unique scope when no cache exists. See
    :func:`find_known_roots`. A directory with only ffmpeg is NOT listed here (needs BOTH);
    see :func:`detect_ffmpeg` for that WARN-only case.
    ``uvr_scan`` gates the one-time UVR5-weight-repo probe; pass False to exercise the fast
    PATH/curated probes against temp dirs in isolation (the UVR scan is separately unit-tested).
    """
    out = {"ffmpeg": [], "yt-dlp": []}
    def _norm(path: str) -> str:
        return os.path.normpath(_abs(os.path.expanduser(os.path.expandvars(path))))                .replace("\\", "/")
    # --- ffmpeg: PATH-first, then curated app roots (a dir with BOTH binaries is reusable) ---
    path_ffmpeg = which("ffmpeg")
    if path_ffmpeg and _dir_has_both(os.path.dirname(_abs(path_ffmpeg))):
        out["ffmpeg"].append(_abs(path_ffmpeg))
    for d in list(additional_dirs) + [os.path.join(scope_dir, ".portable", "ffmpeg")]:
        ap = os.path.abspath(os.path.expanduser(d))
        if not os.path.isdir(ap):
            continue
        # only directories shipping BOTH binaries are reusable targets; see :func:`_dir_has_both`
            continue
        if _dir_has_both(ap):
            out["ffmpeg"].append(ap)
    for root in _curated_app_roots():
        # extra guard: skip anything that is not a real directory (defensive; roots are already
        # dir-filtered at creation) -- discovery must never crash on a malformed path.
        if not os.path.isdir(root):
            continue
        try:
            for level, entry in enumerate(os.walk(root)):
                # a malformed host path can make os.walk yield odd-shaped tuples -- unpack defensively so
                # one bad component never aborts the whole walk and hides legitimate bundled-app roots.
                try:
                    dp = entry[0]
                except ValueError:
                    dp = ""
                if level > 3:
                    continue
                # only directories shipping BOTH binaries are reusable targets; see :func:`_dir_has_both`
                if not _dir_has_both(dp):
                    continue
                out["ffmpeg"].append(_abs(dp))
        except (OSError, ValueError):
            # a malformed host path can make os.walk yield odd-shaped tuples -- skip gracefully
            pass
    # --- yt-dlp: this box's AppData standalone, then curated roots (PATH-first) ---
    y_appdata = os.environ.get("APPDATA", "")
    for cand in [os.path.join(y_appdata, "yt-dlp_win_x86", n)
                 for n in ("yt-dlp.exe", "yt-dlp_x86.exe", "yt-dlp")]:
        if _looks_like_ytdlp(cand):
            out["yt-dlp"].append(_abs(cand))
    # portable-scope standalone yt-dlp -- symmetric with the AppData probe layout (win_x86/)
    port_ytdlp = os.path.join(scope_dir, ".portable", "yt-dlp")
    for f in ("yt-dlp.exe", "yt-dlp_x86.exe", "yt-dlp"):
        fp = _abs(os.path.join(port_ytdlp, f))
        if _looks_like_ytdlp(fp) and not any(f.startswith(x) for x in out["yt-dlp"]):
            out["yt-dlp"].append(fp)
    win_x86 = os.path.join(port_ytdlp, "win_x86")
    if os.path.isdir(win_x86):
        for f in ("yt-dlp.exe", "yt-dlp_x86.exe", "yt-dlp"):
            fp = _abs(os.path.join(win_x86, f))
            if _looks_like_ytdlp(fp) and not any(f.startswith(x) for x in out["yt-dlp"]):
                out["yt-dlp"].append(fp)
    for root in _curated_app_roots():
        if os.path.isdir(root):
            def _find(name, exts):
                hit = ""
                # bounded walk: count descent levels from `root` (not absolute components)
                try:
                    for level, entry in enumerate(os.walk(root)):
                        # defensive unpack (see walk above): only dirpath + filenames are needed.
                        try:
                            dp, fn = entry
                        except ValueError:
                            dp, fn = entry[0], []
                        if level > 3:
                            continue
                        if name in fn and any(fn[0].endswith(e) for e in exts):
                            hit = _abs(os.path.join(dp, fn[0]))
                            break
                except (OSError, ValueError):
                    pass  # malformed host path -- skip this curated root gracefully
                return hit
            for p in (_find("yt-dlp", (".exe", ".bat")), _find("yt-dlp", ())):
                if p and not any(p.startswith(x) for x in out["yt-dlp"]):
                    out["yt-dlp"].append(p)
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}  # de-dup, order-preserving
def _load_cached(cfg: "ConfigStore") -> dict:
    """Return cached roots (ffmpeg + yt-dlp) as-is, or empty if nothing recorded."""
    out = {"ffmpeg": list(cfg.get_paths("ffmpeg")), "yt-dlp": list(cfg.get_paths("yt-dlp"))}
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}
def _exists_exe(path: str) -> bool:
    """Return True if `path` is an existing executable (existence on Windows; X_OK elsewhere)."""
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
def summarize(scope_dir: str = "") -> list:
    """Build the ordered status report used by `main`.
    Portable-only: there is exactly one separation engine (the demucs CLI), so there is no UVR5
    install, bundled weight repo, or uvr-root ffmpeg to report. The htdemucs entry reflects whether
    that single engine's weights are usable on this box -- the only thing `main` gates on for repro.
    """
    py = detect_system_python()
    ytdlp = find_ytdlp(scope_dir)
    ffmpeg = detect_ffmpeg(scope_dir=scope_dir)
    engine = detect_engine(scope_dir=scope_dir)
    has_repro = bool(ytdlp.ok) or (engine == ENGINE_HTDEMUCS)
    return [py, ytdlp, ffmpeg, ToolStatus(
        "htdemucs",
        path=engine,
        ok=has_repro,
        note="demucs CLI (%s)" % ("available" if has_repro else "not available"),
    )]
def main(argv=None) -> int:

    scope = getattr(os, "environ", {}).get("AUTOCV_SCOPE") or os.getcwd()

    print("splitfy dependency detection")
    print("=" * 60)

    for s in summarize(scope_dir=scope):
        print(s.report())

    # critical gates for a runnable pipeline *as-is* -- portable, single-engine:
    ffmpeg_both = detect_ffmpeg(scope_dir=scope)
    python_ok = detect_system_python().ok
    ytdlp_ok = find_ytdlp(scope).ok
    has_repro = bool(ytdlp_ok) or (detect_engine(scope_dir=scope) == ENGINE_HTDEMUCS)

    print("=" * 60)
    if ffmpeg_both.ok and python_ok and has_repro:
        print("STATUS: ready to run")
        return 0
    reasons = []
    if not python_ok:
        reasons.append("no system python interpreter found")
    if not ffmpeg_both.ok:
        reasons.append("ffmpeg+ffprobe directory not found (need BOTH binaries)")
    if not has_repro:
        reasons.append("yt-dlp and/or htdemucs repro not detected")
    print("STATUS: needs provisioning -> %s" % "; ".join(reasons))
    return 3


if __name__ == "__main__":
    sys.exit(main())