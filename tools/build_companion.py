"""Build a self-contained, unsigned companion for the current desktop OS."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
from urllib.parse import quote
from urllib.request import urlopen
import zipfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from native_audio import ROOT, SOURCES, SOURCES_FILE, build, checksum, download_source


def python_notices(destination: Path) -> None:
    build_marker = Path(sys.base_prefix) / "BUILD"
    if not build_marker.is_file():
        raise RuntimeError("Desktop builds require uv-managed Python so its native dependency notices "
                           "can be included. Run uv sync --managed-python --python 3.12 --group bundle.")
    release = build_marker.read_text().strip()
    if len(release) != 8 or not release.isdigit():
        raise RuntimeError("Unrecognized Python distribution build marker")
    triples = {
        ("darwin", "arm64"): "aarch64-apple-darwin",
        ("darwin", "x86_64"): "x86_64-apple-darwin",
        ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
        ("win32", "amd64"): "x86_64-pc-windows-msvc",
    }
    triple = triples[(sys.platform, platform.machine().lower())]
    prefix = "cpython-%s+%s-%s-" % (platform.python_version(), release, triple)
    base = "https://github.com/astral-sh/python-build-standalone/releases/download/" + release
    with urlopen(base + "/SHA256SUMS", timeout=60) as response:
        sums = [line.split(maxsplit=1) for line in response.read().decode().splitlines() if line.strip()]
    matches = [(digest, name.lstrip("*")) for digest, name in sums
               if name.lstrip("*").startswith(prefix) and name.endswith("-full.tar.zst") and "pgo" in name]
    if len(matches) != 1:
        raise RuntimeError("Cannot identify the matching redistributable Python license archive")
    digest, name = matches[0]
    if len(digest) != 64:
        raise RuntimeError("The Python release does not provide a SHA-256 digest")
    cache = ROOT / "build" / "python-notices"
    cache.mkdir(parents=True, exist_ok=True)
    url = base + "/" + quote(name, safe="")
    archive = cache / name
    if not archive.exists():
        partial = archive.with_suffix(".partial")
        try:
            with urlopen(url, timeout=120) as response, partial.open("wb") as file:
                shutil.copyfileobj(response, file)
            if checksum(partial) != digest:
                raise RuntimeError("Python notice archive checksum mismatch")
            partial.replace(archive)
        finally:
            partial.unlink(missing_ok=True)
    if checksum(archive) != digest:
        raise RuntimeError("Cached Python notice archive checksum mismatch")
    import zstandard
    copied = 0
    destination.mkdir(parents=True, exist_ok=True)
    with archive.open("rb") as file, zstandard.ZstdDecompressor().stream_reader(file) as reader:
        with tarfile.open(fileobj=reader, mode="r|") as source:
            for entry in source:
                path = Path(entry.name)
                if (entry.isfile() and "licenses" in path.parts and not path.is_absolute()
                        and ".." not in path.parts):
                    target = destination / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(entry) as notice:
                        target.write_bytes(notice.read())
                    copied += 1
    if not copied or not (destination / "python" / "licenses" / "LICENSE.cpython.txt").is_file():
        raise RuntimeError("The Python distribution did not contain interpreter and dependency notices")
    (destination / "provenance.json").write_text(json.dumps({
        "url": url, "sha256": digest, "build": release,
    }, indent=2))


def runtime_distributions() -> list:
    pending = [Requirement("jamly-audio-sep"), Requirement("pyinstaller")]
    seen = set()
    result = {}
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        identity = (name, tuple(sorted(requirement.extras)))
        if identity in seen:
            continue
        seen.add(identity)
        distribution = importlib.metadata.distribution(name)
        result[name] = distribution
        for raw in distribution.requires or []:
            dependency = Requirement(raw)
            if dependency.marker is None or any(
                dependency.marker.evaluate({"extra": extra})
                for extra in requirement.extras | {""}
            ):
                pending.append(dependency)
    return list(result.values())


def notices(staging: Path) -> list[dict]:
    destination = staging / "notices"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    inventory = []
    for distribution in runtime_distributions():
        name = distribution.metadata["Name"]
        record = {"name": name, "version": distribution.version,
                  "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License"),
                  "project_urls": distribution.metadata.get_all("Project-URL", [])}
        inventory.append(record)
        target = destination / name
        target.mkdir(exist_ok=True)
        (target / "METADATA.txt").write_text(distribution.read_text("METADATA") or "", encoding="utf-8")
        for file in distribution.files or []:
            if (any(word in file.name.lower() for word in ("license", "copying", "notice", "copyright"))
                    and ".." not in file.parts):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    copied = target / str(file)
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, copied)
    python_notices(destination / "python-runtime")
    shutil.copyfile(ROOT / "LICENSE.md", destination / "JAMLY-LICENSE.md")
    for name in SOURCES:
        archive = download_source(name, ROOT / "build" / "native-audio" / "downloads")
        target = destination / name
        target.mkdir(exist_ok=True)
        with tarfile.open(archive) as source:
            for entry in source:
                relative = Path(entry.name)
                if (entry.isfile() and len(relative.parts) >= 2 and ".." not in relative.parts
                        and not relative.is_absolute()
                        and any(word in relative.name.lower() for word in ("license", "copying", "notice", "copyright"))):
                    copied = target.joinpath(*relative.parts[1:])
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(entry) as file:
                        copied.write_bytes(file.read())
        if not any(target.iterdir()):
            raise RuntimeError("Missing native redistribution notices for " + name)
    shutil.copyfile(ROOT / "build" / "native-audio" / "built.json", destination / "native-build.json")
    return inventory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=4, help="Native compilation parallelism")
    args = parser.parse_args()
    import soundfile
    from deno import find_deno_bin
    from jamly import __version__

    if soundfile.__libsndfile_version__ != SOURCES["libsndfile"]["version"]:
        raise RuntimeError("Update the libsndfile corresponding-source pin before distributing this wheel")
    staging = ROOT / "build" / "desktop-staging"
    staging.mkdir(parents=True, exist_ok=True)
    if not (Path(sys.base_prefix) / "BUILD").is_file():
        raise RuntimeError("Use uv-managed Python for desktop builds: "
                           "uv sync --locked --group bundle --managed-python --python 3.12")
    build(ROOT / "build" / "native-audio", args.jobs)
    inventory = notices(staging)
    identity = sys.platform + "-" + platform.machine().lower()
    deno = Path(find_deno_bin())
    bundle_info = {
        "jamly": __version__, "platform": identity, "python": sys.version,
        "uv_lock_sha256": checksum(ROOT / "uv.lock"), "packages": inventory,
        "native_sources": SOURCES, "deno_sha256": checksum(deno),
        "models": ["htdemucs", "htdemucs_ft"], "weights_bundled": False,
        "git_revision": os.environ.get("GITHUB_SHA"),
    }
    (staging / "bundle.json").write_text(json.dumps(bundle_info, indent=2), encoding="utf-8")
    output = ROOT / "dist" / "desktop"
    output.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, JAMLY_DENO_BINARY=str(deno),
                       PYINSTALLER_CONFIG_DIR=str(ROOT / "build" / "pyinstaller-cache"))
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--log-level", "WARN",
        "--distpath", str(output), "--workpath", str(ROOT / "build" / "pyinstaller"),
        str(ROOT / "tools" / "Jamly.spec"),
    ], check=True, cwd=ROOT, env=environment)
    release = "Jamly-%s-%s" % (__version__, identity)
    archives = ROOT / "dist" / "companions"
    archives.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        artifact = archives / (release + ".zip")
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(output / "Jamly.app"), str(artifact)], check=True)
    else:
        kind = "zip" if os.name == "nt" else "gztar"
        artifact = Path(shutil.make_archive(str(archives / release), kind, output, "Jamly"))
    source_archive = archives / (release + "-native-sources.zip")
    with zipfile.ZipFile(source_archive, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in SOURCES:
            source = download_source(name, ROOT / "build" / "native-audio" / "downloads")
            archive.write(source, source.name)
        archive.write(SOURCES_FILE, "native-sources.json")
        archive.write(Path(__file__).with_name("native_audio.py"), "native_audio.py")
        archive.writestr("README.txt",
                        "Exact sources for the bundled FFmpeg, LAME, OpenSSL and libsndfile.\n"
                        "native_audio.py builds all four native dependencies from these sources. "
                        "The source-built libsndfile replaces the wheel's library and is used only "
                        "for PCM audio; encoded media uses sphn/FFmpeg. Jamly uses libsndfile dynamically, "
                        "so the bundled library "
                        "can be replaced with a compatible modified library.\n"
                        "To rebuild the audio executables with Python 3.12 and a C toolchain:\n"
                        "python native_audio.py --work-dir ./build/native-audio\n"
                        "The companion is an aggregation: bundled components retain their own licenses.\n")
    checksum_file = archives / (release + ".sha256")
    checksum_file.write_text("".join(
        "%s  %s\n" % (checksum(path), path.name) for path in (artifact, source_archive)
    ))
    print(artifact)
    print(source_archive)
    print(checksum_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
