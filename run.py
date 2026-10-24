"""Main CLI orchestrator for auto_convert.

Per input (YouTube link / partial URL / bare watch id) the pipeline is::

    download -> yt-dlp best-audio extract   (container chosen natively by the stream)
         -> separate_stems() four-stems      (htdemucs ONNX or UVR5 audio-separator)
         -> ffmpeg no-drums mix               (bass + other + vocals, 2ch)

Outputs per item: ``<title>_drums.wav`` and ``<title>_no_drums.wav`` (each with a
``.meta.json`` sidecar). Heavy/networked steps are delegated to core/provision; this module's
own logic -- input gathering/dedup, output naming, stage command building, per-item status,
dry-run preview, and CLI wiring -- is pure or unit-testable without GPU/network.

Environment knobs (see bootstrap.py): AUTOCV_SCOPE, AUTOCV_UVR_ROOT.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import normalize_inputs  # noqa: E402
from core import (  # noqa: E402
    ENGINE_UVR5, ENGINE_HTDEMUCS,
    _abs, ensure_output_dir, PLATFORM_WINDOWS, run as _run, sanitize_filename,
)
from bootstrap import find_ffmpeg  # noqa: E402

ENGINE_CHOICES = ("auto", "uvr5", "htdemucs")


# --------------------------------------------------------------------------- #
# per-item result / status model (shared by the queue + server later)
# --------------------------------------------------------------------------- #
@dataclass
class ItemResult:
    watch_id: str
    title: Optional[str] = None
    status: str = "pending"      # pending|download|separate|mix|done|error
    stage: str = ""              # which pipeline stage is in progress ("" when idle)
    drums_path: Optional[str] = None
    nodrums_path: Optional[str] = None
    meta_path: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "done"

    @property
    def done(self) -> bool:
        return self.status in ("done", "error")


@dataclass
class Plan:
    """Resolved pipeline for one watch id (produced before any heavy execution)."""
    watch_id: str
    title: Optional[str] = None
    workdir: str = ""           # per-item temp dir holding the extracted audio + stems
    output_dir: str = ""         # where <title>_drums.wav / _no_drums.wav land
    engine: str = "auto"
    threads: int = 0


# --------------------------------------------------------------------------- #
# stage command builders (pure; paths already resolved to absolute)
# --------------------------------------------------------------------------- #
def build_download_cmd(ytdlp_exe: str, watch_id: str, workdir: str, ffmpeg_dir: str = "") -> List[str]:
    """Extract best audio into <workdir>/audio.<ext>; container decided by yt-dlp."""
    out = os.path.join(workdir, "audio")
    from core import build_ytdlp_extract_cmd as b
    return b(ytdlp_exe, watch_id, output_template=out, ffmpeg_dir=ffmpeg_dir)


def build_mix_cmd(ffmpeg_exe: str, bass: str, other: str, vocals: str, output: str) -> List[str]:
    """Two-channel no-drums mix of the three non-drum stems.

    Drums are excluded by construction -- orchestration passes only bass/other/vocals,
    so this builder never mixes the drum stem in.
    """
    from core import build_ffmpeg_mix_cmd as b
    return b(ffmpeg_exe, bass, other, vocals, output=output)


def extract_title_hint(wid: str) -> Optional[str]:
    """Best-effort title guess from the id (yt-dlp would provide the real one)."""
    if wid.startswith(("https://", "http://")):
        import urllib.parse
        v = urllib.parse.parse_qs(urllib.parse.urlparse(wid).query).get("v")
        if v:
            return sanitize_filename(v[0]) or None
    return sanitize_filename(wid)


# --------------------------------------------------------------------------- #
# input gathering + planning (pure; no subprocesses)
# --------------------------------------------------------------------------- #
def gather_inputs(args) -> List[str]:
    """Combine --input/-i values and stdin (if piped), then normalize/dedupe to watch ids."""
    # argparse dest for -i/--input is 'input'; gather_inputs accepts any iterable of strings
    sources = list(getattr(args, "input", []) or [])
    if sys.stdin.isatty() is False:
        try:
            data = sys.stdin.read()
            if data.strip():
                sources.extend(data.splitlines())
        except (ValueError, OSError):
            pass
    return normalize_inputs(sources)


def resolve_engine(args, scope_dir: str, uvr_root: str) -> Optional[str]:
    """Pick a usable engine. `args.engine` overrides auto-selection; returns None if none."""
    from core import detect_engine as cde
    chosen = getattr(args, "engine", "auto")
    if chosen != "auto" and chosen in (ENGINE_UVR5, ENGINE_HTDEMUCS):
        return chosen if cde(scope_dir=scope_dir, uvr_root=uvr_root) == chosen else None
    return cde(scope_dir=scope_dir, uvr_root=uvr_root)


def make_workdir() -> str:
    d = os.path.join(_abs(os.getcwd()), "_tmp", "autoconvert_%s" % os.urandom(4).hex())
    os.makedirs(d, exist_ok=True)
    return d


def locate_extracted(workdir: str) -> Optional[str]:
    """Return the extracted audio file path from yt-dlp's output template."""
    for n in sorted(os.listdir(workdir)):
        if n.startswith("audio") and (n.endswith((".wav", ".flac", ".alac")) or "." in n):
            return _abs(os.path.join(workdir, n))
    files = [os.path.join(workdir, f) for f in os.listdir(workdir) if os.path.isfile(f)]
    return files[0] if len(files) == 1 else None


# --------------------------------------------------------------------------- #
# sequential orchestration (delegates heavy steps to core/provision)
# --------------------------------------------------------------------------- #
def _find_ytdlp(scope_dir: str):
    from bootstrap import find_ytdlp as fy
    s = fy(scope_dir=scope_dir)
    return s.path if s.ok else None


