"""Main CLI orchestrator for jamly.

Per input (any yt-dlp source) the pipeline is::

    download -> yt-dlp best-audio extract   (container chosen natively by the stream)
         -> separate_stems() four-stems      (torch `demucs` CLI, model htdemucs)
         -> ffmpeg no-drums mix               (bass + other + vocals)
         -> per-stem encode-or-copy           (lossy format default; raw WAV on request)

Outputs per item: ``<title> [<id>]_drums.<fmt>`` and
``<title> [<id>]_no_drums.<fmt>`` where ``<fmt>`` is the
chosen audio format (see :func:`build_parser`'s ``--audio-format``); lossy MP3 is the default,
raw WAV is opt-in. Each output keeps a ``.meta.json`` sidecar.
Heavy/networked steps are delegated to core (and the subprocesses it spawns); this module's own
gathering/dedup, output naming, stage command building, per-item status, dry-run preview, and CLI
wiring -- is pure or unit-testable without GPU/network.

Environment knobs (see bootstrap.py): JAMLY_SCOPE.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import json
import tempfile
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .normalize import normalize_inputs_full, make_input, Input
from .core import (
    AUDIO_FORMATS, DEFAULT_OUTPUT_FORMAT, FORMAT_WAV,
    build_stem_encode_cmd,
    ENGINE_HTDEMUCS,
    RunResult, _abs, _print_tail, PLATFORM_WINDOWS, run as _run, sanitize_filename,
    tool_source,
)
from .bootstrap import find_ffmpeg
from .logging import SYMBOL, Logger
from .naming import parse_title_artist, stem_prefix, build_metadata_name, meta_dict
from jamly.watch_queue import WatchQueue, default_queue_path

# best-effort metadata lookup timeout (seconds); the whole thing is wrapped so a slow/failing yt-dlp
# never blocks or fails item processing -- process_one degrades to the watch-id fallback.
METADATA_FETCH_TIMEOUT = 60

ENGINE_CHOICES = ("auto", "htdemucs")


# module-level logger; quiet by default so importing this module never prints (tests/callers opt in).
# Tests replace it via ``monkeypatch.setattr("jamly.run._log", ...)`` to capture output.
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
    """Stream the yt-dlp extraction live so its ``[download] X% ...`` progress shows in real time.

    Output is written to the terminal (NOT captured -- RunResult.stdout/.stderr are empty, so
    success is decided by returncode). A compact bar is drawn for each line carrying download
    telemetry via ``on_line``. Never raises -- callers inspect ``res.ok`` / the audio path."""
    from .core import run as _run_streamed

    def _render_progress(line: str) -> None:
        prog = Logger.parse_download_progress(line)
        if prog and prog.get("pct"):
            bar = Logger.render_bar(prog["pct"], prog.get("dl"), prog.get("total"))
            _log.note(bar + "   (extracting audio)")

    res = _run_streamed(cmd, stream=True, on_line=_render_progress)
    return res


# --------------------------------------------------------------------------- #
# per-item result / status model (shared by the queue + server later)
# --------------------------------------------------------------------------- #
@dataclass
class ItemResult:
    watch_id: str
    title: Optional[str] = None
    status: str = "pending"      # lifecycle: pending|download|separate|mix|done|skipped|error
    stage: str = ""              # which pipeline stage is in progress ("" when idle)
    drums_path: Optional[str] = None
    nodrums_path: Optional[str] = None
    meta_path: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status in ("done", "skipped")

    @property
    def finished(self) -> bool:
        # finished at all -- success ("done") OR failure ("error"); distinct from ``ok`` above.
        return self.status in ("done", "skipped", "error")


@dataclass
class Plan:
    """Resolved pipeline for one watch id (produced before any heavy execution)."""
    watch_id: str
    title: Optional[str] = None
    workdir: str = ""           # per-item temp dir holding the extracted audio + stems
    output_dir: str = ""         # where the named drums/no-drums pair lands
    engine: str = "auto"
    threads: int = 0


# --------------------------------------------------------------------------- #
# stage command builders (pure; paths already resolved to absolute)
# --------------------------------------------------------------------------- #
def build_download_cmd(
    ytdlp_exe: str,
    watch_id: str,
    workdir: str,
    source_url: Optional[str] = None,
    ffmpeg_dir: str = "",
    suppress_subs: Optional[bool] = None,
) -> List[str]:
    """Extract best audio into <workdir>/<watch_id>.<ext>; container decided by yt-dlp.

    The extract passes the *source url* (full URL / partial path, or a bare id if that is all we
    have) to yt-dlp so any host it understands resolves. The stable id is kept for output filenames.
    When ``source_url`` is None (e.g. a bare id was fed in), the bare id itself is used as yt-dlp's
    URL argument -- matching the historical YouTube-only behaviour.

    ``suppress_subs`` forwards to :func:`build_ytdlp_extract_cmd` so subtitle downloads are skipped
    for this audio-only pipeline on every host; pass False only if a specific build rejects it.
    None means "no host-specific override" -> the builder's portable default applies."""
    url = source_url if source_url else watch_id
    out = os.path.join(workdir, watch_id + ".%(ext)s")
    from .core import build_ytdlp_extract_cmd as b
    # None => let the builder apply its portable default; an explicit value is forwarded verbatim.
    return b(ytdlp_exe, url, output_template=out, ffmpeg_dir=ffmpeg_dir,
             suppress_subs=suppress_subs)


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
def gather_inputs(args) -> List[Input]:
    """Combine --input/-i values and stdin (if piped), then normalize/dedupe into :class:`Input`\nobjects.

    Each item carries a stable filesystem-safe id and the original source text. The resumable queue
    keys on the stable id and retains the source needed by yt-dlp."""
    # argparse dest for -i/--input is 'input'; gather_inputs accepts any iterable of strings
    sources = list(getattr(args, "input", []) or [])
    if sys.stdin.isatty() is False:
        try:
            data = sys.stdin.read()
            if data.strip():
                sources.extend(data.splitlines())
        except (ValueError, OSError):
            pass
    return normalize_inputs_full(sources)


