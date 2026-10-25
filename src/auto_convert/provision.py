"""Self-provisioning for the portable pipeline scope (.portable/).

Reuses an already-installed tool when possible (reuse-first policy, DESIGN.md §10); only
downloads missing pieces into a private `.portable/<os>/` layout so nothing is installed
globally. Currently provisions the ffmpeg + ffprobe static build; detection hooks are
already wired to find it under `<scope>/.portable/<os>/ffmpeg`.

Pure standard-library; network calls isolated in `fetch_*` (unit-testable with a fake opener), and
reuse-first persistence delegated to :class:`ConfigStore` via ``record_provision`` /
``resolve_recorded_tools`` so later launches reuse recorded paths without rescanning.
"""
from __future__ import annotations

import io
import os
import platform
import sys
import tempfile
import zipfile

from .core import PLATFORM_WINDOWS, _abs  # noqa: E402


# --------------------------------------------------------------------------- #
# OS / architecture mapping (BtbN FFmpeg-Builds naming)
# --------------------------------------------------------------------------- #
def _os_family() -> str:
    if sys.platform.startswith("win"):
        return "win"
    machine = platform.machine().lower()
    if any(a in machine for a in ("arm64", "aarch64")):
        return "macos" if sys.platform == "darwin" else "linux-arm"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _win_arch() -> str:
    machine = platform.machine().lower()
    if any(a in machine for a in ("arm64", "aarch64")):
        return "win-arm64"
    arch_bits = str(platform.architecture()[0])
    return "win32" if "32" in arch_bits else "win64"


def _linux_arch() -> str:
    machine = platform.machine().lower()
    if any(a in machine for a in ("arm64", "aarch64")):
        return "linux-arm"
    return "linux"


def build_asset_name(osf: str, arch: str) -> str:
    """Return the BtbN release-asset filename for this platform+arch."""
    if osf == "win":
        return f"ffmpeg-master-latest-{arch}-gpl.zip"
    if osf == "linux-arm":
        return f"ffmpeg-master-latest-linuxarm64-gpl.tar.xz"
    if osf == "macos":
        # BtbN ships macOS as .tar.xz under linux naming; fall back to generic.
        return f"ffmpeg-master-latest-{arch}-gpl.tar.xz"
    return f"ffmpeg-master-latest-{arch}-gpl.tar.xz"


def base_url() -> str:
    return "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"


# --------------------------------------------------------------------------- #
# network helpers (isolated for testability)
# --------------------------------------------------------------------------- #
class FetchError(Exception):
    pass


def fetch_to_bytes(url: str, opener=None, timeout: int = 30) -> bytes:
    """Download `url` into memory. `opener` defaults to urllib; tests inject a fake."""
    if opener is None:
        import urllib.request
        opener = urllib.request.urlopen

    class _Resp:
        def __init__(self, data):
            self._data = data

        def read(self):
            return self._data

    try:
        resp = opener(url, timeout=timeout)  # real opener; fake returns _Resp-like
        if hasattr(resp, "_data"):
            return resp._data                      # injected bytes path
        return resp.read()                         # real HTTP body
    except Exception as exc:  # noqa: BLE001 - surface a single FetchError
        raise FetchError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# extraction helpers
# --------------------------------------------------------------------------- #
def _find_file(root: str, name: str) -> str:
    for dirpath, _dirs, files in os.walk(root):
        if name in files and os.path.isfile(os.path.join(dirpath, name)):
            return os.path.join(dirpath, name)
    return ""


def _extract_archive(zip_path: str, dest_dir: str) -> None:
    """Extract a BtbN build archive (.zip on Windows, .tar.xz elsewhere) into `dest_dir`.`"""
    ext = os.path.splitext(zip_path)[1].lower()
    if ext == ".zip":
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest_dir)
    else:  # .tar.xz / .tgz
        import tarfile, lzma
        with open(zip_path, "rb") as fh, tarfile.open(fileobj=fh, mode="r:") as tf:
            for member in tf.getmembers():
                if not member.isreg():
                    continue
                # extract member-by-member to guard against unsafe absolute paths
                try:
                    safe = os.path.join(dest_dir, os.path.relpath(member.name, "."))
                    extracted = tf.extractfile(member)
                    with open(safe, "wb") as out:
                        for chunk in iter(lambda: extracted.read(1 << 20), b""):
                            out.write(chunk)
                except (OSError, tarfile.TarError):
                    continue


