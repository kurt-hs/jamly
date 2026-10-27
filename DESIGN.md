# Design — audio-separation pipeline

Input URL / partial URL → highest-quality audio (`yt-dlp`) → torch `demucs` four-stem separation →
two finished outputs plus a `.meta.json`. See `README.md` for usage and `AGENTS.md` for contributing
conventions. It records the design, decisions, and machine-specific findings below.

## Overview (§0)

The Python package lives under `src/jamly/` (stdlib-only; heavy imports such as torch are lazy):
`normalize.py` (URL→watch-id), `core.py` (engine availability check + command builders + stem run),
`bootstrap.py` (reuse-first dependency detection for python / yt-dlp / ffmpeg), `watch_queue.py`
(`WatchQueue`: resumable queue with a persisted done-set), `run.py` (CLI orchestrator) and
`server.py` (streaming `/status` SSE status server). Binary discovery is a cache-aware probe: PATH
first, then a curated list of well-known app locations (`_curated_app_roots`) plus `.portable/<os>/`,
made robust on hosts where a git-bash-style env can make `os.walk` yield odd-shaped tuples.

Test suite is green under **both** runners (`python -m unittest discover -s tests` and `pytest`). The
exact test count grows as features are added, so it isn't pinned in this doc; see AGENTS.md for how the
suite stays portable under either runner.

### Developer experience — VSCode config / extensions (§9)

`.vscode/` scaffolding gives contributors linting + type-checking + pytest-debug out of the box:
`.vscode/extensions.json` (Python, Pylance, `pytest-test-selector`, a formatter such as black/ruff,
code-spell-checker, remote-ssh), `.vscode/settings.json` (pyright `basic`, format-on-save via black,
ASCII-only source), and `.vscode/launch.json` (modern schema: top-level object with default
`pytest-debug` task + named per-task tasks each tagged with a unique `taskSig`; debug tasks set
`PYTHONPATH=<root>/src` so no editable install is needed). Dropped the interpreter-pinned
`.code-workspace` at user request. The AGENTS.md / README.md references are in place; there is no CI
host, so lint/type-check are enforced locally via the VSCode dev-config rather than GitHub Actions.

### Friendly run-time output (§14)

A terse/friendly per-stage log is emitted through the module logger and unit-tested via
`tests/test_logging.py`. Per raw input it produces: one line flagged `(blank)`/`(invalid)`, a dedup
summary; a tools report naming each binary's version and install type/location (e.g. `[portable]
portable`, `[app] Shotcut`) or warning when absent; an engine availability line; a streamed live
download bar (`parse_download_progress`/`render_bar`); and clickable output links with a Windows "click
here to open" hint. Symbols + optional ANSI color are emitted through the module logger, forced UTF-8
on startup so block glyphs survive legacy cp1252 consoles.

## Goal (§1)

Given a YouTube link / partial URL / watch ID, download the highest-quality audio with `yt-dlp`, split
it into four stems via the torch-based `demucs` CLI (`python -m demucs separate --audio … --out <dir> -n
htdemucs`), and emit two finished files — **drums** (`<title>_drums.wav`) and **no-drums** (bass + other
+ vocals, mixed into `<title>_no_drums.wav`). The `demucs` package fetches its own weights on demand; no
local install of PyTorch, ONNX Runtime, or UVR5 is required. Python orchestrates yt-dlp + separation; the
whole thing runs in a private `.portable/` scope so nothing needs to be installed globally.

## Environment findings on this machine (§2)

> Findings for **this box**; the portable design (`§7`) does not depend on them — `bootstrap.py`
> self-provisions missing tools into `.portable/`. Useful as ground truth for local testing.

| Item | Finding |
|------|---------|
| System Python | `%USERPROFILE%\anaconda3\python.exe` (v3.13) — no yt-dlp / audio-separator / torch here |
| `demucs` package | Not installed on this box; enabling four-stem separation needs `pip install demucs` (it fetches its own weights). |
| yt-dlp binary | Standalone exe at `%USERPROFILE%\AppData\Roaming\yt-dlp_win_x86\yt-dlp`. Custom-build quirks: `--no-subs` is invalid → omit it; `-x/--extract-audio` needs an ffmpeg/ffprobe location and this build is intermittent about detecting them (only `ffmpeg.exe` present). Verify success exit code + output path before relying on it. |
| ffmpeg / ffprobe | A directory with only `ffmpeg.exe` (no `ffprobe`) is NOT usable by this tool — the "needs BOTH" rule holds. Prefer a static build shipping both into `.portable/`; reuse only when the host matches. |

> The shared extractor defaults to injecting `--no-subs`, and this box's custom yt-dlp rejects it, so
> local CLI runs must set `AUTOCV_SUPPRESS_SUBS=0` (env escape-hatch; default None keeps every other
> host identical). Provisioning ffmpeg for macOS is unsupported upstream — no static build ships, so that
> platform falls back to PATH detection / system ffmpeg.

## Single-engine route decision (§3)

There is exactly **one** separation engine: the torch-based `demucs` PyPI CLI (`python -m demucs
separate`). There is no UVR5 / audio-separator reuse and no ONNX Runtime `.onnx` path — those were never
part of this design, and nothing falls back to them if the package is missing.

- **`--engine auto`** (default) resolves by availability: it runs whenever `demucs` is importable from
  site-packages; otherwise separation is not possible rather than falling back to another engine.
- **`--engine htdemucs`** selects the same torch CLI explicitly (`-n htdemucs`). It exists so callers can
  name the single path, but there is no second route — both options drive one binary.

The four stems are emitted as `drums`, `bass`, `other`, `vocals`; two of them (bass + other + vocals) feed
the ffmpeg mix into the **no-drums** file while the drum stem is kept verbatim. See §5 for the command and
§6 for naming.

## Architecture (§4)

```
User / ext  --> run.py (validate+normalize) --> .portable/ scope (python, yt-dlp, ffmpeg+ffprobe BOTH)
Flow: normalize watch ID -> yt-dlp best-audio download -> torch demucs four-stem separation (-n htdemucs) -> ffmpeg mix of bass+other+vocals -> tag/rename + .meta.json
run.py self-provisions .portable/ on first run and reuses tools already installed. The demucs package is
installed separately via `pip install demucs` (it fetches its own weights).
```

### Command shapes (§5)

**yt-dlp** (native lossless container; pass it straight through, no re-encode):

```
yt-dlp <url> --extract-audio --audio-format best --audio-quality 0 \
    --ffmpeg-location <dir-with-ffmpeg.exe-and-ffprobe.exe> -o '%(title)s.%(ext)s'
