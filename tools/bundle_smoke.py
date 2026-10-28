"""Exercise a relocated desktop archive with no external executables on PATH."""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener, url2pathname
import zipfile

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]


def api(url, token, path, body=None, *, form=False):
    headers = {"Authorization": "Bearer " + token}
    if form:
        data = urlencode(body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Origin"] = "null" if path == "/app/session" else url
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    else:
        data = None
    with build_opener(ProxyHandler({})).open(Request(url + path, data=data, headers=headers), timeout=10) as response:
        data = response.read()
        return data if form else json.loads(data)


def unpack(archive: Path, destination: Path) -> Path:
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(destination)], check=True)
        return destination / "Jamly.app" / "Contents" / "MacOS" / "jamly-runtime"
    if os.name == "nt":
        with zipfile.ZipFile(archive) as file:
            file.extractall(destination)
        return destination / "Jamly" / "jamly-runtime.exe"
    with tarfile.open(archive) as file:
        file.extractall(destination, filter="data")
    return destination / "Jamly" / "jamly-runtime"


def check_macos_links(root: Path) -> None:
    if sys.platform != "darwin":
        return
    magic = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as file:
                if file.read(4) not in magic:
                    continue
            linked = subprocess.check_output(["/usr/bin/otool", "-L", str(path)], text=True)
            for line in linked.splitlines()[1:]:
                library = line.strip().split(" (", 1)[0]
                if library.startswith("/") and not library.startswith(("/usr/lib/", "/System/Library/")):
                    raise AssertionError("Non-relocatable dependency in %s: %s" % (path, library))


