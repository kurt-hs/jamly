# auto-convert

A small, portable **four-stem audio-separation** tool. Given a YouTube link it downloads the
highest-quality available audio and splits it into two WAV stems:

```
drums.wav      – drums only
no-drums.wav   – everything else (vocals + instruments)
```

Separation uses the **Demucs v4 four-stems (`htdemucs`) ONNX Runtime** model, so it runs with
**no PyTorch / no heavy installs**. If an Ultimate Vocal Remover 5 (UVR5) install is present on
the machine its bundled models are reused automatically instead.

## How it works

1. Normalize the input to a YouTube watch ID (full URL, partial path, or bare ID).
2. `yt-dlp` extracts the highest-quality audio in its native lossless container — passed straight
   through to separation with no re-encode.
3. Demucs v4 four-stems (`htdemucs`) split it; the **drums** stem and the mix of the remaining three
   stems are written as `drums.wav` / `no-drums.wav`.

## Layout

```
src/auto_convert/
  normalize.py    URL -> YouTube watch-id extraction/validation + dedup
  core.py         engine auto-detection (htdemucs ONNX vs UVR5), command builders, stem run
  bootstrap.py    dependency/tool detection (python, yt-dlp, ffmpeg, UVR5)
  provision.py    self-provision missing tools into .portable/<os>/ (reuse-first)
  queue.py        persistent, resumable input queue (JSON-backed, in-memory usable)
  run.py          CLI orchestrator: download -> separate -> metadata/renaming
  server.py       HTTP(S) server with SSE `GET /status` progress streaming
tests/            unittest suite (absolute-package imports; runs under pytest or -m unittest)
PLAN.md           long-form design + portability research notes
AGENTS.md         contributing & coding conventions
```

## Quick start

From a checkout:

```bash
python src/auto_convert/run.py -i <youtube-url> [-i <more>] [--engine auto|uvr5|htdemucs]
# or, if installed via pip install -e . :  auto-convert -i <youtube-url>
```

Run the test suite (pytest OR unittest):

```bash
python -m pytest                    # uses [tool.pytest.ini_options] pythonpath=["src"]
python -m unittest discover -s tests
```

## HTTP server mode

`server.py` runs a small endpoint for browser userscripts/extension popups: `GET /status` streams
progress JSON, `/items` adds videos to the queue. See `PLAN.md §11` for the full UX design.

## Notes on portability

The tool is designed to "just work" on any machine. `provision.py` self-heals a private
`.portable/<os>/` scope: it reuses already-installed tools first and only downloads what is missing
(static ffmpeg+ffprobe build, ONNX Runtime + the Demucs weights). See `PLAN.md §10` for details.