def extract_build(zip_path: str, dest_dir: str) -> dict:
    """Extract a BtbN build zip/tar into `dest_dir`; move ffmpeg/ffprobe to `bin/`.

    Returns {"ffmpeg": <abs path>, "ffprobe": <abs path>}. Raises FetchError if either
    binary is not found in the archive.
    """
    os.makedirs(_abs(dest_dir), exist_ok=True)
    # extract the downloaded build into dest_dir (creates bin/ffmpeg.exe etc.)
    _extract_archive(zip_path, dest_dir)
    # reuse existing binaries if present (idempotent re-provisioning)
    bin_dir = os.path.join(dest_dir, "bin")

    def _present(name):
        cand = os.path.join(bin_dir, name)
        if os.path.isfile(cand):
            return cand
        found = _find_file(dest_dir, name)
        if found:
            try:
                os.makedirs(os.path.dirname(cand), exist_ok=True)
                os.replace(found, cand)
            except OSError:
                pass
        return None

    ffmpeg = _present("ffmpeg.exe" if PLATFORM_WINDOWS else "ffmpeg")
    ffprobe = _present("ffprobe.exe" if PLATFORM_WINDOWS else "ffprobe")
    if not (ffmpeg and ffprobe):
        raise FetchError("downloaded build missing ffmpeg or ffprobe binary")
    return {"ffmpeg": _abs(ffmpeg), "ffprobe": _abs(ffprobe)}


# --------------------------------------------------------------------------- #
# public provisioning entry point
# --------------------------------------------------------------------------- #
def is_provisioned(scope_dir: str = "") -> bool:
    """True if `<scope>/.portable/<os>/ffmpeg/bin` already has BOTH binaries."""
    d = ffmpeg_bin_dir(scope_dir)
    return bool(d) and _exists_all(d, ("ffmpeg.exe", "ffprobe.exe") if PLATFORM_WINDOWS else ("ffmpeg", "ffprobe"))


def ffmpeg_bin_dir(scope_dir: str = "") -> str:
    osf = _os_family()
    scope = _abs(os.path.expanduser(scope_dir)) if scope_dir else _abs(os.getcwd())
    return os.path.join(scope, ".portable", osf, "ffmpeg", "bin")


# --------------------------------------------------------------------------- #
# reuse-first persistence (ConfigStore integration)
#
# After a successful download-only extraction we RECORD the portable bin dir into
# ConfigStore (relative to scope) so later launches can REUSE it without rescanning PATH /
# app roots. ``resolve_recorded_tools`` is the fast-path: given a scope + store, it returns
# tool -> absolute path for any recorded location that still exists on disk.
# --------------------------------------------------------------------------- #
_PROVISIONABLE = ("ffmpeg", "ffprobe")


def record_provision(store: "ConfigStore", scope_dir: str, tools: dict) -> list:
    """Record freshly-provisioned binaries into `store` (relative to scope).

    `tools` is the result of :func:`extract_build` ({"ffmpeg": <abs>, "ffprobe": <abs>}). Paths
    are stored relative to ``scope_dir`` so they survive moving the portable tree; each entry is
    marked ``preferred=True`` with ``origin="portable"``. Returns the list of recorded tool names.
    """
    scope = _abs(os.path.expanduser(scope_dir)) if scope_dir else _abs(os.getcwd())
    rel_root = os.path.relpath(tools["ffmpeg"], scope)
    # record both ffmpeg and ffprobe (a build always ships the pair); keep only what succeeded
    recorded = []
    for tool in _PROVISIONABLE:
        if tools.get(tool):
            store.record(
                tool,
                paths=[os.path.relpath(tools[tool], scope)],  # portable-relative path
                preferred=True,
                origin="portable",
            )
            recorded.append(tool)
    return recorded


def resolve_recorded_tools(scope_dir: str, store=None) -> dict:
    """Return {tool: abs_path} for recorded tool paths that still exist on disk.

    Handles both portable-relative paths (``.portable/<os>/ffmpeg/bin``, resolved against
    ``scope_dir``) and absolute paths already present on disk. Empty when nothing usable is
    recorded -- callers fall back to their normal PATH/app scan. This is the reuse-without-
    rescanning fast-path.
    """
    if store is None:
        from .config_state import ConfigStore, default_config_path
        store = ConfigStore.from_path(default_config_path())
    scope = _abs(os.path.expanduser(scope_dir)) if scope_dir else _abs(os.getcwd())
    out = {}
    for tool in _PROVISIONABLE:
        paths = store.get_paths(tool)
        for p in paths:
            cand = p if os.path.isabs(p) and os.path.isfile(p) else os.path.join(scope, p)
            if os.path.isfile(cand):
                out[tool] = _abs(cand)
                break
    return out


def _exists_all(root: str, names) -> bool:
    return all(os.path.isfile(os.path.join(root, n)) for n in names)


def _origin_of(tool: str, existing_dirs) -> str:
    """Reuse-first origin for one binary: 'reused' if an existing install provides it,
    else '' (fall through to download). Existing dirs are checked before any network fetch.

    A directory counts as a reusable install only when it holds BOTH ffmpeg+ffprobe directly;
    see :func:`_both_present`. So checking the requested tool against such a dir is sufficient --
    if that dir ships the pair, it certainly ships this tool's binary too."""
    # a reusable install dir ships the pair; since it contains both binaries, any requested tool
    # is satisfied by it. Checked before any network fetch (reuse-first).
    for d in list(existing_dirs or []):
        if _both_present(_abs(d)):
            return "reused"
    return ""


