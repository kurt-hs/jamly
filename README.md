# splitfy

A small, portable **four-stem audio-separation** tool. Give it anything `yt-dlp` can fetch —
a full URL, partial path, or bare id from any supported host (YouTube included) — and it
downloads the highest-quality available audio and splits it into two WAV stems:

```
drums.wav      – drums only
no-drums.wav   – everything else (vocals + instruments)
```

Separation uses the **Demucs v4 four-stems (`htdemucs`) ONNX Runtime** model, so it runs with
**no PyTorch / no heavy installs**. If an Ultimate Vocal Remover 5 (UVR5) install is present on
the machine its bundled models are reused automatically instead.

## How it works

1. Normalize each input (`normalize.py`) to a short, stable id and keep the original source text,
   so any host `yt-dlp` understands resolves.
2. `yt-dlp` extracts the highest-quality audio in its native lossless container — passed straight
   through to separation with no re-encode.
3. Demucs v4 four-stems (`htdemucs`) split it; the **drums** stem and the mix of the remaining three
   stems are written as `drums.wav` / `no-drums.wav`.

## Layout

```
src/splitfy/
  normalize.py    any-source input normalization: extract a stable id, validate + dedup
  core.py         engine auto-detection (htdemucs ONNX vs UVR5), command builders, stem run
  bootstrap.py    dependency/tool detection (python, yt-dlp, ffmpeg, UVR5)
  provision.py    self-provision missing tools into .portable/<os>/ (reuse-first)
  queue.py        persistent, resumable input queue (JSON-backed, in-memory usable)
  run.py          CLI orchestrator: download -> separate -> metadata/renaming
  server.py       HTTP(S) server with SSE `GET /status` progress streaming
tests/            unittest suite (absolute-package imports; runs under pytest or -m unittest)
browser/extension # MV3 WebExtension + shared DOM-free client (manifest, service worker,
                  popup/options HTML); browser/userscripts/splitfy.user.js is the
                  standalone Tampermonkey panel (installed separately from the extension)
DESIGN.md           long-form design + portability research notes
AGENTS.md         contributing & coding conventions
```

## Quick start

From a checkout:

```bash
python src/splitfy/run.py -i <source-url-or-id> [-i <more>] [--engine auto|uvr5|htdemucs]
# or, if installed via pip install -e . :  splitfy -i <source-url-or-id>
```

Run the test suite (pytest OR unittest):

```bash
python -m pytest                    # uses [tool.pytest.ini_options] pythonpath=["src"]
python -m unittest discover -s tests
```

## HTTP server mode

`server.py` runs a small endpoint for browser userscripts/extension popups: `GET /status` streams
progress JSON, `/items` adds videos to the queue. See `DESIGN.md §11` for the full UX design.

Start it (foreground):  `python -m splitfy.server --port 8765 [--scope .] [--audio-format wav|mp3]`

The extension and Tampermonkey userscript connect from a *cross-origin* context, so the server emits
CORS headers (`Access-Control-Allow-Origin: *`) and answers `OPTIONS` preflight — covered by
`test_server.py::ServerIntegrationTests::test_cors_headers_on_responses_and_preflight`.

For always-on / reboot-persistent service use per-platform wrappers (they resolve tools from the repo,
so no paths are baked into source). See `DESIGN.md §12`:

* **Windows** — foreground: `windows\start-splitfy-server.bat [PORT]`; persistent: run
  `powershell windows\install-schedule-task.ps1` (and `uninstall-schedule-task.ps1` to remove).
* **Linux**   — systemd unit `linux\splitfy-server.service`, driven by `linux\splitfy-server-launch.sh`
  (`start|stop|status|restart [PORT]`). See `DESIGN.md §14`; edit the absolute paths in that `.service` file first.

## Notes on portability

The tool is designed to "just work" on any machine. `provision.py` self-heals a private
`.portable/<os>/` scope: it reuses already-installed tools first and only downloads what is missing
(static ffmpeg+ffprobe build, ONNX Runtime + the Demucs weights). See `DESIGN.md §10` for details.