```

**Demucs four-stems** (torch `demucs` CLI; no `--four-stems` flag, source count comes from the model):

```
python -m demucs separate --audio <audio> --out <workdir> -n htdemucs [-j <threads>]
```

Writes `<workdir>/separated/htdemucs/{drums,bass,other,vocals}.wav` — exactly what `core._locate_stems`
walks. Requires `pip install demucs`.

**ffmpeg mix of non-drum stems:**

```
ffmpeg -nostdin -y -i bass.wav -i other.wav -i vocals.wav \
    -shortest -map 0? -mixer_channels 2 <workdir>/no_drums.wav
```

### Output naming (§6)

All outputs for one item share the **parsed title** as their common prefix, with a `.meta.json` sidecar
keyed by the bare watch id so every file traces back to a single record:

- extract:  `<workdir>/<title>.<ext>`           (build_download_cmd)
- drums:    `<output_dir>/<title>_drums.<fmt>`   (+ `<watch_id>.meta.json`, named but not written by code)
- no-drums: `<output_dir>/<title>_no_drums.<fmt>`

The id is URL-safe, unique per video, and known before extraction — a stable filename basis on every
platform (no title to rely on yet; no unicode/special chars; no truncation). The real title (`res.title`)
stays a display/logging name only. The **no-drums mix excludes the drum stem by construction**: ffmpeg
blends bass+other+vocals into `nodrums_out`, never mixing in `<title>_drums.<fmt>`. Output format
(`--audio-format`) defaults to lossy MP3; raw WAV is verbatim (copy, no encoder).

## Durable / portable scope (§7)

A single folder that self-heals on any host: reuse what is installed and download & install the rest into
its own private `.portable/` scope so it "just works" anywhere (Windows primary; macOS/Linux/ARM as
stretch). No global installs required.

- `bootstrap.py`: detect/reuse each dependency into `<project>/.portable/<os>/` — Python, yt-dlp, and a
  **static ffmpeg+ffprobe build that ships BOTH binaries**. Reuse-first: every check hits PATH/existing
  installs first; only missing pieces self-provision.
- Everything needed lives under `.portable/` (gitignored) — no pollution of the system or any host install.
  The `demucs` package and its weights are fetched separately by `pip install demucs`. Network access is a
  deliberate capability; some videos may need yt-dlp cookies/oauth (`cookies.txt`).

### On-disk state files (§7)

Three small JSON files are the only persistent on-disk state; all three live at project root and are gitignored:

- **`.portable/<os>/...`** — private tooling scope. Holds reused or self-provisioned binaries (yt-dlp standalone + a static ffmpeg+ffprobe build that ships BOTH, plus `<os_family>/ffmpeg` scoped fallbacks). Python is NOT stored here; it is resolved from PATH/system first.
- **`splitfy_queue.json`** — persistent input queue. Schema `{items:[{watch_id,title,status,added_at}], done:[...], updated_at}`; `done` keeps completed/failed ids for history + dedup. Shared by the CLI and HTTP server through the same JSON protocol (override via env `AUTOCV_QUEUE` or the `--queue` flag). WatchQueue loads an empty queue if the file is missing or invalid, so it never blocks a run.
- **`splitfy.json`** — **does not exist.** The ConfigStore/splitfy.json persistence design was removed during portabilization; there is no persisted binary/override cache. Tool resolution (python / yt-dlp / ffmpeg) is live detection on every launch (`bootstrap.py` + `core.py`), so nothing here can drift out of sync.

## Binary discovery (§8)

### Curated known-install roots (PATH-first, then curated)

1. **Curated app locations** (`core.py` / `bootstrap.py`, pure stdlib): probe PATH first, then a curated
   list of well-known app folders — `%APPDATA%\yt-dlp_win_x86`, `AppData\Local\Programs\{Shotcut,DaVinci
   Resolve,OBS Studio,Blender,Kdenlive,Avidemux,HandBrake,PotPlayer}` (apps that ship ffmpeg/ffprobe), plus
   `.portable/<os>/`. This is a *best-effort* probe; it does not need to find every copy on this box — it
   needs to cover the common cases cross-machine.
2. **FFmpeg nuance**: a directory with only `ffmpeg.exe` (no ffprobe) is usable for some yt-dlp builds but
   not this one — keep the "needs BOTH" rule, and treat third-party shared ffmpeg as *local-reuse-only* (not
   portable). Open question: whether to also detect a system-wide ffmpeg/ffprobe via `%PATH%` on Windows.

### Design

1. **Reuse-first**: `bootstrap.py` checks PATH + known roots first; only when absent (or `--provision`) does
   it download into `.portable/<os>/`: the official yt-dlp standalone, and a static ffmpeg+ffprobe build. The
   script then points yt-dlp's `--ffmpeg-location` at the discovered dir.
2. **No persisted config**: tool resolution is live detection on every launch; there is no `splitfy.json`
   cache to keep in sync (see AGENTS.md).

> Discovery falls back to the PATH/app scan when nothing reusable is found, and reuses a recorded ffmpeg/ffprobe
> dir only if it still exists on disk — that reuse fast-path keeps common multi-launch runs cheap. See the probe/cache
> coverage under `tests/test_bootstrap.py`.

## §9 — Developer experience: VSCode config / extensions

See **§0** (Developer experience — VSCode config / extensions) above; this heading is retained for
cross-reference from README.md and AGENTS.md. `.vscode/` scaffolding gives contributors linting +
type-checking + pytest-debug out of the box, debug tasks set `PYTHONPATH=<root>/src`, and ruff rules are
kept in parity with a project-level `[tool.ruff]`.

## §13 — Browser userscript & background-extension

Both clients build on the shared contract the server already emits: a `/status` SSE stream with per-item
terminal records (`last_completed`) and `pending`/`done` counts, plus the `watch_queue` queue-file schema.

- **Userscript** — `browser/userscripts/jamly.user.js` (Tampermonkey/Greasemonkey manifest):
  `GM_addStyle` UI, `/status` polling via fetch/XHR fallback, read/write of the local queue JSON, and
  progress + completion alerts with download links. Runs alongside `python -m jamly.server`.
- **Background-extension variant** — MV3 extension in `browser/extension/` (`manifest.json`, a service
  worker that imports the shared DOM-free client via `importScripts`, plus popup/options HTML). Shares one
  protocol with the userscript (`GET /status` SSE + `POST /add`); a small server writes to the same queue
  file so users can enqueue from an always-on background page instead of the CLI process.
- **Shared contract** — both clients use the identical `/status` JSON shape and `watch_queue` schema;
  the shared client (`browser/extension/splitfy_client.js`) is DOM-free, so it ships as a static asset in
  the extension rather than as a `jamly` module. A contract test drives the parsing logic directly;
  streamed-path tests use bounded reads so they stay deterministic under both runners.
- Read-only to project scope (never writes outside `.portable/` or the queue file); alerts on completion
  with download links.

The browser source lives under `browser/extension/` and `browser/userscripts/` — it is **not** part of the
Python package, tests, or `src/jamly/`, and its manifest resolves all references relative to its own
folder (so no path edits are needed when files move together).

## §14 — Cross-platform server delivery

The HTTP listener runs as a foreground process:  `python -m jamly.server --port 8765`.
There is **no** auto-start mechanism and the installed `jamly` console script drives batch/queue mode,
not the listener (see `src/jamly/server.py`). To make it always-on / reboot-persistent across platforms,
wrappers resolve tools from the repo root (no baked-in paths):

- **CORS** — every response carries `Access-Control-Allow-Origin: *` and an `OPTIONS` preflight is answered, so
  the MV3 extension and Tampermonkey userscript can fetch over HTTP from their cross-origin contexts. Covered by
  `test_server.py::ServerIntegrationTests::test_cors_headers_on_responses_and_preflight`.
- **Windows** — foreground: `scripts/windows/start-jamly-server.bat [PORT]`; persistent: `powershell scripts/windows/install-schedule-task.ps1`
  (and `uninstall-schedule-task.ps1` to remove). The task runs from the repo root so `import jamly` needs no install.
- **Linux** — systemd unit `scripts/linux/jamly-server.service` driven by `scripts/linux/jamly-server-launch.sh` (`start|stop|status|restart [PORT]`).
  Edit the absolute paths in that `.service` file (`WorkingDirectory`, `ExecStart`) to point at your checkout before enabling.
