"""Report the dependencies used by the installed Jamly interpreter."""
from __future__ import annotations

from dataclasses import dataclass, replace
import importlib.metadata
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import sysconfig

from .core import ENGINE_HTDEMUCS, detect_engine, run
from .runtime import bundled_binary, module_command


@dataclass(frozen=True)
class ToolStatus:
    name: str
    path: str | None = None
    ok: bool = False
    note: str = ""

    @property
    def missing(self) -> bool:
        return not self.ok

    def report(self) -> str:
        return "%-12s [%s] %s%s" % (
            self.name, "OK" if self.ok else "MISSING", self.path or "-",
            " (%s)" % self.note if self.note else "",
        )


def detect_system_python() -> ToolStatus:
    return ToolStatus("python", sys.executable, True, sys.version.split()[0])


def find_ytdlp(scope_dir: str = "") -> ToolStatus:
    available = importlib.util.find_spec("yt_dlp") is not None
    version = importlib.metadata.version("yt-dlp") if available else ""
    return ToolStatus("yt-dlp", sys.executable if available else None, available,
                      "%s; python -m yt_dlp" % version if available else "")


def ytdlp_command() -> list[str]:
    if not find_ytdlp().ok:
        raise RuntimeError("yt-dlp is missing; reinstall Jamly in this Python environment")
    return module_command("yt_dlp")


def find_deno() -> ToolStatus:
    bundled = bundled_binary("deno")
    if bundled is not None:
        return ToolStatus("deno", str(bundled), bundled.is_file())
    scripts = sysconfig.get_path("scripts")
    search_path = scripts + os.pathsep + os.environ.get("PATH", "")
    executable = shutil.which("deno", path=search_path)
    return ToolStatus("deno", executable, executable is not None)


def ytdlp_options() -> list[str]:
    deno = find_deno()
    if not deno.ok:
        raise RuntimeError("Deno is missing; reinstall Jamly with its yt-dlp[deno] dependency")
    return ["--ignore-config", "--js-runtimes", "deno:" + deno.path,
            "--no-playlist", "--no-write-subs", "--socket-timeout", "30",
            "--retries", "3"]


def _dir_has_both(root: str) -> bool:
    suffix = ".exe" if os.name == "nt" else ""
    return all(shutil.which(str(Path(root) / (name + suffix))) is not None
               for name in ("ffmpeg", "ffprobe"))


def detect_ffmpeg(ffmpeg_dirs=None, scope_dir="", additional_dirs=()) -> ToolStatus:
    directories = list(ffmpeg_dirs or []) + list(additional_dirs)
    bundled = bundled_binary("ffmpeg")
    if bundled:
        directories.append(str(bundled.parent))
    on_path = shutil.which("ffmpeg")
    if on_path:
        directories.append(str(Path(on_path).absolute().parent))
    for directory in directories:
        if _dir_has_both(directory):
            return ToolStatus("ffmpeg", str(Path(directory).absolute()), True,
                              "ffmpeg and ffprobe")
    return ToolStatus("ffmpeg", note="install ffmpeg and ffprobe on PATH")


def find_ffmpeg(scope_dir: str = "") -> str | None:
    result = detect_ffmpeg(scope_dir=scope_dir)
    return result.path if result.ok else None


@dataclass(frozen=True)
class RunReport:
    python: ToolStatus
    ytdlp: ToolStatus
    ffmpeg: ToolStatus
    engine: ToolStatus
    javascript: ToolStatus

    @property
    def ready(self) -> bool:
        return all(item.ok for item in self.entries)

    @property
    def entries(self) -> tuple[ToolStatus, ...]:
        return self.python, self.ytdlp, self.javascript, self.ffmpeg, self.engine


def summarize(scope_dir: str = "") -> RunReport:
    engine = detect_engine()
    return RunReport(
        detect_system_python(), find_ytdlp(), detect_ffmpeg(),
        ToolStatus("demucs", engine, engine == ENGINE_HTDEMUCS,
                   "model weights download on first separation"),
        find_deno(),
    )


def check_runtime(report: RunReport) -> RunReport:
    commands = {
        "ytdlp": module_command("yt_dlp", "--version"),
        "javascript": [report.javascript.path, "--version"],
        "ffmpeg": [str(Path(report.ffmpeg.path or "") / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")), "-version"],
        "engine": module_command("jamly.bootstrap", "--engine-check"),
    }
    checked = {}
    for field, command in commands.items():
        entry = getattr(report, field)
        if not entry.ok:
            continue
        try:
            result = run(command, timeout=30)
            lines = (result.stdout if result.ok else result.stderr).strip().splitlines()
            checked[field] = replace(entry, ok=result.ok,
                                     note=lines[0] if result.ok and lines else
                                     lines[-1] if lines else "Command did not return a version")
        except (OSError, TimeoutError) as exc:
            checked[field] = replace(entry, ok=False, note=str(exc))
    return replace(report, **checked)


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Check Jamly's installed runtime")
    parser.add_argument("--engine-check", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.engine_check:
        import torch
        import demucs
        from .demucs_worker import resolve_device
        importlib.import_module("demucs.api")
        importlib.import_module("soundfile")
        print("Demucs %s; Torch %s; automatic device: %s" % (
            demucs.__version__, torch.__version__, resolve_device("auto"),
        ))
        return 0
    report = check_runtime(summarize())
    for item in report.entries:
        print(item.report())
    print("Dependencies ready" if report.ready else "Install the missing dependencies above")
    return 0 if report.ready else 3


if __name__ == "__main__":
    raise SystemExit(main())
