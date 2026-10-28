"""Paths and subprocess entry points shared by source and frozen installations."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


def bundle_root() -> Path | None:
    return Path(sys._MEIPASS) if getattr(sys, "frozen", False) else None


def module_command(module: str, *args: str, executable: str | None = None) -> list[str]:
    if bundle_root() is not None:
        executable = executable or str(
            Path(sys.executable).with_name("jamly-runtime.exe" if os.name == "nt" else "jamly-runtime")
        )
        return [executable, "--module", module, *args]
    return [executable or sys.executable, "-m", module, *args]


def bundled_binary(name: str) -> Path | None:
    root = bundle_root()
    return root / "bin" / (name + (".exe" if os.name == "nt" else "")) if root else None


def browser_assets(kind: str) -> Path:
    root = bundle_root()
    installed = (root or Path(sys.prefix)) / "share" / "jamly" / "browser" / kind
    checkout = Path(__file__).resolve().parents[2] / "browser" / kind
    source = checkout if root is None and checkout.is_dir() else installed
    if not source.is_dir():
        raise FileNotFoundError("Browser assets are missing; reinstall Jamly")
    return source


def open_directory(path: Path) -> None:
    if not path.is_dir():
        raise ValueError("The export directory no longer exists")
    if os.name == "nt":
        os.startfile(str(path))
        return
    opener = "/usr/bin/open" if sys.platform == "darwin" else shutil.which("xdg-open")
    if not opener:
        raise RuntimeError("No desktop folder opener is installed; use the displayed output path")
    subprocess.run([opener, str(path)], check=True, timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
