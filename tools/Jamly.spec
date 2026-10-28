# The companion and worker share one onedir runtime; no extraction or source checkout at launch.
import os
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
staging = root / "build" / "desktop-staging"
native = root / "build" / "native-audio" / "install" / "bin"
suffix = ".exe" if sys.platform == "win32" else ""

datas = collect_data_files("jamly") + collect_data_files("demucs", include_py_files=True)
datas += collect_data_files("julius", include_py_files=True)
datas += collect_data_files("yt_dlp") + collect_data_files("yt_dlp_ejs")
datas += copy_metadata("jamly-audio-sep", recursive=True)
datas += [
    (str(staging / "notices"), "notices"),
    (str(staging / "bundle.json"), "."),
]
for kind in ("extension", "userscripts"):
    for path in (root / "browser" / kind).iterdir():
        if path.is_file() and not path.name.endswith("_test.js"):
            datas.append((str(path), "share/jamly/browser/" + kind))

hidden = [
    "jamly.bootstrap", "jamly.companion", "jamly.demucs_worker", "jamly.run", "jamly.server",
    "jamly.service", "demucs.htdemucs", "demucs.hf", "yt_dlp.__main__",
]
hidden += collect_submodules("yt_dlp_ejs")
a = Analysis(
    [str(root / "tools" / "desktop_entry.py")],
    pathex=[str(root / "src")],
    binaries=[
        (str(native / ("ffmpeg" + suffix)), "bin"),
        (str(native / ("ffprobe" + suffix)), "bin"),
        (os.environ["JAMLY_DENO_BINARY"], "bin"),
    ],
    datas=datas, hiddenimports=hidden,
    excludes=["tkinter", "IPython", "matplotlib", "notebook", "pytest", "readline", "_gdbm", "dbm.gnu", "zstandard"],
    noarchive=False,
)
a.datas = [entry for entry in a.datas if not entry[0].endswith("/direct_url.json")]
# Use a source-built PCM library instead of redistributing the wheel's opaque codec bundle.
sndfile = native / "libsndfile-1.dll" if sys.platform == "win32" else (
    native.parent / "lib" / ("libsndfile.1.dylib" if sys.platform == "darwin" else "libsndfile.so.1")
)
replaced = False
for index, (destination, source, kind) in enumerate(a.binaries):
    if "_soundfile_data" in destination and "libsndfile" in destination:
        a.binaries[index] = (destination, str(sndfile.resolve()), kind)
        replaced = True
if not replaced:
    raise RuntimeError("The soundfile library hook changed; inspect it before bundling")
pyz = PYZ(a.pure)
launcher = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="Jamly", console=sys.platform == "linux",
    strip=False, upx=False,
)
runtime = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="jamly-runtime", console=True,
    strip=False, upx=False,
)
collection = COLLECT(
    launcher, runtime, a.binaries, a.datas, strip=False, upx=False, name="Jamly",
)
if sys.platform == "darwin":
    app = BUNDLE(
        collection, name="Jamly.app", bundle_identifier="com.jamly.companion",
        info_plist={
            "CFBundleShortVersionString": "0.3.0",
            "CFBundleVersion": "0.3.0",
            "LSMinimumSystemVersion": "13.0",
            "LSUIElement": True,
            "NSHighResolutionCapable": True,
        },
    )