def stop_backend(url: str, token: str, state: Path) -> None:
    record = json.loads(next(state.glob("server-*.json")).read_text())
    handle = None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, record["pid"])
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
    try:
        api(url, token, "/app/stop", {})
        if handle:
            if kernel.WaitForSingleObject(handle, 20000) != 0:
                raise RuntimeError("The bundled backend did not exit after shutdown")
        else:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    os.kill(record["pid"], 0)
                except ProcessLookupError:
                    break
                time.sleep(.1)
            else:
                raise RuntimeError("The bundled backend did not exit after shutdown")
    finally:
        if handle:
            kernel.CloseHandle(handle)
    if list(state.glob("server-*.json")):
        raise RuntimeError("Companion did not release its instance after shutdown")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--real-models", action="store_true", help="Run both actual model presets")
    args = parser.parse_args()
    from jamly import __version__
    suffix = ".tar.gz" if sys.platform == "linux" else ".zip"
    archive = args.archive or ROOT / "dist" / "companions" / (
        "Jamly-%s-%s-%s%s" % (__version__, sys.platform, platform.machine().lower(), suffix)
    )
    with tempfile.TemporaryDirectory(prefix="jamly relocated package ") as directory:
        root = Path(directory)
        executable = unpack(archive, root / "application with spaces")
        check_macos_links(root / "application with spaces")
        environment = dict(os.environ, PATH="", PYTHONPATH="", PYTHONHOME="",
                           HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_TELEMETRY="1")
        environment.pop("VIRTUAL_ENV", None)
        for key in list(environment):
            if key.startswith("JAMLY_"):
                del environment[key]
        state = root / "state"
        output = root / "output"
        command = [str(executable), "--no-browser", "--state-dir", str(state), "--port", "0",
                   "--output-dir", str(output)]
        url = token = None
        media_server = None
        media_thread = None
        try:
            doctor = subprocess.run([str(executable), "--doctor"], cwd=root, env=environment,
                                    check=True, capture_output=True, text=True, timeout=120)
            print(doctor.stdout, end="")
            launcher = executable.with_name("Jamly.exe" if os.name == "nt" else "Jamly")
            gui_command = (["/usr/bin/open", "-n", "-W", str(executable.parents[2]), "--args"]
                           if sys.platform == "darwin" else [str(launcher)])
            subprocess.run(gui_command + command[1:], cwd=root, env=environment, check=True,
                           capture_output=True, text=True, timeout=120)
            instance = json.loads(next(state.glob("server-*.json")).read_text())
            url = "http://127.0.0.1:%d" % instance["port"]
            token = (state / "server-token").read_text().strip()
            opened = api(url, token, "/app/launch", {})
            second = subprocess.run(command, cwd=root, env=environment, check=True,
                                    capture_output=True, text=True, timeout=60)
            assert json.loads(second.stdout)["url"] == url, "Reopening created another backend"
            launch_file = Path(url2pathname(urlsplit(opened["launch_file"]).path))
            ticket = re.search(r'name="ticket" value="([^"]+)"', launch_file.read_text()).group(1)
            session = api(url, token, "/app/session", {"ticket": ticket}, form=True)
            assert b'sessionStorage.setItem("jamly-token"' in session
            assert not launch_file.exists(), "Consumed launch file was retained"
            setup = api(url, token, "/app/extension", {})
            assert (Path(setup["directory"]) / "manifest.json").is_file()
            changed = {"model": "htdemucs_ft", "audio_format": "wav", "output_dir": str(output)}
            assert api(url, token, "/settings", changed) == changed
            assert api(url, token, "/status")["defaults"] == changed

            if args.real_models:
                samples = .2 * np.sin(np.arange(11025) * 2 * np.pi * 440 / 44100)
                sf.write(root / "generated.wav", np.stack((samples, samples), axis=1), 44100, subtype="FLOAT")
                binary_root = (executable.parent.parent / "Frameworks" if sys.platform == "darwin"
                               else executable.parent / "_internal")
                ffmpeg = binary_root / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
                fixture_environment = dict(environment)
                if os.name == "nt":
                    # Frozen parents supply this DLL directory; this unfrozen fixture builder must too.
                    fixture_environment["PATH"] = str(binary_root)
                subprocess.run([
                    str(ffmpeg), "-nostdin", "-y", "-i", str(root / "generated.wav"),
                    "-c:a", "libmp3lame", "-b:a", "320k", str(root / "generated.mp3"),
                ], check=True, env=fixture_environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
                media_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
                media_thread = threading.Thread(target=media_server.serve_forever, daemon=True)
                media_thread.start()
                source = "http://127.0.0.1:%d/generated.mp3" % media_server.server_address[1]
                generations = []
                for model, fmt in (("htdemucs", "mp3"), ("htdemucs_ft", "wav")):
                    api(url, token, "/add", {"inputs": [source], "model": model, "audio_format": fmt})
                    deadline = time.monotonic() + 600
                    while time.monotonic() < deadline:
                        status = api(url, token, "/status")
                        job = next(item for item in status["jobs"] if item["source_url"] == source)
                        if job["status"] in ("done", "skipped"):
                            break
                        if job["status"] == "error":
                            raise AssertionError(job["error"])
                        time.sleep(.25)
                    else:
                        raise TimeoutError("Bundled model did not finish")
                    manifests = list(output.glob("*/%s/%s/metadata.json" % (model, fmt)))
                    assert len(manifests) == 1
                    metadata = json.loads(manifests[0].read_text())
                    assert metadata["model"] == model
                    generations.append(metadata["generation"])
                    infos = [sf.info(manifests[0].parent / record["file"])
                             for record in metadata["outputs"].values()]
                    assert len(infos) == 2
                    assert (infos[0].frames, infos[0].channels, infos[0].samplerate) == (
                        infos[1].frames, infos[1].channels, infos[1].samplerate,
                    )
                    output_url = job["outputs"][0]["url"].replace("/outputs/", "/app/download/", 1)
                    downloaded = api(url, token, output_url, {"token": token}, form=True)
                    assert len(downloaded) > 0
                    print("Bundled %s -> %s pair succeeded" % (model, fmt), flush=True)
                assert len(set(generations)) == 2
            print("Relocated companion, pairing, settings, and packaged assets succeeded", flush=True)
        except subprocess.CalledProcessError as exc:
            print(exc.stdout or "", file=sys.stderr)
            print(exc.stderr or "", file=sys.stderr)
            raise
        finally:
            if media_server:
                media_server.shutdown()
                media_server.server_close()
                media_thread.join()
            if url and token:
                stop_backend(url, token, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