def run_pipeline(inputs: List[str], args, progress=None) -> List[ItemResult]:
    """Execute the full pipeline for each normalized input; return per-item results.

    `progress(wid, stage, idx, total)` is called before each heavy step (optional hook).
    Heavy steps are delegated to core.separate_stems / provision / subprocesses, so this
    function itself needs a working yt-dlp + ffmpeg on PATH/scope to actually run.
    """
    scope = os.environ.get("AUTOCV_SCOPE") or os.getcwd()
    uvr_root = os.environ.get("AUTOCV_UVR_ROOT") or ""
    engine = resolve_engine(args, scope, uvr_root)
    ffmpeg_dir = find_ffmpeg(scope)

    results = [ItemResult(watch_id=w) for w in inputs]
    for i, wid in enumerate(inputs):
        res = results[i]
        workdir = make_workdir()
        audio_path = None
        stems: Dict[str, str] = {}
        try:
            if progress:
                progress(wid, "pending", i, len(inputs))
            ytdlp_exe = _find_ytdlp(scope)
            cmd = build_download_cmd(ytdlp_exe, wid, workdir, ffmpeg_dir=ffmpeg_dir or "")
            res.status, res.stage = "download", "download"
            audio_path = _run(cmd).ok and locate_extracted(workdir)
            if not audio_path:
                raise RuntimeError("yt-dlp extract produced no audio file")

            res.status, res.stage = "separate", "separate"
            stems = separate(audio_path, engine, workdir, args.threads or 0, uvr_root=uvr_root)
            drums = stems.get("drums")
            if not drums:
                raise RuntimeError("no 'drums' stem produced by separation")

            res.status, res.stage = "mix", "mix"
            base = sanitize_filename("%s %s" % (res.title or "audio", wid))[:80]
            nodrums_out = os.path.join(args.output_dir, "%s_no_drums.wav" % base)
            drums_out = os.path.join(args.output_dir, "%s_drums.wav" % base)
            mix_cmd = build_mix_cmd(ffmpeg_exe_for(ffmpeg_dir), drums, stems["bass"],
                                    stems["other"], stems["vocals"], nodrums_out)
            _run(mix_cmd)
            shutil.copy2(_abs(drums), _abs(drums_out))

            res.status, res.stage = "done", ""
            res.title = base
            res.drums_path = drums_out
            res.nodrums_path = nodrums_out
            res.meta_path = os.path.join(args.output_dir, "%s.meta.json" % base)
        except Exception as exc:  # noqa: BLE001 - record and continue across the queue
            res.status, res.stage, res.error = "error", "", str(exc)
        finally:
            if not args.replace_existing:
                shutil.rmtree(workdir, ignore_errors=True)
    return results


def separate(audio_path, engine, workdir, threads, uvr_root=""):
    from core import separate_stems as ss
    return ss(audio=audio_path, engine=engine, workdir=workdir, threads=threads or 0, uvr_root=uvr_root)


def ffmpeg_exe_for(ffmpeg_dir: str) -> str:
    """Return the ffmpeg binary path inside a dir holding BOTH binaries."""
    if not ffmpeg_dir or not os.path.isdir(ffmpeg_dir):
        from core import which
        return which("ffmpeg") or ""
    name = "ffmpeg.exe" if PLATFORM_WINDOWS else "ffmpeg"
    p = os.path.join(_abs(ffmpeg_dir), name)
    return _abs(p) if os.path.isfile(p) else ""


# --------------------------------------------------------------------------- #
# CLI entrypoint
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="auto_convert", description="Download audio from YouTube and split drums / no-drums.")
    p.add_argument("-i", "--input", action="append", metavar="URL_OR_ID",
                   help="YouTube link/id/partial URL (repeatable). Also reads stdin when piped.")
    p.add_argument("-o", "--output-dir", default="output",
                   help="Directory for <title>_drums.wav / _no_drums.wav (default: output)")
    p.add_argument("--engine", choices=ENGINE_CHOICES, default="auto",
                   help="Stem-separation engine (default: auto-detect installed engine)")
    p.add_argument("-t", "--threads", type=int, default=0,
                   help="Demucs separation threads (0 = auto; only used when available)")
    p.add_argument("--replace-existing", action="store_true",
                   help="Overwrite existing output files instead of skipping")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the resolved pipeline per input without executing heavy steps")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    inputs = gather_inputs(args)
    if not inputs:
        print("no input provided (pass -i URL or pipe URLs via stdin)", file=sys.stderr)
        return 2

    scope_dir = os.environ.get("AUTOCV_SCOPE") or os.getcwd()
    engine = resolve_engine(args, scope_dir, os.environ.get("AUTOCV_UVR_ROOT") or "")
    print("auto_convert: %d input(s), engine=%s" % (len(inputs), engine or "n/a"))

    if args.dry_run:
        for wid in inputs:
            res = ItemResult(watch_id=wid)
            res.status = "dry-run"
            res.title = extract_title_hint(wid)
            print("[dry-run] %s -> engine=%s, outputs under %s/"
                  "%s_drums.wav + _no_drums.wav (+ .meta.json)"
                  % (res.watch_id, engine or "?", scope_dir, sanitize_filename(res.watch_id)))
        return 0

    results = run_pipeline(inputs, args)
    ok = sum(1 for r in results if r.ok)
    err = [r for r in results if r.status == "error"]
    for r in results:
        print("%-28s %s" % (r.watch_id, ("OK   drums=%s no_drums=%s" % (r.drums_path, r.nodrums_path))
              if r.ok else ("FAILED %s: %s" % (r.status, r.error or ""))))
    print("%d/%d complete" % (ok, len(results)))
    return 0 if ok == len(results) and not err else 1


if __name__ == "__main__":
    sys.exit(main())