def resolve_engine(args, scope_dir: str) -> Optional[str]:
    """Pick a usable engine. `args.engine` overrides auto-selection; returns None if none.

    There is exactly one engine (the Demucs PyPI CLI). Auto resolves to
    ``ENGINE_HTDEMUCS`` when the package is available and ``None`` otherwise; an explicit choice
    must name that same engine -- any other value never runs, so a stale/unknown selection degrades
    to None rather than executing."""
    from .core import detect_engine as cde
    chosen = getattr(args, "engine", "auto")
    if chosen == "auto":
        return cde(scope_dir=scope_dir)
    # only the demucs CLI engine exists; an explicit choice must name it AND be available
    if chosen == ENGINE_HTDEMUCS:
        return ENGINE_HTDEMUCS if cde(scope_dir=scope_dir) else None
    return None


def make_workdir() -> str:
    return tempfile.mkdtemp(prefix="jamly-")


def locate_extracted(workdir: str) -> Optional[str]:
    """Return the extracted audio file path from yt-dlp's output template.

    Extraction runs before any stem files exist, so ``workdir`` holds exactly one file -- the
    extracted audio. Return it (sorted for determinism); ``None`` when there is no regular file.
    No format preference: yt-dlp chose the container and we take whatever single file it wrote."""
    files = sorted(f for f in os.listdir(workdir) if os.path.isfile(os.path.join(workdir, f)))
    return _abs(os.path.join(workdir, files[0])) if files else None


# --------------------------------------------------------------------------- #
# per-host overrides (kept in env, never baked into shared code)
# --------------------------------------------------------------------------- #
def _resolve_suppress_subs() -> Optional[bool]:
    """yt-dlp subtitle-suppression override for this host, or None to use the portable default.

    Reads ``JAMLY_SUPPRESS_SUBS`` ("0"/"1"). Returns None when unset so callers fall back to the
    builder's portable default; a value is returned only when a specific build needs it. Host quirks
    live in env, not shared code."""
    val = os.environ.get("JAMLY_SUPPRESS_SUBS")
    return None if val is None else (val != "0")


# --------------------------------------------------------------------------- #
# sequential orchestration (delegates heavy steps to core)
# --------------------------------------------------------------------------- #
def _find_ytdlp(scope_dir: str):
    from .bootstrap import find_ytdlp as fy
    s = fy(scope_dir=scope_dir)
    return s.path if s.ok else None


