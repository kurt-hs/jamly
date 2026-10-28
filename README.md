# jamly

Jamly downloads one audio source supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp),
separates it with Demucs, and writes:

- `<title> [<id>]_drums.<format>`
- `<title> [<id>]_no_drums.<format>`
- `<id>.meta.json`

The no-drums track is the bass, vocals, and other stems mixed together. The stable id is retained
in named outputs so two uploads with the same title cannot overwrite each other.

## Requirements

- Python 3.11 or 3.12.
- `ffmpeg` and `ffprobe` on `PATH`.
- Enough disk space for PyTorch and the Demucs model. The model downloads on first use.

Jamly declares yt-dlp and Demucs as Python dependencies; it does not download executables or modify
system packages itself.

## Install

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/kurtisnotcurtis/jamly.git
jamly -i "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

From a checkout:

```bash
uv sync --extra test
uv run jamly -i "https://vimeo.com/123456789"
```

You can also use `python -m jamly.run` after installing the package. Running
`python src/jamly/run.py` directly is not supported by the src-layout package.

## CLI

```text
jamly -i URL_OR_ID [-i URL_OR_ID ...]
      [-o OUTPUT_DIR] [--audio-format mp3|wav]
      [--threads N] [--replace-existing]
```

Inputs may be YouTube links/ids or any other source expression accepted by yt-dlp. YouTube URLs
deduplicate by video id; other sources receive a stable hash id while their original URL is retained
in the queue and metadata.

Jamly also reads newline-separated inputs from stdin:

```bash
printf '%s\n' "https://youtu.be/dQw4w9WgXcQ" "ytsearch1:artist song" | jamly
```

Defaults:

- output directory: `./output`
- format: 320 kbps MP3
- model: Demucs `htdemucs`
- queue: `./jamly_queue.json`

Use `--audio-format wav` for float WAV output. A complete existing output set is skipped. A partial
set is treated as an error unless `--replace-existing` is supplied.

## Server and browser clients

Start the local server:

```bash
jamly-server --port 8765
# equivalent:
python -m jamly.server --port 8765
```

Endpoints:

- `GET /status` — continuous SSE status stream.
- `GET /status?once=1` — one SSE event, then close; used by polling browser clients.
- `POST /add` — enqueue `{"inputs": [...], "titles": [...]}`.
- `GET /items` — pending and completed ids.
- `GET /healthz` — health and queue location.

Loopback mode needs no token. Binding to another interface requires
`--server-token SECRET` or `JAMLY_SERVER_TOKEN=SECRET`; browser clients must configure the same
token.

### WebExtension

Load `browser/extension/` as an unpacked Manifest V3 extension. Its options page configures the
server URL and optional token. Localhost permissions are included; choosing another server asks for
that origin only.

### Userscript

Install `browser/userscripts/jamly.user.js` in Tampermonkey or Violentmonkey. It displays status on
YouTube and YouTube Music pages. Edit `SERVER_URL` and `SERVER_TOKEN` near the top when not using the
default loopback server.

## Background service helpers

- Windows foreground: `scripts\windows\start-jamly-server.bat [PORT]`
- Windows startup task:
  `powershell -ExecutionPolicy Bypass scripts\windows\install-schedule-task.ps1`
- Linux foreground: `scripts/linux/jamly-server-launch.sh foreground [PORT]`
- Linux user service: `scripts/linux/jamly-server-launch.sh start [PORT]`

The Linux launcher generates `~/.config/systemd/user/jamly-server.service` with the actual checkout
and Python paths.

## Development

```bash
uv sync --extra test
uv run pytest
uv run python -m unittest discover -s tests
uv run ruff check .
node browser/extension/jamly_client_test.js
node browser/extension/jamly_extension_bg_test.js
uv build
```

The unit tests mock downloads and inference. A real run additionally validates the installed ffmpeg,
yt-dlp, PyTorch, model download, and available CPU/GPU backend.

Only download content you are authorized to use and follow the source site's terms.
