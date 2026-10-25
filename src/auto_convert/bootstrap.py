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
import platform
import sys

from .config_state import (ConfigStore, default_config_path)
from .core import (_abs, PLATFORM_WINDOWS, _scan_uvr_roots, which)  # noqa: E402


def _os_family() -> str:
    """Coarse OS family used for the portable scope layout (win/linux/macos)."""
    if sys.platform.startswith("win"):
        return "win"
    machine = platform.machine().lower()
    if any(a in machine for a in ("arm64", "aarch64")):
        return "macos" if sys.platform == "darwin" else "linux-arm"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


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
    # scoped fallback: binaries may be nested under .portable/<os>/ffmpeg/* in any subfolder
    port_root = os.path.join(scope, ".portable", _os_family(), "ffmpeg")
    if os.path.isdir(port_root):
        def _scoped(name):
            for entry in os.walk(port_root):
                try: dp, dn, fn = entry
                except ValueError: dp, dn, fn = entry[0], [], []
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


def find_ffmpeg(scope_dir: str = "") -> Optional[str]:
    """Return an absolute path to a dir holding BOTH ffmpeg+ffprobe, else None.

    Thin wrapper over :func:`detect_ffmpeg` used by run.py / server mode so the caller
    can pass that exact directory to yt-dlp's ``--ffmpeg-location`` and to the mix step.
    """
    ts = detect_ffmpeg(scope_dir=scope_dir)
    return ts.path if ts.ok else None


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


# --------------------------------------------------------------------------- #
# curated cross-machine install-root discovery (§11 / PLAN.md §10)
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
    home = _abs(userprofile) if (userprofile and PLATFORM_WINDOWS) else \
          (_abs(os.path.expandvars("~")) if not PLATFORM_WINDOWS else "")

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
    ``auto_convert.json``), the recorded paths are returned immediately -- no filesystem scan,
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
    for cat in ("ffmpeg", "yt-dlp", "uvr_root"):
        paths = list(fresh[cat])
        if len(paths) <= 1 and store.get_paths(cat):
            # keep the previously-recorded path when this run found nothing new; only add it
            # if it is genuinely absent (avoids clobbering a useful root with an empty scan)
            pass
        else:
            store.record(cat, list(fresh[cat]))
    try:
        store.save()
    except OSError:
        pass  # caching is best-effort; discovery still returns the freshly-scanned paths
    return fresh


def _scan_known_roots(scope_dir: str = "", additional_dirs=(), uvr_scan=True) -> dict:
    """Exhaustive filesystem probe of reusable install roots (PATH-first, then curated).

    Returns::

        {"ffmpeg": [dir...], "yt-dlp": [path...], "uvr_root": [root...]}

    This is the *slow* path -- invoked only once per unique scope when no cache exists. See
    :func:`find_known_roots`. A directory with only ffmpeg is NOT listed here (needs BOTH);
    see :func:`detect_ffmpeg` for that WARN-only case.

    ``uvr_scan`` gates the one-time UVR5-weight-repo probe; pass False to exercise the fast
    PATH/curated probes against temp dirs in isolation (the UVR scan is separately unit-tested).
    """
    out = {"ffmpeg": [], "yt-dlp": [], "uvr_root": []}

    # normalized marker to skip this box's UVR5-bundled ffmpeg (surfaced via uvr_roots, not here)
    uvr_marker = "programs/ultimate vocal remover"

    def _norm(path: str) -> str:
        return os.path.normpath(_abs(os.path.expanduser(os.path.expandvars(path))))                .replace("\\", "/")

    # --- ffmpeg: PATH-first, then curated app roots (skip the UVR5 bundled copy; it is a uvr root) ---
    path_ffmpeg = which("ffmpeg")
    if path_ffmpeg and _dir_has_both(os.path.dirname(_abs(path_ffmpeg))):
        out["ffmpeg"].append(_abs(path_ffmpeg))

    for d in list(additional_dirs) + [os.path.join(scope_dir, ".portable", "ffmpeg")]:
        ap = os.path.abspath(os.path.expanduser(d))
        if not os.path.isdir(ap):
            continue
        # skip the UVR5 bundled copy: it is surfaced via uvr_roots (not a portable reuse target)
        if uvr_marker in _norm(d).lower():
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
                try: dp, dn, fn = entry
                except ValueError: dp, dn, fn = entry[0], [], []
                if level > 3:
                    continue
                rel = _norm(dp).lower()
                # only directories shipping BOTH binaries are reusable targets; the UVR5 bundled copy is a uvr root (skipped above)
                if not _dir_has_both(dp):
                    continue
                if uvr_marker in rel:
                    continue  # reported via uvr_root, not here
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
                        try: dp, dn, fn = entry
                        except ValueError: dp, dn, fn = entry[0], [], []
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

    # --- UVR5 installs shipping the reusable Demucs weight repo (reuses _scan_uvr_roots) ---
    if uvr_scan:
        uvr_roots = list(_scan_uvr_roots())
        out["uvr_root"] = uvr_roots

    return {k: list(dict.fromkeys(v)) for k, v in out.items()}  # de-dup, order-preserving


def _load_cached(cfg: "ConfigStore") -> dict:
    """Return cached roots (all three categories) as-is, or empty if nothing recorded."""
    out = {"ffmpeg": list(cfg.get_paths("ffmpeg")), "yt-dlp": list(cfg.get_paths("yt-dlp")),
           "uvr_root": list(cfg.get_paths("uvr_root"))}
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
