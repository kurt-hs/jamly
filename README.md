# Jamly

Create **drums** and **drumless** tracks from an individual media URL supported by yt-dlp.
Separation runs locally with a choice of two Demucs presets. Estimates can contain bleed or artifacts;
"drumless" is not a guarantee of perfect removal.

## Desktop companion (preview)

The companion is the main interface; the browser extension is optional. Desktop archives include
Python, Demucs, yt-dlp, Deno, ffmpeg and ffprobe. You do not need to install those separately.

The **Companion packages** GitHub Actions workflow produces unsigned preview artifacts for macOS
Apple Silicon and Intel, Windows x64, and Linux x64 (glibc 2.35+, such as Ubuntu 22.04 or newer).
Use artifacts from a successful run of the commit you intend to try. These are portable packages,
not signed installers or a notarized public release; your OS may warn about or refuse unsigned apps.

| Platform | Open the companion |
|---|---|
| macOS 13+ | Extract the zip, move `Jamly.app` to Applications, then open it |
| Windows | Extract the **whole** zip and open `Jamly.exe`; keep its companion files together |
| Linux | Extract the tar.gz and run `Jamly` inside the extracted folder |

Jamly opens a local browser page and connects it automatically. Paste individual track URLs,
choose a preset/format and output folder, then select **Split drums**. **Show files** opens the
completed export; **Save a copy** uses the browser's download location. Failures and retries are
shown in the job list. A track's job shows its latest submission; earlier model/format exports remain on disk.

Closing the page does not stop jobs. Reopening the application reconnects to the existing backend.
Use **Quit companion** to stop it; unfinished work resumes on the next launch.
No account or extension is needed. This release exports files; playback and browser-only inference
are out of scope.

The application and models can use several GB of disk space. Internet access is needed to retrieve
media and, on first use of each preset, model weights. Inference stays on your computer.

## CLI / source installation

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
jamly-companion
```

Python 3.11 and 3.12 are supported. `jamly-companion` launches the same interface as the desktop
packages. `jamly-server` runs just the backend. The standalone CLI remains available below.
On Intel macOS, use the checkout/development instructions below and install Rust to build sphn;
that workflow applies the required CMake build constraint. The desktop package avoids these requirements.

## Separation presets

| Preset | Model / CLI flag | Tradeoff |
|---|---|---|
| **Balanced** (default) | `--model htdemucs` | Good quality with faster processing |
| **Higher quality** | `--model htdemucs_ft` | Fine-tuned estimates; about **4x longer**, with more weights downloaded on first use |

Higher quality may improve separation, but the improvement varies by track; it is not a guarantee of
perfect isolation. The same explanation appears beside the preset selector and in its tooltip.
These are the official Demucs models described [upstream](https://github.com/adefossez/demucs#models).
Weights are downloaded on demand, not included in the desktop archives.

## Use the CLI

```sh
jamly -i "https://youtu.be/VIDEO_ID"
jamly -i URL -i ANOTHER_URL --audio-format wav
jamly -i URL --replace-existing
jamly -i URL --model htdemucs_ft
```

Newline-separated inputs can also be piped to `jamly`. Use `-i=-VIDEO_ID` for a bare id starting
with a dash. Playlists, search results, multi-item pages, and live/upcoming streams are rejected;
submit individual completed recordings instead.
Cookie/account integration is not provided; media that requires authentication may fail.

Standalone CLI defaults (independent of saved companion defaults):

| Setting | Default |
|---|---|
| Output | `~/Music/jamly` (`-o DIR` overrides it) |
| Format | 320 kbps MP3; `--audio-format wav` writes float32 WAV |
| Preset | Balanced (`htdemucs`); `--model htdemucs_ft` selects Higher quality |
| Device | Automatic CUDA / MPS / CPU; `--device cpu` forces CPU |
| Parallel jobs | `0`; `--threads N` increases memory use |
| State and queue | Per-user application data; `--state-dir DIR` or `--queue FILE` overrides it |

Each track/model/format is published as one directory:

```text
~/Music/jamly/<id>/htdemucs/wav/
  Artist - Song [id]_drums.wav
  Artist - Song [id]_no_drums.wav
  metadata.json
