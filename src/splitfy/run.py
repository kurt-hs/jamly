"""Main CLI orchestrator for splitfy.

Per input (YouTube link / partial URL / bare watch id) the pipeline is::

    download -> yt-dlp best-audio extract   (container chosen natively by the stream)
         -> separate_stems() four-stems      (htdemucs ONNX or UVR5 audio-separator)
         -> ffmpeg no-drums mix               (bass + other + vocals, 2ch)
         -> per-stem encode-or-copy           (lossy format default; raw WAV on request)

Outputs per item: ``<title>_drums.<fmt>`` and ``<title>_no_drums.<fmt>`` where ``<fmt>`` is the
chosen audio format (see :func:`build_parser`'s ``--audio-format``); lossy MP3 is the default,
raw WAV is opt-in. Each output keeps a ``.meta.json`` sidecar.
Heavy/networked steps are delegated to core/provision; this module's own logic -- input
gathering/dedup, output naming, stage command building, per-item status, dry-run preview, and CLI
wiring -- is pure or unit-testable without GPU/network.

Environment knobs (see bootstrap.py): AUTOCV_SCOPE, AUTOCV_UVR_ROOT.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .normalize import normalize_inputs_full, extract_watch_id, Input
from .core import (
    AUDIO_FORMATS, DEFAULT_OUTPUT_FORMAT, FORMAT_WAV,
    build_stem_encode_cmd,
    ENGINE_UVR5, ENGINE_HTDEMUCS,
    RunResult, _abs, PLATFORM_WINDOWS, run as _run, sanitize_filename,
    tool_source,
)
from .bootstrap import find_ffmpeg
from .provision import resolve_recorded_tools
from .logging import Logger
from .naming import parse_title_artist, stem_prefix, build_metadata_name, meta_dict
from splitfy.watch_queue import WatchQueue, default_queue_path

# best-effort metadata lookup timeout (seconds); the whole thing is wrapped so a slow/failing yt-dlp
# never blocks or fails item processing -- process_one degrades to the watch-id fallback.
METADATA_FETCH_TIMEOUT = 300

ENGINE_CHOICES = ("auto", "uvr5", "htdemucs")


# module-level logger; quiet by default so importing this module never prints (tests/callers opt in).
# Tests replace it via ``monkeypatch.setattr("splitfy.run._log", ...)`` to capture output.
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
    have) to yt-dlp so any host it understands resolves -- not just YouTube. The watch-id prefix is
    kept for output filenames only, so every stem produced later stays traceable to one name.
    When ``source_url`` is None (e.g. a bare id was fed in), the bare id itself is used as yt-dlp's
    URL argument -- matching the historical YouTube-only behaviour.

    ``suppress_subs`` forwards to :func:`build_ytdlp_extract_cmd` so subtitle downloads are skipped
    for this lossless-audio pipeline on every host; pass False only if a specific build rejects it.
    None means "no host-specific override" -> the builder's portable default applies."""
    url = source_url if source_url else watch_id
    out = os.path.join(workdir, watch_id)
    from .core import build_ytdlp_extract_cmd as b
    # None => let the builder apply its portable default; an explicit value is forwarded verbatim.
    return b(ytdlp_exe, url, output_template=out, ffmpeg_dir=ffmpeg_dir,
             suppress_subs=(suppress_subs or True))


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

    Each item carries the extracted watch id *and* the original source text, so yt-dlp receives a
    full URL / partial path (not just a bare id) for any host it understands -- while output names
    stay rooted on the short watch id. The resumable queue keys only on ``watch_id``."""
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

    Reads ``AUTOCV_SUPPRESS_SUBS`` ("0"/"1"). Returns None when unset so callers fall back to the
    builder's portable default; a value is returned only when a specific build needs it. Host quirks
    live in env, not shared code."""
    val = os.environ.get("AUTOCV_SUPPRESS_SUBS")
    return None if val is None else (val != "0")


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
    # reuse-first: if a prior run recorded a portable ffmpeg path that still exists on disk,
    # prefer it here so we skip the broader PATH/app scan (resolve_recorded_tools returns {} when
    # nothing usable is recorded -> find_ffmpeg(scope) runs unchanged).
    ffmpeg_dir = resolve_recorded_tools(scope).get("ffmpeg") or find_ffmpeg(scope)
    # uvr_root is returned (not just used locally) so process_one can pass it straight to core.separate_stems
    return scope, engine, ffmpeg_dir, uvr_root


