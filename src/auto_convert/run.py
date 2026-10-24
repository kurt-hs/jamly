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

from .normalize import normalize_inputs, extract_watch_id
from .core import (
    ENGINE_UVR5, ENGINE_HTDEMUCS,
    _abs, ensure_output_dir, PLATFORM_WINDOWS, run as _run, sanitize_filename,
    tool_source,
)
from .bootstrap import find_ffmpeg
from .logging import Logger
from auto_convert._input_queue import WatchQueue, default_queue_path

ENGINE_CHOICES = ("auto", "uvr5", "htdemucs")


# module-level logger; quiet by default so importing this module never prints (tests/callers opt in).
# Tests replace it via ``monkeypatch.setattr("auto_convert.run._log", ...)`` to capture output.
_log = Logger()


def get_logger() -> Logger:
    """Return the process logger. Callers may swap it for a capturing instance (see :func:`main`)."""
    return _log


def ensure_utf8_output(streams=None):
    """Force UTF-8 on stdout/stderr when they can't already encode Unicode.

    Windows defaults to the console code page (often cp1252), which cannot render block-glyph
    output such as the download bar. Reconfiguring to UTF-8 keeps friendly symbols intact for any
    terminal that is not forced to a legacy 8-bit code page; failures are swallowed so redirections
    and non-interactive captures keep working.
    """
    for s in (streams or (sys.stdout, sys.stderr)):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001 - never let encoding setup break the run
                pass


def _stream_download(cmd: List[str]) -> 'RunResult':
    """Run the yt-dlp extraction live so its ``[download]`` progress streams to the terminal.

    Returns the :class:`core.RunResult`. Any final ``[download] X% (a/b)`` telemetry is parsed into a
    compact bar printed via the logger. Never raises -- callers inspect ``res.ok`` / the audio path."""
    from .core import run as _run_streamed
    res = _run_streamed(cmd, stream=True)
    if not res.ok or not (res.stdout or "").strip():
        raise RuntimeError("yt-dlp produced no output")
    prog = Logger.parse_download_progress(res.stdout)
    if prog and prog.get("pct"):
        bar = Logger.render_bar(prog["pct"], prog.get("dl"), prog.get("total"))
        _log.note(bar + "   (extracted to audio.<ext>)")
    return res


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
    from .core import build_ytdlp_extract_cmd as b
    return b(ytdlp_exe, watch_id, output_template=out, ffmpeg_dir=ffmpeg_dir)


def build_mix_cmd(ffmpeg_exe: str, bass: str, other: str, vocals: str, output: str) -> List[str]:
    """Two-channel no-drums mix of the three non-drum stems.

    Drums are excluded by construction -- orchestration passes only bass/other/vocals,
    so this builder never mixes the drum stem in.
    """
    from .core import build_ffmpeg_mix_cmd as b
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
    from .core import detect_engine as cde
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
    from .bootstrap import find_ytdlp as fy
    s = fy(scope_dir=scope_dir)
    return s.path if s.ok else None


def _resolve_run_env(args) -> tuple:
    """Resolve tools + engine for one run (cheap file checks; called per item)."""
    scope = os.environ.get("AUTOCV_SCOPE") or os.getcwd()
    uvr_root = os.environ.get("AUTOCV_UVR_ROOT") or ""
    engine = resolve_engine(args, scope, uvr_root)
    ffmpeg_dir = find_ffmpeg(scope)
    return scope, engine, ffmpeg_dir


def process_one(wid: str, args, progress=None) -> ItemResult:
    """Run the full pipeline for ONE watch id; returns an ItemResult.

    Kept separate from run_pipeline so a persistent queue can drive one item at a time,
    mark it done in the queue file, and resume later. Heavy steps are delegated to
    core.separate_stems / provision / subprocesses (yt-dlp + ffmpeg required on PATH/scope).
    """
    res = ItemResult(watch_id=wid)
    workdir = make_workdir()
    audio_path = None
    stems: Dict[str, str] = {}
    try:
        scope, engine, ffmpeg_dir = _resolve_run_env(args)
        if progress:
            progress(wid, "pending", 0, 0)
        ytdlp_exe = _find_ytdlp(scope)
        cmd = build_download_cmd(ytdlp_exe, wid, workdir, ffmpeg_dir=ffmpeg_dir or "")
        res.status, res.stage = "download", "download"
        audio_path = _stream_download(cmd).ok and locate_extracted(workdir)
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
    return res


def run_pipeline(inputs: List[str], args, progress=None) -> List[ItemResult]:
    """Execute the full pipeline for each normalized input; return per-item results.

    Thin wrapper over process_one so a persistent queue (#15/#16) can drive one item at a
    time and save/resume incrementally. `progress(wid, stage, idx, total)` is called before
    each heavy step (optional hook)."""
    scope = os.environ.get("AUTOCV_SCOPE") or os.getcwd()
    engine = resolve_engine(args, scope, os.environ.get("AUTOCV_UVR_ROOT") or "")
    print("auto_convert: %d input(s), engine=%s" % (len(inputs), engine or "n/a"))

    results = []
    for i, wid in enumerate(inputs):
        res = process_one(wid, args)
        if progress and len(inputs) > 1:
            progress(wid, res.stage, i + 1, len(inputs))
        results.append(res)
    return results


def separate(audio_path, engine, workdir, threads, uvr_root=""):
    from .core import separate_stems as ss
    return ss(audio=audio_path, engine=engine, workdir=workdir, threads=threads or 0, uvr_root=uvr_root)


def ffmpeg_exe_for(ffmpeg_dir: str) -> str:
    """Return the ffmpeg binary path inside a dir holding BOTH binaries."""
    if not ffmpeg_dir or not os.path.isdir(ffmpeg_dir):
        from .core import which
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