def _resolve_run_env(args) -> tuple:
    """Resolve tools + engine for one run (cheap file checks; called per item).

    Scope resolution order is explicit so the CLI and server share one contract: an ``args.scope``
    override wins when present -- that is the HTTP server's --scope, which must govern tool
    resolution exactly as it governs where the queue file lives (see ItemServer). Otherwise fall
    back to JAMLY_SCOPE then cwd. The bare CLI namespace has no scope attribute, so getattr() is
    None and ``python run.py`` behaves identically to before.
    """
    scope = getattr(args, "scope", None) or os.environ.get("JAMLY_SCOPE") or os.getcwd()

    engine = resolve_engine(args, scope)
    # No reuse cache exists at runtime: always resolve ffmpeg by a live PATH/app scan.
    ffmpeg_dir = find_ffmpeg(scope)
    return scope, engine, ffmpeg_dir


def stem_output_paths(output_dir: str, watch_id: str, fmt: str,
                      parsed: Optional[Tuple[str, str]] = None) -> tuple:
    """Consistent output paths for one item's stems + sidecar.

    Both stems share a sanitized human prefix and always retain the stable id. The metadata sidecar
    is named directly from that id.
    """
    safe_id = sanitize_filename(watch_id)
    prefix = sanitize_filename(stem_prefix(watch_id, parsed))
    if parsed:
        max_human = max(1, 180 - len(safe_id) - 3)
        prefix = "%s [%s]" % (prefix[:max_human].rstrip(), safe_id)
    nodrums = os.path.join(output_dir, "%s_no_drums.%s" % (prefix, fmt))
    drums = os.path.join(output_dir, "%s_drums.%s" % (prefix, fmt))
    meta = os.path.join(output_dir, "%s.meta.json" % watch_id)
    return drums, nodrums, meta


def _fetch_item_metadata(ytdlp_exe: str, source_url: Optional[str]) -> Optional[dict]:
    """Best-effort real title/artist/uploader/duration from yt-dlp JSON.

    Returns a dict with keys title, artist, author, uploader, duration_seconds and id -- or None if
    the lookup fails (offline, bare id, or error). Never raises.
    """
    try:
        if not source_url:
            return None
        cmd = [ytdlp_exe, "--dump-single-json", "--skip-download", "--no-playlist", source_url]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=METADATA_FETCH_TIMEOUT)
    except Exception:  # noqa: BLE001 - metadata is best-effort; any failure degrades to fallback
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        payload = json.loads(proc.stdout)
    except (TypeError, ValueError):
        return None
    fields = {
        "title": payload.get("title"),
        "artist": payload.get("artist") or payload.get("creator"),
        "author": payload.get("channel") or payload.get("uploader"),
        "uploader": payload.get("uploader") or payload.get("channel"),
        "duration": payload.get("duration"),
        "id": payload.get("id"),
    }
    dur = fields.get("duration")
    try:
        fields["duration_seconds"] = int(dur) if dur is not None else None
    except (TypeError, ValueError):
        fields["duration_seconds"] = None
    return {k: v for k, v in fields.items() if v not in (None, "")}


def _temporary_output_path(path: str) -> str:
    root, ext = os.path.splitext(os.path.abspath(path))
    return "%s.tmp-%s%s" % (root, uuid.uuid4().hex[:10], ext)


