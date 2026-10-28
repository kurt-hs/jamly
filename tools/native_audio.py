"""Build redistributable ffmpeg/ffprobe from checksum-pinned source, on the target OS."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SOURCES_FILE = Path(__file__).with_name("native-sources.json")
SOURCES = json.loads(SOURCES_FILE.read_text())


def sndfile_library(prefix: Path) -> Path:
    if os.name == "nt":
        return prefix / "bin" / "libsndfile-1.dll"
    return prefix / "lib" / ("libsndfile.1.dylib" if sys.platform == "darwin" else "libsndfile.so.1")


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_source(name: str, cache: Path) -> Path:
    info = SOURCES[name]
    archive = cache / Path(urlsplit(info["url"]).path).name
    cache.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        if checksum(archive) != info["sha256"]:
            raise RuntimeError("Cached source checksum mismatch: " + str(archive))
        return archive
    temporary = archive.with_suffix(archive.suffix + ".partial")
    try:
        with urlopen(info["url"], timeout=120) as response, temporary.open("wb") as file:
            if urlsplit(response.url).scheme != "https":
                raise RuntimeError("Refusing an insecure source download")
            shutil.copyfileobj(response, file)
        if checksum(temporary) != info["sha256"]:
            raise RuntimeError("Source checksum mismatch: " + info["url"])
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def build(work: Path, jobs: int = 4) -> Path:
    work = work.resolve()
    marker = work / ".jamly-native-build"
    if work.exists() and any(work.iterdir()) and not marker.exists():
        raise ValueError("Choose an empty native-build directory: " + str(work))
    work.mkdir(parents=True, exist_ok=True)
    marker.touch()
    platform_key = sys.platform + "-" + platform.machine().lower()
    targets = {
        "darwin-arm64": ("darwin64-arm64-cc", "aarch64-apple-darwin"),
        "darwin-x86_64": ("darwin64-x86_64-cc", "x86_64-apple-darwin"),
        "linux-x86_64": ("linux-x86_64", "x86_64-pc-linux-gnu"),
        "win32-amd64": ("mingw64", "x86_64-w64-mingw32"),
    }
    if platform_key not in targets:
        raise ValueError("Unsupported desktop build target: " + platform_key)
    identity = {
        "platform": platform_key, "sources": checksum(SOURCES_FILE),
        "builder": checksum(Path(__file__)),
    }
    prefix = work / "install"
    suffix = ".exe" if os.name == "nt" else ""
    executables = [prefix / "bin" / (name + suffix) for name in ("ffmpeg", "ffprobe")]
    binaries = executables + [sndfile_library(prefix)]
    stamp = work / "built.json"
    if stamp.exists():
        saved = json.loads(stamp.read_text())
        if (saved.get("inputs") == identity and all(path.is_file() for path in binaries)
                and saved.get("binaries") == {path.name: checksum(path) for path in binaries}):
            return prefix

    shell = os.environ.get("JAMLY_BUILD_SHELL") or shutil.which("bash")
    if not shell:
        raise RuntimeError("Building desktop audio tools requires Bash and a C build toolchain")
    environment = dict(os.environ)
    if sys.platform == "darwin":
        environment["MACOSX_DEPLOYMENT_TARGET"] = "13.0"
    log_path = work / "build.log"

    def posix(path: Path) -> str:
        if os.name != "nt":
            return str(path)
        return subprocess.check_output(
            [shell, "--noprofile", "--norc", "-c", 'cygpath -u "$1"', "jamly", str(path)],
            text=True,
        ).strip()

    def execute(command: list[str], cwd: Path):
        with log_path.open("a", encoding="utf-8") as log:
            log.write("\n" + shlex.join(command) + "\n")
            log.flush()
            result = subprocess.run(
                [shell, "--noprofile", "--norc", "-c", shlex.join(command)],
                cwd=cwd, env=environment, stdout=log, stderr=subprocess.STDOUT,
            )
        if result.returncode:
            tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-60:])
            raise RuntimeError("Native build failed; see %s\n%s" % (log_path, tail))

    for generated in (prefix, work / "source"):
        if generated.exists():
            shutil.rmtree(generated)
    source = work / "source"
    source.mkdir()
    for name in ("openssl", "lame", "ffmpeg", "libsndfile"):
        archive = download_source(name, work / "downloads")
        with tarfile.open(archive) as file:
            file.extractall(source, filter="data")
    root = posix(prefix)
    environment["PKG_CONFIG_PATH"] = root + "/lib/pkgconfig"
    environment["CFLAGS"] = "-O2 -fPIC"
    openssl_target, host = targets[platform_key]
    parallel = "-j%d" % max(1, jobs)

    print("Building pinned audio dependencies for " + platform_key, flush=True)
    directory = source / SOURCES["openssl"]["directory"]
    execute(["perl", "Configure", openssl_target, "no-shared", "no-tests", "no-module",
             "--prefix=" + root, "--libdir=lib"], directory)
    execute(["make", parallel], directory)
    execute(["make", "install_sw"], directory)

    directory = source / SOURCES["lame"]["directory"]
    execute(["./configure", "--prefix=" + root, "--build=" + host, "--host=" + host,
             "--disable-shared", "--enable-static", "--disable-frontend"], directory)
    execute(["make", parallel], directory)
    execute(["make", "install"], directory)

    directory = source / SOURCES["ffmpeg"]["directory"]
    flags = [
        "./configure", "--prefix=" + root, "--disable-autodetect", "--disable-shared",
        "--enable-static", "--disable-doc", "--disable-debug", "--disable-ffplay",
        "--disable-x86asm", "--enable-libmp3lame", "--enable-openssl", "--enable-version3",
        "--pkg-config-flags=--static", "--extra-cflags=-I" + root + "/include",
        "--extra-ldflags=-L" + root + "/lib",
    ]
    if os.name == "nt":
        flags.extend(("--target-os=mingw32", "--arch=x86_64", "--disable-pthreads", "--enable-w32threads"))
    execute(flags, directory)
    execute(["make", parallel], directory)
    execute(["make", "install"], directory)
    directory = source / SOURCES["libsndfile"]["directory"]
    if os.name == "nt":
        environment["CFLAGS"] += " -std=gnu11"
    execute(["./configure", "--prefix=" + root, "--build=" + host, "--host=" + host,
             "--disable-static", "--enable-shared", "--disable-external-libs",
             "--disable-mpeg", "--disable-alsa", "--disable-sqlite", "--disable-full-suite"], directory)
    execute(["make", parallel], directory)
    execute(["make", "install"], directory)
    for binary in executables:
        subprocess.run([str(binary), "-version"], check=True, stdout=subprocess.DEVNULL)
    stamp.write_text(json.dumps({
        "inputs": identity, "binaries": {path.name: checksum(path) for path in binaries},
    }, indent=2))
    return prefix


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build" / "native-audio")
    parser.add_argument("--jobs", type=int, default=4)
    options = parser.parse_args()
    print(build(options.work_dir, options.jobs))