# --------------------------------------------------------------------------- #
# friendly CLI output helpers (all route through the module logger; quiet by default)
# --------------------------------------------------------------------------- #
def _blank_count(items):
    return sum(1 for i in items if not str(i).strip())


def _colored(text: str, kind) -> str:
    """Colour a string via the module logger's colour flag; plain text when disabled."""
    return _log._c(text, kind) if _log.color_enabled else text


def echo_inputs(raw, titles):
    """Show how each raw input parsed -> normalized watch id (+ best-effort title hint).

    Computes each input's own normalized id (via ``extract_watch_id``) so blank / invalid /
    duplicate entries are all shown and flagged -- giving a transparent view of the dedup result.
    """
    kept = dropped = 0
    for i, w in enumerate(raw, 1):
        hint = titles[i - 1] if i - 1 < len(titles) else None
        tag = ""
        if not isinstance(w, str) or not w.strip():
            wid = ""
            tag = "  (blank)" if isinstance(w, str) and w.strip() == "" else "  (non-string)"
        else:
            try:
                wid = extract_watch_id(w)
            except Exception:
                wid = ""
                tag = "  (invalid)"
        kept += bool(wid)
        dropped += not kept
        line = "%2d. %s -> %s%s" % (i, w, wid, tag)
        hint = '   "%s"' % hint if hint else ""
        _log.note(line + hint)
    tail = "; dropped %d (%d blank / invalid / duplicate)" % (len(raw) - kept, dropped) if dropped else ""
    _log.step("%d raw input(s) -> %d watch id(s) after normalize+dedup%s" % (len(raw), kept, tail))


def describe_tools(ytdlp_path: str = "", ffmpeg_dir: str = "") -> None:
    """Report which yt-dlp + ffmpeg binaries are in use (name/version/install-type/location).

    ``tool_source`` classifies each path into a kind (portable/app/path) and a short label (app name,
    ``portable``, winget/AppData location); :meth:`Logger.tool` renders them as
    ``yt-dlp 2024.7.9 [portable] portable``.
    """
    _log.step("tools")
    if ytdlp_path:
        src = tool_source(ytdlp_path)
        ver = _log.version_of(ytdlp_path, ("-V",)) or "?"
        _log.tool("yt-dlp", ver, kind=src["kind"], loc=_colored(src["label"], "grey"))
    else:
        _log.warn("no yt-dlp found on PATH / scope -- download step will fail")
    if ffmpeg_dir:
        src = tool_source(ffmpeg_dir)
        _log.tool("ffmpeg", "", kind=src["kind"], loc=_colored(src["label"], "grey"))
    else:
        _log.warn("no ffmpeg found -- mux step will fail")


def link_outputs(res: ItemResult) -> None:
    """Print the finished output files as clickable links (drums / no-drums / .meta.json)."""
    if res.ok and (res.drums_path or res.nodrums_path):
        for p in (res.drums_path, res.nodrums_path, res.meta_path):
            if p:
                _log.link(p)


def main(argv=None) -> int:
    # Make Unicode output (block-bar glyphs, etc.) safe even on a legacy cp1252 console.
    ensure_utf8_output()

    args = build_parser().parse_args(argv)
    scope_dir = os.environ.get("AUTOCV_SCOPE") or os.getcwd()
    qpath = getattr(args, "queue", None) or os.environ.get("AUTOCV_QUEUE") \
        or default_queue_path(scope_dir)

    queue = WatchQueue.from_path(qpath)
    raw_inputs = gather_inputs(args)
    if not raw_inputs:
        _log.warn("no input provided (pass -i URL, pipe URLs via stdin, or seed the queue file)")
        return 2

    # show how each input parsed -> normalized id (+ title hint), then dedup into the queue
    titles = [extract_title_hint(w) for w in raw_inputs]
    echo_inputs(raw_inputs, titles)
    newly = queue.add_all(raw_inputs, titles)
    _log.note("seeded %d new item(s) into %s" % (newly, qpath))

    if args.dry_run:
        pending = queue.pending_ids()
        _log.step("dry-run")
        _log.note("[dry-run] would process %d pending item(s): %s"
                  % (len(pending), ", ".join(w[:12] for w in pending) or "(none)"))
        return 0

    # resolve tools once, report install types/locations/version + engine
    ytdlp_path = _find_ytdlp(scope_dir)
    ffmpeg_dir = find_ffmpeg(scope_dir)
    describe_tools(ytdlp_path, ffmpeg_dir)
    engine = resolve_engine(args, scope_dir, os.environ.get("AUTOCV_UVR_ROOT") or "")
    _log.step("engine=%s" % (engine or "none available"))

    # drive the queue one item at a time: mark done + save after each (resumable)
    remaining = list(queue.pending_ids())
    if not remaining:
        _log.note("nothing to do (%d queued, all complete)" % len(remaining))
        return 0
    _log.step("processing %d pending item(s); queue=%s" % (len(remaining), qpath))

    ok = err = done = 0
    for i, wid in enumerate(remaining):
        res = process_one(wid, args)
        hint = (' "%s"' % res.title) if res.title else ""
        _log.step("%2d/%d  %s%s" % (i + 1, len(remaining), wid[:16], hint))
        link_outputs(res)
        if not res.ok:
            _log.fail("%s: %s" % (res.watch_id[:16], res.error or "unknown error"))
        if res.ok:
            ok += 1
        else:
            err += 1
        queue.mark_done(wid)
        done += 1
        try:
            queue.save()
        except OSError as exc:
            _log.warn("could not persist queue (%s): %s" % (qpath, exc))
    _log.summary(ok, len(remaining), ok, err)
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