def stem_output_paths(output_dir: str, watch_id: str, fmt: str,
                      parsed: Optional[Tuple[str, str]] = None) -> tuple:
    """Consistent output paths for one item's stems + sidecar.

    Both stems share the human prefix -- a confident ``[Artist] - [Songname]`` parse when available
    (see :func:`naming.parse_title_artist`), otherwise the bare watch id so outputs stay uniquely
    traceable on every platform and match the yt-dlp extract <wid>.<ext>; each stem then carries an
    indicative suffix (<_drums / _no_drums>). The meta file always mirrors the bare watch id. Pure.
    """
    prefix = stem_prefix(watch_id, parsed)
    nodrums = os.path.join(output_dir, "%s_no_drums.%s" % (prefix, fmt))
    drums = os.path.join(output_dir, "%s_drums.%s" % (prefix, fmt))
    meta = os.path.join(output_dir, "%s.meta.json" % watch_id)
    return drums, nodrums, meta


def _fetch_item_metadata(ytdlp_exe: str, source_url: Optional[str]) -> Optional[dict]:
    """Best-effort real title/artist/uploader/duration from yt-dlp --print.

    Returns a dict with keys title, artist, author, uploader, duration_seconds and id -- or None if
    the lookup fails (offline, bare id, or error). Never raises: process_one falls back to parsing
    the raw input text, then the watch-id prefix, when this returns None.
    """
    try:
        cmd = [
            ytdlp_exe,
            "--print",
            "%(title)s|%(artist)s|%(author)s|%(uploader)s|%(duration)s|%(id)s",
            source_url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=METADATA_FETCH_TIMEOUT)
    except Exception:  # noqa: BLE001 - metadata is best-effort; any failure degrades to fallback
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    fields = {}
    for pair in proc.stdout.splitlines():
        pair = pair.strip()
        if not pair:
            continue
        key, _, value = pair.partition("|")
        if key and value.strip():
            fields[key] = value.strip()
    dur = fields.get("duration", "")
    try:
        fields["duration_seconds"] = int(dur) if dur != "" else None
    except ValueError:
        pass
    return {k: v for k, v in fields.items() if k}


def process_one(wid: str, args, progress=None, source_url: Optional[str] = None) -> ItemResult:
    """Run the full pipeline for ONE watch id; returns an ItemResult.

    Kept separate from run_pipeline so a persistent queue can drive one item at a time,
    mark it done in the queue file, and resume later. Heavy steps are delegated to
    core.separate_stems / provision / subprocesses (yt-dlp + ffmpeg required on PATH/scope).

    ``source_url`` is the original input text (full URL / partial path / bare id) kept from
    normalization; it is passed straight to yt-dlp so any host it understands resolves -- not just
    YouTube. When None, only a bare id was fed in and that same id is used as yt-dlp's URL arg."""
    res = ItemResult(watch_id=wid)
    workdir = make_workdir()
    audio_path = None
    stems: Dict[str, str] = {}
    try:
        scope, engine, ffmpeg_dir, uvr_root = _resolve_run_env(args)
        if progress:
            progress(wid, "pending", 0, 0)
        ytdlp_exe = _find_ytdlp(scope)
        # subtitle-suppression override for this host (env), else the portable default
        cmd = build_download_cmd(
            ytdlp_exe, wid, workdir, source_url=source_url, ffmpeg_dir=ffmpeg_dir or "",
            suppress_subs=_resolve_suppress_subs())
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
        # All three outputs share the watch id as their common prefix: the yt-dlp extract is
        # <wid>.<ext>, and each stem adds an indicative suffix (<_drums / _no_drums>). The id is
        # URL-safe, unique per video, and known before extraction -- so it's a stable filename basis
        # on every platform (no title to rely on yet, no unicode/special chars, no truncation).
        fmt = getattr(args, "audio_format", DEFAULT_OUTPUT_FORMAT) or DEFAULT_OUTPUT_FORMAT
        # best-effort real title/artist/uploader/duration from yt-dlp --print; None -> fall back to
        # parsing the raw input text (a bare id / partial path has no usable fields) then the watch-
        # id prefix. The real title is parsed for filenames, while .meta.json records every field.
        item_meta = _fetch_item_metadata(ytdlp_exe, source_url)
        parsed = parse_title_artist(item_meta["title"] if item_meta else source_url)
        drums_out, nodrums_out, meta_path = stem_output_paths(args.output_dir, wid, fmt, parsed=parsed)
        # Finalize the primary (drums) stem first. Both stems are produced together before success;
        # doing it here means a drum re-encode failure surfaces immediately and never leaves its
        # no-drums complement half-mixed on disk (the workdir is wiped in finally regardless).
        finalize_output(ffmpeg_exe_for(ffmpeg_dir), drums, drums_out, fmt)
        res.status, res.stage = "mix", "mix"
        # No-drums mix: ffmpeg blends bass+other+vocals (drums excluded by construction) into
        # nodrums_out. build_mix_cmd takes the established 5-arg form -- (ffmpeg, bass, other,
        # vocals, output) -- matching core.build_ffmpeg_mix_cmd and its tests; 'drums' is intentionally
        # omitted so it never lands in the mix.
        mix_cmd = build_mix_cmd(ffmpeg_exe_for(ffmpeg_dir), stems["bass"],
                                stems["other"], stems["vocals"], nodrums_out)
        _run(mix_cmd)

        # both stems succeeded: surface the human name and write the structured sidecar. output_dir
        # already holds the written stems, so its parent exists -- safe to open the meta file there.
        res.status, res.stage = "done", ""
        if item_meta is not None:
            title = item_meta["title"] or None
            artist = item_meta.get("artist") or None
            uploader = item_meta.get("uploader") or None
            duration_seconds = item_meta.get("duration_seconds") or 0
        else:
            # no live metadata -- best-effort parse of the raw input, then bare watch id everywhere
            title = parsed[0] if parsed else None
            artist = parsed[1] if parsed else None
            uploader = None
            duration_seconds = 0
        res.title = build_metadata_name(wid, (parsed[0], parsed[1]) if parsed else None, "original")
        human_meta_path = os.path.abspath(meta_path)
        with open(human_meta_path, "w", encoding="utf-8") as fh:
            json.dump(meta_dict(
                watch_id=wid, source_url=source_url, title=title, artist=artist,
                uploader=uploader, duration=duration_seconds), fh)
        res.drums_path = drums_out
        res.nodrums_path = nodrums_out
        res.meta_path = human_meta_path
    except Exception as exc:  # noqa: BLE001 - record and continue across the queue
        res.status, res.stage, res.error = "error", "", str(exc)
    finally:
        if not args.replace_existing:
            shutil.rmtree(workdir, ignore_errors=True)
    return res


