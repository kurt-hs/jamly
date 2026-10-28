# Design

Jamly is a small orchestration layer around yt-dlp, Demucs, and ffmpeg:

```text
source
  -> yt-dlp best available audio
  -> Demucs htdemucs four-stem separation
  -> drums stem
  -> bass + other + vocals -> no-drums mix
  -> matching WAV or MP3 outputs + metadata
```

## Packaging and supported Python

The package supports Python 3.11 and 3.12. yt-dlp and Demucs are declared runtime dependencies.
Demucs currently constrains PyTorch to versions without Python 3.13 wheels on some platforms, so
Python 3.13 is deliberately excluded.

ffmpeg and ffprobe remain system dependencies. `bootstrap.py` detects them from explicit paths,
legacy portable directories, or `PATH`; it does not download or install binaries.

## Inputs and stable ids

`normalize.py` separates the processing source from the filesystem-safe queue id:

- YouTube ids and URLs use the canonical 11-character video id.
- Other yt-dlp sources use `src-<16 hex characters>`, derived from SHA-256 of the exact input.
- The original source is retained in every `Input` and queue record.

This allows arbitrary yt-dlp URLs and extractor expressions without placing raw URLs in filenames or
losing the source when work is resumed from the queue.

## Queue

`WatchQueue` persists:

```json
{
  "items": [
    {
      "watch_id": "src-0123456789abcdef",
      "source_url": "https://soundcloud.com/artist/song",
      "title": null
    }
  ],
  "done": [],
  "updated_at": 0
}
```

The stable id is the deduplication key. `source_url` is passed to yt-dlp. Writes use a unique
temporary file followed by `os.replace`.

## Download and metadata

The extraction command uses:

```text
yt-dlp --no-playlist --no-write-subs --extract-audio
       --audio-format best --audio-quality 0
       -o <workdir>/<id>.%(ext)s <source>
```

Metadata is queried before expensive work with `--dump-single-json --skip-download --no-playlist`.
That establishes deterministic output names and allows complete existing outputs to be skipped.

## Separation

The official Demucs CLI has no `separate` subcommand:

```text
python -m demucs --out <workdir> -n htdemucs [-j N] <audio>
```

With `--out <workdir>`, Demucs writes `<workdir>/htdemucs/<track>/<stem>.wav`. Jamly scans the unique
per-item work directory for exactly `drums`, `bass`, `other`, and `vocals`.

Availability uses `importlib.util.find_spec("demucs")`; arbitrary `sys.path` directories are never
treated as site-packages.

## Mix and finalization

ffmpeg mixes the three non-drum stems with a labeled filter output:

```text
[0:a][1:a][2:a]amix=inputs=3:duration=longest:normalize=0:dropout_transition=0[mix]
```

The intermediate no-drums file is float WAV. Both drums and no-drums then pass through the same
finalizer:

- WAV: verbatim copy of the float stem/mix.
- MP3: libmp3lame at 320 kbps.

Files are written to same-directory temporary paths and atomically replaced only after both audio
outputs and metadata succeed. Failed runs therefore do not leave a newly created partial pair.

Named outputs always include the stable id:

```text
Artist - Song [id]_drums.mp3
Artist - Song [id]_no_drums.mp3
id.meta.json
```

## Server

`ItemServer` owns one in-memory `WatchQueue` and one worker. Queue mutation and status snapshots use
short locks; downloads and inference run outside locks.

`GET /status` is a continuous SSE stream. `GET /status?once=1` sends one identical event and closes,
which makes browser polling bounded without maintaining a permanent connection.

Off-loopback binds require a bearer token for `POST /add`. CORS preflight permits `Authorization`
and private-network requests. Completed event history is bounded to the latest 100 records.

## Browser clients

The Manifest V3 extension and userscript poll the one-event status endpoint. The extension stores the
resolved status object, never the pending Promise. Its options page stores an optional bearer token
and requests permission only for the configured non-local origin.

The userscript is limited to YouTube and YouTube Music pages. All server-provided text is escaped
before insertion into HTML.

## Service wrappers

The Windows batch file sets `PYTHONPATH=<repo>/src` before invoking the module. The scheduled task
executes that launcher through `cmd.exe` at startup.

The Linux launcher either runs in the foreground or generates a user-level systemd unit containing
the resolved repository, Python, port, scope, and `PYTHONPATH`.

## Validation

CI runs pytest, unittest, Ruff, JavaScript contract tests, lock verification, and package builds on
Python 3.11 and 3.12. Tests mock network/model work; focused integration checks exercise real ffmpeg
commands and the live HTTP/SSE contract.