def process_one(wid: str, args, progress=None, source_url: Optional[str] = None) -> ItemResult:
    """Run the full pipeline for one stable input id."""
    res = ItemResult(watch_id=wid)
    workdir = ""
    temporary_outputs: List[str] = []
    source = source_url or wid
    try:
        scope, engine, ffmpeg_dir = _resolve_run_env(args)
        ytdlp_exe = _find_ytdlp(scope)
        ffmpeg_exe = ffmpeg_exe_for(ffmpeg_dir)
        if not ytdlp_exe:
            raise RuntimeError("yt-dlp is not installed or not on PATH")
        if not engine:
            raise RuntimeError("demucs is not installed; reinstall jamly with its runtime dependencies")
        if not ffmpeg_exe:
            raise RuntimeError("ffmpeg and ffprobe are required on PATH")

        output_dir = os.path.abspath(os.path.expanduser(args.output_dir))
        os.makedirs(output_dir, exist_ok=True)
        fmt = getattr(args, "audio_format", DEFAULT_OUTPUT_FORMAT) or DEFAULT_OUTPUT_FORMAT
        item_meta = _fetch_item_metadata(ytdlp_exe, source) or {}
        parsed = parse_title_artist(item_meta.get("title"))
        if not parsed and item_meta.get("artist") and item_meta.get("title"):
            parsed = (item_meta["artist"], item_meta["title"])
        drums_out, nodrums_out, meta_path = stem_output_paths(
            output_dir, wid, fmt, parsed=parsed)

        existing = [path for path in (drums_out, nodrums_out, meta_path) if os.path.exists(path)]
        if existing and not args.replace_existing:
            if len(existing) == 3:
                res.status = "skipped"
                res.title = item_meta.get("title") or wid
                res.drums_path = drums_out
                res.nodrums_path = nodrums_out
                res.meta_path = meta_path
                return res
            raise RuntimeError(
                "partial outputs already exist; pass --replace-existing: %s"
                % ", ".join(existing))

        if progress:
            progress(wid, "pending", 0, 0)
        workdir = make_workdir()
        cmd = build_download_cmd(
            ytdlp_exe,
            wid,
            workdir,
            source_url=source,
            ffmpeg_dir=ffmpeg_dir or "",
            suppress_subs=_resolve_suppress_subs(),
        )
        res.status, res.stage = "download", "download"
        audio_path = _stream_download(cmd).ok and locate_extracted(workdir)
        if not audio_path:
            raise RuntimeError("yt-dlp extract produced no audio file")

        res.status, res.stage = "separate", "separate"
        stems: Dict[str, str] = separate(audio_path, workdir, args.threads or 0)
        missing = [name for name in ("drums", "bass", "other", "vocals") if not stems.get(name)]
        if missing:
            raise RuntimeError("separation did not produce: %s" % ", ".join(missing))

        res.status, res.stage = "mix", "mix"
        nodrums_wav = os.path.join(workdir, "no_drums.wav")
        mix_cmd = build_mix_cmd(
            ffmpeg_exe, stems["bass"], stems["other"], stems["vocals"], nodrums_wav)
        mix_res = _run(mix_cmd)
        if not mix_res.ok:
            _print_tail(mix_res.stderr)
            raise RuntimeError(
                "mix failed (exit %d): %s"
                % (mix_res.returncode, " ".join(mix_res.args[:4])))

        drums_tmp = _temporary_output_path(drums_out)
        nodrums_tmp = _temporary_output_path(nodrums_out)
        meta_tmp = _temporary_output_path(meta_path)
        temporary_outputs.extend((drums_tmp, nodrums_tmp, meta_tmp))
        finalize_output(ffmpeg_exe, stems["drums"], drums_tmp, fmt)
        finalize_output(ffmpeg_exe, nodrums_wav, nodrums_tmp, fmt)

        title = item_meta.get("title") or ""
        artist = item_meta.get("artist") or (parsed[0] if parsed else "")
        uploader = item_meta.get("uploader") or ""
        duration_seconds = item_meta.get("duration_seconds") or 0
        with open(meta_tmp, "w", encoding="utf-8") as fh:
            json.dump(
                meta_dict(
                    watch_id=wid,
                    source_url=source,
                    title=title,
                    artist=artist,
                    uploader=uploader,
                    duration=duration_seconds,
                ),
                fh,
                ensure_ascii=False,
                indent=2,
            )

        for temporary, final in (
            (drums_tmp, drums_out),
            (nodrums_tmp, nodrums_out),
            (meta_tmp, meta_path),
        ):
            os.replace(temporary, final)
            temporary_outputs.remove(temporary)

        res.status, res.stage = "done", ""
        res.title = title or build_metadata_name(wid, parsed, "original")
        res.drums_path = drums_out
        res.nodrums_path = nodrums_out
        res.meta_path = meta_path
    except Exception as exc:  # noqa: BLE001 - record and continue across the queue
        res.status, res.stage, res.error = "error", "", str(exc)
    finally:
        for path in temporary_outputs:
            try:
                os.remove(path)
            except OSError:
                pass
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    return res


