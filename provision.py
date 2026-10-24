"""Self-provisioning for the portable pipeline scope (.portable/).

Reuses an already-installed tool when possible (reuse-first policy, PLAN.md §10); only
downloads missing pieces into a private `.portable/<os>/` layout so nothing is installed
globally. Currently provisions the ffmpeg + ffprobe static build; detection hooks are
already wired to find it under `<scope>/.portable/<os>/ffmpeg`.

Pure standard-library, network calls isolated in `fetch_*` so they can be unit-tested
with a fake opener without touching the real internet.
"""
from __future__ import annotations

import io
import os
import platform
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import PLATFORM_WINDOWS, _abs  # noqa: E402


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


def _exists_all(root: str, names) -> bool:
    return all(os.path.isfile(os.path.join(root, n)) for n in names)


def provision(scope_dir: str = "") -> dict:
    """Ensure ffmpeg+ffprobe exist under the portable scope; download if missing.

    Returns {"provisioned": True/False, "dir": <bin dir> | None, "downloaded": bool}.
    """
    osf = _os_family()
    arch = {"win": _win_arch(), "linux": _linux_arch()}[osf]
    dest_dir = os.path.join(_abs(os.path.expanduser(scope_dir)) if scope_dir else _abs(os.getcwd()),
                            ".portable", osf, "ffmpeg")

    if is_provisioned(scope_dir):
        return {"provisioned": True, "dir": ffmpeg_bin_dir(scope_dir), "downloaded": False}

    name = build_asset_name(osf, arch)
    url = base_url() + name
    try:
        data = fetch_to_bytes(url)
    except FetchError as exc:
        return {"provisioned": False, "dir": None, "error": str(exc)}

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, os.path.basename(url))
        try:
            open(zip_path, "wb").write(data)
            result = extract_build(zip_path, dest_dir)
        except FetchError as exc:
            return {"provisioned": False, "dir": None, "error": str(exc)}

    if is_provisioned(scope_dir):
        return {"provisioned": True, "dir": ffmpeg_bin_dir(scope_dir), "downloaded": True}
    return {"provisioned": False, "dir": None, "error": "post-extract check failed"}


def main(argv=None) -> int:
    scope = os.environ.get("AUTOCV_SCOPE") or os.getcwd()
    res = provision(scope)
    if isinstance(res, dict) and res.get("downloaded"):
        print(f"provisioned ffmpeg+ffprobe into:\n  {res['dir']}")
    elif isinstance(res, dict) and res.get("provisioned"):
        print("ffmpeg+ffprobe already present at:", res["dir"])
    else:
        print("PROVISION FAILED:", res if isinstance(res, str) else res.get("error", res))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
