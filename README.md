# splitfy

A small, portable **audio-separation** tool. Give it anything `yt-dlp` can fetch — a full URL,
partial path, or bare id from any supported host (YouTube included) — and it downloads the
highest-quality available audio and splits it into its four stems:

```
drums.wav  – drums only
bass.wav   – bass only
other.wav  – everything else but drums & bass
vocals.wav – vocals only
```

Separation uses the **torch-based `demucs` PyPI CLI** (`python -m demucs separate`, model
`htdemucs`). It has no bundled weights: on first use it fetches its own package (and, if needed,
the model) via `pip install demucs`. There is a single engine only — no UVR5 / audio-separator
reuse and no reused `.th` weights. Heavy imports stay lazy, so importing this tool never pulls
PyTorch; availability checks are import-free.

## How it works

1. Normalize each input (`normalize.py`) to a short, stable id and keep the original source text,
   so any host `yt-dlp` understands resolves.
2. `yt-dlp` extracts the highest-quality audio in its native lossless container — passed straight
   through to separation with no re-encode.
3. The torch `demucs` CLI separates it into four stems (`drums`, `bass`, `other`, `vocals`). Two of
   them are consumed by ffmpeg: bass + other + vocals are mixed (2 channels) into the **no-drums**
   file, while the drum stem is kept as-is.
4. Outputs land next to each other, named after the parsed title with a `.meta.json` sidecar keyed
   by the bare watch id — see `DESIGN.md §7`.

## Layout

```
src/splitfy/
  normalize.py    any-source input normalization: extract a stable id, validate + dedup
  core.py         engine availability check (single torch demucs CLI), command builders, stem run
  bootstrap.py    dependency/tool detection (python, yt-dlp, ffmpeg) -- reuse-first, no provisioning
  watch_queue.py  persistent, resumable input queue with a persisted done-set (JSON-backed)
  run.py          CLI orchestrator: download -> separate -> mix/metadata/renaming
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
python src/splitfy/run.py -i <source-url-or-id> [-i <more>] [--engine auto|htdemucs]
# or, if installed via pip install -e . :  splitfy -i <source-url-or-id>
```

Run the test suite (pytest OR unittest):

```bash
python -m pytest                    # uses [tool.pytest.ini_options] pythonpath=["src"]
python -m unittest discover -s tests
```

## HTTP server mode

`server.py` runs a small endpoint for browser userscripts/extension popups: `GET /status` streams
progress JSON, `/items` adds videos to the queue. See `DESIGN.md §13` for the shared contract and
`§14` for the full UX design.

Start it (foreground):  `python -m splitfy.server --port 8765 [--scope .] [--audio-format wav|mp3]`

The extension and Tampermonkey userscript connect from a *cross-origin* context, so the server emits
CORS headers (`Access-Control-Allow-Origin: *`) and answers `OPTIONS` preflight — covered by
`test_server.py::ServerIntegrationTests::test_cors_headers_on_responses_and_preflight`.

For always-on / reboot-persistent service use per-platform wrappers (they resolve tools from the repo,
so no paths are baked into source). See `DESIGN.md §14`:

* **Windows** — foreground: `scripts/windows\start-splitfy-server.bat [PORT]`; persistent: run
  `powershell scripts/windows/install-schedule-task.ps1` (and `uninstall-schedule-task.ps1` to remove).
* **Linux**   — systemd unit `scripts/linux\splitfy-server.service`, driven by `scripts/linux/splitfy-server-launch.sh`
  (`start|stop|status|restart [PORT]`). See `DESIGN.md §14`; edit the absolute paths in that `.service` file first.

## Notes on portability

The tool is designed to "just work" on any machine. It self-heals a private `.portable/<os>/`
scope: it reuses already-installed tools first and only downloads what is missing (the official per-OS
standalone yt-dlp build, and a static ffmpeg+ffprobe build that ships **both** binaries). The `demucs`
package and its weights are fetched separately by `pip install demucs`. See `DESIGN.md §10` for details.