def run_pipeline(inputs: List[Input], args, progress=None) -> List[ItemResult]:
    """Execute the full pipeline for each normalized input; return per-item results.

    Thin wrapper over process_one so a persistent queue (#15/#16) can drive one item at a
    time and save/resume incrementally. `progress(wid, stage, idx, total)` is called before
    each heavy step (optional hook)."""
    scope = getattr(args, "scope", None) or os.environ.get("JAMLY_SCOPE") or os.getcwd()
    engine = resolve_engine(args, scope)
    print("jamly: %d input(s), engine=%s" % (len(inputs), engine or "n/a"))

    results = []
    for i, item in enumerate(inputs):
        res = process_one(item.watch_id, args, source_url=item.source_url)
        if progress and len(inputs) > 1:
            # label on the stable watch id (the queue keys on it); res.title mirrors that
            progress(item.watch_id, res.stage, i + 1, len(inputs))
        results.append(res)
    return results


def separate(audio_path, workdir, threads=0):
    from .core import separate_stems as ss
    return ss(audio=audio_path, workdir=workdir, threads=threads)


def ffmpeg_exe_for(ffmpeg_dir: str) -> str:
    """Return the ffmpeg binary path inside a dir holding BOTH binaries."""
    if not ffmpeg_dir or not os.path.isdir(ffmpeg_dir):
        from .core import which
        return which("ffmpeg") or ""
    name = "ffmpeg.exe" if PLATFORM_WINDOWS else "ffmpeg"
    p = os.path.join(_abs(ffmpeg_dir), name)
    return _abs(p) if os.path.isfile(p) else ""