def run_pipeline(inputs: List[Input], args, progress=None) -> List[ItemResult]:
    """Execute the full pipeline for each normalized input; return per-item results.

    Thin wrapper over process_one so a persistent queue (#15/#16) can drive one item at a
    time and save/resume incrementally. `progress(wid, stage, idx, total)` is called before
    each heavy step (optional hook)."""
    scope = os.environ.get("AUTOCV_SCOPE") or os.getcwd()
    engine = resolve_engine(args, scope, os.environ.get("AUTOCV_UVR_ROOT") or "")
    print("splitfy: %d input(s), engine=%s" % (len(inputs), engine or "n/a"))

    results = []
    for i, item in enumerate(inputs):
        res = process_one(item.watch_id, args, source_url=item.source_url)
        if progress and len(inputs) > 1:
            # label on the stable watch id (the queue keys on it); res.title mirrors that
            progress(item.watch_id, res.stage, i + 1, len(inputs))
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


def finalize_output(ffmpeg_exe: str, src_wav: str, dst_path: str, fmt: str) -> bool:
    """Write one final stem file in `fmt`: re-encode for lossy, copy verbatim for raw WAV.

    Returns True on success. Raw (``wav``) output copies the source verbatim and needs no encoder,
    so it works even when ffmpeg is unavailable; a lossy target requires ffmpeg to encode -- if that
    step fails we raise so the item is marked errored rather than "done" with a missing file.
    """
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
        prog="splitfy", description="Download audio from YouTube and split drums / no-drums.")
    p.add_argument("-i", "--input", action="append", metavar="URL_OR_ID",
                   help="YouTube link/id/partial URL (repeatable). Also reads stdin when piped.")
    p.add_argument("-o", "--output-dir", default="output",
                   help="Directory for <title>_drums.<fmt> / _no_drums.<fmt> (default: output)")
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
    """Show how each input parsed -> normalized watch id (+ best-effort title hint).

    ``raw`` may be bare-id strings or the :class:`Input` objects returned by gather_inputs; either
    way this computes each item's own normalized id (via ``extract_watch_id``) so blank / invalid /
    duplicate entries are all shown and flagged -- giving a transparent view of the dedup result."""
    kept = dropped = 0
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

    # show how each input parsed -> normalized id (+ title hint), then dedup by watch_id into the
    # queue (the queue keys on ids; original sources are used for yt-dlp resolution only).
    titles = [extract_title_hint(getattr(i, "source_url", i)) for i in raw_inputs]
    echo_inputs(raw_inputs, titles)
    newly = queue.add_all([i.watch_id for i in raw_inputs], titles)
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
