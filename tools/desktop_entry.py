"""One frozen entry point, with a windowed launcher and a console subprocess runtime."""
from __future__ import annotations

import argparse
import multiprocessing
import os
from pathlib import Path
import runpy
import sys
import traceback


def main() -> int:
    multiprocessing.freeze_support()
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    from jamly.config import runtime_paths
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument("--state-dir", "--scope", dest="state_dir")
    preliminary.add_argument("--queue")
    settings, _rest = preliminary.parse_known_args()
    state, _queue = runtime_paths(settings)
    if sys.stdout is None or sys.stderr is None:
        state.mkdir(parents=True, exist_ok=True)
        log = os.fdopen(os.open(state / "launcher.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600),
                        "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    arguments = sys.argv[1:]
    aliases = {"--doctor": "jamly.bootstrap", "--cli": "jamly.run", "--service": "jamly.service"}
    allowed = {"yt_dlp", "jamly.demucs_worker", "jamly.server", "jamly.bootstrap", "jamly.run", "jamly.service"}
    if arguments and arguments[0] in aliases:
        arguments = ["--module", aliases[arguments[0]], *arguments[1:]]
    if arguments[:1] == ["--module"]:
        if len(arguments) < 2 or arguments[1] not in allowed:
            print("Unknown bundled runtime module", file=sys.stderr)
            return 2
        sys.argv = [arguments[1], *arguments[2:]]
        runpy.run_module(arguments[1], run_name="__main__")
        return 0
    from jamly.companion import main as launch
    try:
        return launch(arguments)
    except Exception as exc:
        traceback.print_exc()
        if "--no-browser" not in arguments and Path(sys.executable).stem == "Jamly":
            from jamly.companion import show_startup_error
            show_startup_error(state, str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