def finalize_output(ffmpeg_exe: str, src_wav: str, dst_path: str, fmt: str) -> bool:
    """Write one final stem file in `fmt`: re-encode for lossy, copy verbatim for raw WAV.

    Returns True on success. Raw (``wav``) output copies the source verbatim and needs no encoder,
    so it works even when ffmpeg is unavailable; a lossy target requires ffmpeg to encode -- if that
    step fails we raise so the item is marked errored rather than "done" with a missing file.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
    if fmt == FORMAT_WAV:
        shutil.copy2(_abs(src_wav), _abs(dst_path))   # raw WAV -> verbatim, no encoder needed
        return True
    enc = build_stem_encode_cmd(ffmpeg_exe, src_wav, dst_path, fmt=fmt)
    res = _run(enc)
    if not res.ok:
        raise RuntimeError("failed to encode %s output (exit %d): %s" % (
            fmt, res.returncode, " ".join(res.args[:4])))
    return True


# --------------------------------------------------------------------------- #
# CLI entrypoint
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jamly", description="Download audio from YouTube and split drums / no-drums.")
    p.add_argument("-i", "--input", action="append", metavar="URL_OR_ID",
                   help="YouTube link/id/partial URL (repeatable). Also reads stdin when piped.")
    p.add_argument("-o", "--output-dir", default="output",
                   help="Directory for the drums/no-drums pair (default: output)")
    p.add_argument("--scope", default=None,
                   help="Working scope for tool resolution and the default queue (default: cwd)")
    p.add_argument("--queue", default=None,
                   help="Queue file path (default: env JAMLY_QUEUE or <scope>/jamly_queue.json)")
    p.add_argument("--audio-format", choices=AUDIO_FORMATS, default=DEFAULT_OUTPUT_FORMAT,
                   help=("Final audio format; 'wav' = lossless/raw WAVs, '%s' = reasonably-encoded lossy (default)"
                         % DEFAULT_OUTPUT_FORMAT))
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
    """Show how each input maps to its stable queue id (+ best-effort title hint).

    ``raw`` may be bare-id strings or the :class:`Input` objects returned by gather_inputs; either
    way this computes each item's stable id so blank / invalid /
    duplicate entries are all shown and flagged -- giving a transparent view of the dedup result."""
    kept = dropped = 0
    seen = set()
    for i, w in enumerate(raw, 1):
        # accept bare-id strings or Input objects (gather_inputs returns the latter); either way
        # echo its own normalized id so blank / invalid / duplicate entries are all flagged.
        source = getattr(w, "source_url", w)
        hint = titles[i - 1] if i - 1 < len(titles) else None
        tag = ""
        if not isinstance(source, str) or not source.strip():
            wid = ""
            tag = "  (blank)" if isinstance(source, str) and source.strip() == "" else "  (non-string)"
        else:
            try:
                # ``w`` may be an :class:`Input`; extract from its string source (not the object,
                # which has no .strip()) so a valid bare id / URL is recognised, not flagged invalid.
                wid = make_input(source).watch_id
            except Exception:
                wid = ""
                tag = "  (invalid)"
        if wid and wid in seen:
            tag = "  (duplicate)"
            dropped += 1
        elif wid:
            seen.add(wid)
            kept += 1
        else:
            dropped += 1
        line = "%2d. %s -> %s%s" % (i, w, wid, tag)
        hint = '   "%s"' % hint if hint else ""
        _log.note(line + hint)
    tail = "; dropped %d (%d blank / invalid / duplicate)" % (len(raw) - kept, dropped) if dropped else ""
    _log.step("%d raw input(s) -> %d stable id(s) after normalize+dedup%s" % (
        len(raw), kept, tail))


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
    scope_dir = args.scope or os.environ.get("JAMLY_SCOPE") or os.getcwd()
    qpath = getattr(args, "queue", None) or os.environ.get("JAMLY_QUEUE") \
        or default_queue_path(scope_dir)

    queue = WatchQueue.from_path(qpath)
    raw_inputs = gather_inputs(args)
    if raw_inputs:
        titles = [extract_title_hint(i.source_url) for i in raw_inputs]
        echo_inputs(raw_inputs, titles)
        newly = queue.add_all(
            [i.watch_id for i in raw_inputs],
            titles,
            [i.source_url for i in raw_inputs],
        )
        _log.note("seeded %d new item(s) into %s" % (newly, qpath))
    elif not queue.pending_ids():
        _log.warn("no input provided and the queue has no pending items")
        return 2

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
    engine = resolve_engine(args, scope_dir)
    _log.step("engine=%s" % (engine or "none available"))

    # drive the queue one item at a time: mark done + save after each (resumable)
    remaining = list(queue.pending_items())
    if not remaining:
        _log.note("nothing to do (all queued items are complete)")
        return 0
    _log.step("processing %d pending item(s); queue=%s" % (len(remaining), qpath))

    # terminal status per item maps onto the shared vocab used by the popup/options UI:
    # DONE -> green, ERROR -> red; every other stage is transient inside process_one() and
    # only observable live on an SSE client. The badge word/icon/color match the browser surfaces.
    def _stage_badge(status: str) -> str:
        # done/error reuse the browser vocab's exact glyphs (U+2714 / U+2717) so both surfaces
        # render identical icons; other stages fall back to a dim info bullet.
        icon = {"done": "\u2714", "skipped": "\u2714", "error": "\u2717"}.get(
            status, SYMBOL["info"])
        label = {
            "done": "DONE",
            "skipped": "SKIPPED",
            "error": "ERROR",
        }.get(status, status or "PENDING")
        color = {
            "done": "green",
            "skipped": "green",
            "error": "red",
            "separate": "cyan",
            "mix": "yellow",
            "download": "blue",
        }.get(status, "grey")
        return "%s %s" % (_log._c(icon, color), _log._c(label, color))

    ok = err = done = 0
    for i, item in enumerate(remaining):
        wid = item["watch_id"]
        res = process_one(wid, args, source_url=item.get("source_url") or wid)
        badge = _stage_badge(res.status)
        title = (' "%s"' % res.title) if res.title else ""
        _log.step("%2d/%d  %s%s%s" % (i + 1, len(remaining), badge, "  ", wid[:16] + title))
        link_outputs(res)
        if not res.ok:
            _log.fail("%s: %s" % (res.watch_id[:16], res.error or "unknown error"))
        if res.ok:
            ok += 1
            # only a success is terminal: a failed item stays PENDING so it is retried on the next
            # run (a persistent failure would otherwise be marked done and dropped from the queue).
            queue.mark_done(wid)
        else:
            err += 1
        done += 1
        try:
            queue.save()
        except OSError as exc:
            _log.warn("could not persist queue (%s): %s" % (qpath, exc))
    _log.summary(ok, len(remaining), ok, err)
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