def provision(scope_dir: str = "", existing_dirs=None) -> dict:
    """Ensure ffmpeg+ffprobe are available; reuse an existing install when present,
    else download-only-missing into ``.portable/<os>/ffmpeg/bin``.

    Reuse-first policy (PLAN §4 item 3): each binary is classified in ``sources`` as:
      - ``reused`` -- already available, so no new fetch this call. This covers both an existing
        install on a root passed via ``existing_dirs`` (checked before any network call) and a
        portable build left by a prior download.
      - ``downloaded`` -- fetched into the portable build; ``missing`` if that binary is absent
        after extraction.
    The returned ``sources`` map is what callers report as *reused vs downloaded*; later launches
    reuse the portable build / recorded roots without rescanning.

    Returns {"provisioned": bool, "dir": <bin dir> | None, "downloaded": bool,
             "sources": {tool: 'reused'|'downloaded'|'missing'}}.
    """
    osf = _os_family()
    arch = {"win": _win_arch(), "linux": _linux_arch()}[osf]
    dest_dir = os.path.join(_abs(os.path.expanduser(scope_dir)) if scope_dir else _abs(os.getcwd()),
                            ".portable", osf, "ffmpeg")
    existing_dirs = list(existing_dirs) if existing_dirs else []

    # reuse-first: an existing install satisfies both binaries with no download
    sources = {}
    for tool in ("ffmpeg", "ffprobe"):
        origin = _origin_of(tool, existing_dirs)
        if origin:
            sources[tool] = origin

    if is_provisioned(scope_dir):
        # portable build already present from a prior download -> reuse it, no new fetch
        for tool in ("ffmpeg", "ffprobe"):
            sources.setdefault(tool, "reused")
        return {"provisioned": True, "dir": ffmpeg_bin_dir(scope_dir), "downloaded": False,
                "sources": dict(sources)}

    for tool in ("ffmpeg", "ffprobe"):
        sources.setdefault(tool, "downloaded")

    name = build_asset_name(osf, arch)
    url = base_url() + name
    try:
        data = fetch_to_bytes(url)
    except FetchError as exc:
        return {"provisioned": False, "dir": None, "downloaded": False,
                "sources": sources}

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, os.path.basename(url))
        try:
            open(zip_path, "wb").write(data)
            extract_build(zip_path, dest_dir)
        except FetchError as exc:
            return {"provisioned": False, "dir": None, "downloaded": False,
                    "sources": sources}

    # post-extract check: any binary ABSENT on disk is 'missing'; a present one keeps its origin
    for tool in ("ffmpeg", "ffprobe"):
        if not os.path.isfile(os.path.join(ffmpeg_bin_dir(scope_dir), names_for(tool)[0])):
            sources[tool] = "missing"

    return {"provisioned": is_provisioned(scope_dir), "dir": ffmpeg_bin_dir(scope_dir) if is_provisioned(scope_dir) else None,
            "downloaded": True, "sources": dict(sources)}


def _both_present(root: str) -> bool:
    """True if `root` holds BOTH ffmpeg+ffprobe (bare names on POSIX, ``.exe`` on Windows)."""
    ext = ".exe" if PLATFORM_WINDOWS else ""
    ff = os.path.join(_abs(root), "ffmpeg%s" % ext)
    fp = os.path.join(_abs(root), "ffprobe%s" % ext)
    return all(os.path.isfile(p) for p in (ff, fp))


def names_for(tool: str) -> tuple:
    """Binary name(s) for `tool` on this platform (Windows ships ``.exe``)."""
    return ("%s.exe" % tool, "%s.exe" % tool) if PLATFORM_WINDOWS else (tool, tool)


def _format_sources(sources: dict) -> str:
    parts = []
    for tool in ("ffmpeg", "ffprobe"):
        origin = sources.get(tool, "?")
        if origin == "reused":
            parts.append("%s=reused(existing install)" % tool)
        elif origin == "downloaded":
            parts.append("%s=downloaded (into .portable/)" % tool)
        else:
            parts.append("%s=%s" % (tool, origin))
    return ", ".join(parts)


def main(argv=None) -> int:
    scope = os.environ.get("AUTOCV_SCOPE") or os.getcwd()
    res = provision(scope)
    if isinstance(res, dict) and res.get("provisioned"):
        print(f"provisioned ffmpeg+ffprobe into:\n  {res['dir']}")
    elif isinstance(res, dict) and res.get("provisioned"):
        print("ffmpeg+ffprobe already present at:", res["dir"])
    else:
        print("PROVISION FAILED:", res if isinstance(res, str) else res.get("error", res))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
