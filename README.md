# Jamly

Create **drums** and **drumless** tracks from an individual media URL supported by yt-dlp.
Separation runs locally with Demucs `htdemucs`. Estimates can contain bleed or artifacts;
"drumless" is not a guarantee of perfect removal.

## Install

Requirements: [uv](https://docs.astral.sh/uv/getting-started/installation/) and ffmpeg/ffprobe.
Python dependencies, including Demucs, yt-dlp and Deno, are installed together.

Install ffmpeg using your platform's package manager:

```text
macOS:          brew install ffmpeg
Debian/Ubuntu:  sudo apt install ffmpeg
Windows:       winget install Gyan.FFmpeg
```

Open a new terminal after changing Windows PATH, then:

```sh
uv tool install --python 3.12 git+https://github.com/kurtisnotcurtis/jamly.git
jamly-doctor
```

Python 3.11 and 3.12 are the supported/tested versions for this release. PyTorch can require
several GB of disk space on Linux. Model weights download on the first separation.

## Use the CLI

```sh
jamly -i "https://youtu.be/VIDEO_ID"
jamly -i URL -i ANOTHER_URL --audio-format wav
jamly -i URL --replace-existing
```

Newline-separated inputs can also be piped to `jamly`. Use `-i=-VIDEO_ID` for a bare id starting
with a dash. Playlists, search results, multi-item pages, and live/upcoming streams are rejected;
submit individual completed recordings instead.
Cookie/account integration is not provided; media that requires authentication may fail.

Defaults:

| Setting | Default |
|---|---|
| Output | `~/Music/jamly` (`-o DIR` overrides it) |
| Format | 320 kbps MP3; `--audio-format wav` writes float32 WAV |
| Model | `htdemucs` |
| Device | Automatic CUDA / MPS / CPU; `--device cpu` forces CPU |
| Parallel jobs | `0`; `--threads N` increases memory use |
| State and queue | Per-user application data; `--state-dir DIR` or `--queue FILE` overrides it |

Each track/format is published as one directory:

```text
~/Music/jamly/<id>/wav/
  Artist - Song [id]_drums.wav
  Artist - Song [id]_no_drums.wav
  metadata.json
```

Names fall back to the stable input id when title metadata is unavailable. Both tracks share one
gain, applied only to prevent sample clipping. MP3 output is checked after decoding with 1 dB headroom.
Metadata records the source, model, gain, audio dimensions, and output checksums.

A complete, checksum-valid export is reused without downloading or separating again. Partial,
modified, or older exports require `--replace-existing`. Resubmitting a completed id is allowed,
including with another output directory or format. Run `jamly` without new inputs to resume pending
work and retry failed CLI jobs once.

Only one process may own a queue. While the backend owns it, submit jobs through the browser/API,
or stop the backend before running the standalone CLI against that queue.

## Browser extension and backend

The browser component **requires the local backend**. No marketplace installation or native desktop
installer is provided yet.

1. Start `jamly-server` in a terminal.
2. Run `jamly-service browser`. It copies the packaged extension to a stable per-user directory and
   prints its path.
3. In Chrome/Edge's extensions page, enable Developer mode, select **Load unpacked**, and choose that
   directory.
4. Run `jamly-service token`. In the extension's **Pair / settings** page, paste the token and save.
5. Use **Use current page**, or paste individual URLs, then **Split drums**. The popup shows stages,
   failures, retry controls, and authenticated result downloads.

The default backend URL is `http://127.0.0.1:8765`. A saved token is required even on loopback.
Keep it private. When using a custom state directory, pass the same `--state-dir DIR` to the server
and service commands.

The extension polls only while its popup is open; closing the popup does not stop backend jobs.
It requests access to the active tab only when invoked. It does not modify every website.

### Start automatically at login

```sh
jamly-service install
jamly-service status
jamly-service stop
jamly-service start
jamly-service restart
jamly-service uninstall
```

Installation creates a per-user systemd unit on Linux, a LaunchAgent on macOS, or a logon task on
Windows. All use the installed Jamly interpreter, absolute configuration paths and the detected
ffmpeg directory. Use `install --dry-run` to inspect definitions without registering anything.
Linux requires a working user systemd session. Windows registration uses your account, not an
administrator/system service.

Set service options during installation, for example:

```sh
jamly-service install --audio-format wav --output-dir "/path/to/music" --port 8765
```

Uninstalling the service keeps the queue, token and exports. The small files under `scripts/` only
delegate to installed commands; a checkout is not required.

### Optional userscript

`jamly-service userscript` exports a small YouTube/YouTube Music helper for Tampermonkey or
Violentmonkey. Use its **Pair Jamly** menu to set the token. It shows status and enqueues the current
page; use the extension for downloads and retries. It connects only to the default local backend.

## Update and recover

```sh
uv tool upgrade jamly-audio-sep
jamly-doctor
jamly-service browser
jamly-service restart
```

Reload the unpacked extension after exporting its updated files. Backend and browser API versions
must match. yt-dlp/YouTube changes may require upgrading; downloaded model files are cached separately.

Existing flat exports are not deleted or moved. To reuse an older queue file, select it explicitly
with `--queue /path/to/jamly_queue.json`. Failed server jobs remain failed until explicitly retried;
jobs interrupted by shutdown are resumed on restart.

## HTTP API

All endpoints except the minimal `/healthz` probe require `Authorization: Bearer <token>`.

| Method and path | Purpose |
|---|---|
| `GET /healthz` | API version and backend health; no local paths or token |
| `GET /status` or `/items` | JSON counts, current stage, and jobs with errors/result URLs |
| `POST /add` | `{"inputs":["URL"],"replace_existing":false}` |
| `POST /retry/<id>` | `{"replace_existing":false}` |
| `GET /outputs/<id>/<kind>?generation=...` | Download a completed generation; use URLs from `/status` |

Kinds are `drums`, `no_drums`, and `metadata`. A superseded or in-progress output returns 409 instead
of mixing generations. Unrecognized payload fields and invalid JSON are rejected.

This is a local service, not an internet-facing deployment. Browser origins are restricted to
extensions or explicit `--allow-origin` entries; arbitrary Host headers are rejected. Non-loopback
binding requires an explicit token. Remote use additionally needs appropriate host/origin settings
and an authenticated TLS deployment, which this project does not configure.

## Development

Install ffmpeg/ffprobe and Node 24, then from a checkout:

```sh
uv sync --locked
uv run --locked pytest
uv run --locked python -m unittest discover -s tests
uv run --locked ruff check .
node --test browser/extension/jamly_client_test.js browser/extension/jamly_extension_bg_test.js browser/extension/jamly_popup_test.js
uv build
```

Run the real-model integration check explicitly (it downloads model weights on first use):

```sh
uv run --locked python -m unittest discover -s tests -p integration_pipeline.py
```

Use `uv run --locked jamly-server` and `uv run --locked jamly-service ...` when working from a checkout.
See [DESIGN.md](DESIGN.md) for the current data and lifecycle contracts.

Only download media you are authorized to use and follow the source site's terms.