```

Names fall back to the stable input id when title metadata is unavailable. Both tracks share one
gain, applied only to prevent sample clipping. MP3 output is checked after decoding with 1 dB headroom.
Metadata records the source, model, gain, audio dimensions, and output checksums.
Trying the other preset does not overwrite or reuse the first preset's export.

A complete, checksum-valid export is reused without downloading or separating again. Partial,
modified, or older exports require `--replace-existing`. Resubmitting a completed id is allowed,
including with another preset, output directory or format. Run `jamly` without new inputs to resume pending
work and retry failed CLI jobs once.

Only one process may own a queue. While the backend owns it, submit jobs through the browser/API,
or stop the backend before running the standalone CLI against that queue.

## Optional browser extension

The extension sends the current page to the companion; it is not required for pasting URLs or
retrieving results. No marketplace listing is provided yet.

1. In the companion, expand **Optional Chrome / Edge extension** and select **Prepare extension**.
2. In Chrome/Edge's extensions page, enable Developer mode, select **Load unpacked**, and choose the
   displayed directory.
3. In the extension's **Pair / settings** page, use the displayed backend URL and private token.
4. Use **Use current page**, or paste individual URLs, choose a preset, then **Split drums**. The popup shows stages,
   failures, retry controls, and authenticated result downloads.

For a CLI-only installation, start `jamly-server`, then use `jamly-service browser` and
`jamly-service token` to obtain the same directory and token. The default backend URL is
`http://127.0.0.1:8765`. Pairing is required even on loopback. Keep the token private.
When using a custom state directory, pass the same `--state-dir DIR` to the server and service commands.

The extension polls only while its popup is open; closing the popup does not stop backend jobs.
It requests access to the active tab only when invoked. It does not modify every website.

### Advanced: start a CLI installation at login

Desktop packages do not register a login service automatically.

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

For a desktop package, **Quit companion**, replace the entire extracted application with the newer
package, and reopen it. Updates are manual. State, settings and exports live outside the application
and are preserved. An older login service must be stopped before launching a different backend version.

For a CLI installation:

```sh
uv tool upgrade jamly-audio-sep
jamly-doctor
jamly-service browser
jamly-service restart
```

Reload the unpacked extension after exporting its updated files. Backend and browser API versions
must match. yt-dlp/YouTube changes may require upgrading; downloaded model files are cached separately.

Older flat or track/format exports are not deleted or moved, but are not automatically reused in
the new model-specific cache. To reuse an older queue file, select it explicitly
with `--queue /path/to/jamly_queue.json`. Failed server jobs remain failed until explicitly retried;
jobs interrupted by shutdown are resumed on restart.

## HTTP API

API version **3** requires matching backend and browser components. The health probe, static companion
assets and loopback-only `/handshake` challenge are public; none exposes a token or local path.

Job, settings and artifact API calls require `Authorization: Bearer <token>`.

| Method and path | Purpose |
|---|---|
| `GET /healthz` | API version and backend health; no local paths or token |
| `GET /status` or `/items` | JSON counts, current stage, and jobs with errors/result URLs |
| `POST /add` | `{"inputs":["URL"],"model":"htdemucs_ft","audio_format":"wav","replace_existing":false}`; model/format are optional |
| `POST /settings` | Save companion defaults: `model`, `audio_format`, `output_dir` |
| `POST /retry/<id>` | `{"replace_existing":false}` |
| `GET /outputs/<id>/<kind>?generation=...` | Download a completed generation; use URLs from `/status` |

Kinds are `drums`, `no_drums`, and `metadata`. A superseded or in-progress output returns 409 instead
of mixing generations. Unrecognized payload fields and invalid JSON are rejected.
Saved companion defaults override service-install configuration; explicit backend command-line flags
override saved defaults at startup. Changes apply to new submissions, not queued jobs. Runtime flags
such as device, threads and port take effect when starting a backend, not when reconnecting to one.

This is a local service, not an internet-facing deployment. Browser origins are restricted to the
same-origin companion, extensions or explicit `--allow-origin` entries; arbitrary Host headers are rejected. Non-loopback
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
node tools/browser_smoke.mjs
```

The browser check uses an isolated Chrome profile and generated audio, including a real Higher quality
export and native downloads. Set `CHROME_BIN` if Chrome is not in its usual location.

Use `uv run --locked jamly-companion`, `jamly-server`, or `jamly-service ...` from a checkout.

### Build desktop packages

Build on the target OS with uv-managed Python 3.12, whose redistribution notices are collected
automatically. Native prerequisites are a C/C++ toolchain, Make, Perl,
Bash and pkg-config. On macOS, use Xcode Command Line Tools and `brew install pkg-config`.
On Linux, install `build-essential pkg-config perl`. On Windows, use an MSYS2 **MINGW64** terminal
with `make perl mingw-w64-x86_64-gcc mingw-w64-x86_64-pkgconf`.
Intel macOS also needs Rust. The project pins CMake 3 for sphn's bundled Opus build.

```sh
export UV_PROJECT_ENVIRONMENT=.venv-bundle
uv sync --locked --group bundle --managed-python --python 3.12
uv run --no-sync python tools/build_companion.py
uv run --no-sync python tools/bundle_smoke.py --real-models
```

Artifacts are written to `dist/companions`. Distribute the matching native-source archive and checksum
file alongside each application archive. The application includes dependency notices and a build
inventory; bundled components retain their own licenses. Model weights are not bundled.
Builds are unsigned; signing/notarization and publication require separate release credentials.

See [DESIGN.md](DESIGN.md) for packaging, authentication, and data/lifecycle contracts.

Only download media you are authorized to use and follow the source site's terms.
