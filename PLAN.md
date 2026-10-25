# Technical Plan — audio-separation pipeline

YouTube link → highest-quality audio (yt-dlp) → Demucs v4 four-stems (`htdemucs`) → two WAV stems.
See `README.md` for usage and `AGENTS.md` for contributing conventions. This doc is the design +
the findings that shaped it.

## Current state (§0)

The core pipeline + server are implemented under `src/auto_convert/` (package layout, stdlib-only):
`normalize.py` (URL→watch-id), `core.py` (subprocess runners for yt-dlp/Demucs/ffmpeg),
`bootstrap.py` (reuse-first dependency detection), `provision.py` (self-provisioning into
`.portable/`), `_input_queue.py` (`WatchQueue`, resumable + persisted done-set), `run.py` (CLI
orchestrator with queue integration) and `server.py` (streaming `/status` SSE status server). The
§11 discovery implementation complete: cache-aware `find_known_roots()` + new path-backed
`auto_convert/config_state.py` (`ConfigStore`, atomic persistence, empty-if-missing/invalid),
and `tests/test_bootstrap_extra.py` (16 tests covering the probe/cache/persistence contracts). The
probe is made robust on hosts where a git-bash-style env can make `os.walk` yield odd-shaped tuples.

Test suite is green under **both** runners — `python -m unittest discover -s tests` → **120** OK,
`pytest` → **120** passed (added `tests/test_bootstrap_extra.py`).

### Implemented: VSCode dev-config (§12, done)

Added `.vscode/` so contributors get linting + type-checking + pytest-debug out of the box:
`.vscode/extensions.json` (Python, Pylance, pytest-test-selector, black/ruff formatter, code-spell-checker,
remote-ssh), `.vscode/settings.json` (pyright `basic`, format-on-save via black, ASCII-only source),
and `.vscode/launch.json` (modern schema: top-level object with default `pytest-debug` task + named
per-task tasks each tagged with a unique `taskSig`; debug tasks set `PYTHONPATH=<root>/src` so no editable
install is needed). Dropped the interpreter-pinned `.code-workspace` at user request. Remaining §12 work:
wire linting/type-check into CI (GitHub Actions) and reference it from `AGENTS.md`/`README.md`.

### Implemented: friendly run-time output (§14, done)

A terse/friendly per-stage log was added and unit-tested (`src/auto_convert/logging.py`, `core.py`,
`run.py`, `tests/test_logging.py`): one line per raw input with `(blank)`/`(invalid)` flags + a dedup
summary; a tools report naming each binary's version and install type/location (e.g. `[portable] portable`,
`[app] Shotcut`) or warning when absent; an engine line; a streamed live download bar (`parse_download_progress`
/`render_bar`); and clickable output links with a Windows "click here to open" hint. Symbols + optional ANSI
color are emitted through the module logger, forced UTF-8 on startup so block glyphs survive legacy cp1252 consoles.

## Goal (§1)

Given a YouTube link / partial URL / watch ID, download the highest-quality audio with `yt-dlp`,
split it into stems, and emit two files — **drums-only** (`drums.wav`) and **no-drums**
(`no-drums.wav`) = vocals + instruments. Python 3 orchestrates yt-dlp + separation; UVR5 becomes
optional (its models are reused when present) rather than required.

## Route decision (§3, §6)

Chosen: **ROUTE A — Demucs v4 four-stems**. `htdemucs` yields exactly the two target files and is
what UVR5's own "Separate into stems" does under the hood. The alternative (ROUTE B, MDX23C DrumSep)
is a fallback needing stem-merge validation; not used.

### Inference engine options

| Option | Engine / model | Torch? | Portable across machines? | Notes |
| --- | --- | --- | --- | --- |
| **Default (portable)** | ONNX Runtime + `htdemucs` `.onnx` | No | High — pure numpy+onnxruntime; CPU/DirectML/CUDA/CoreML via one provider flag; model auto-downloaded from HF on first use | Same HTDemucs family as UVR5's bundled weight → essentially identical results. Best for "just works" anywhere. |
| Local fast path | audio-separator + torch-CUDA (reuse UVR5 `.th`) | Yes (large install) | Low — needs the model folders + a CUDA venv carried over | GUI-identical quality, fastest on RTX 4070; optional when UVR5 is present. |

Recommendation: default to ONNX Runtime HTDemucs. Provider auto-detected (CUDA preferred, DirectML
fallback). The audio-separator/UVR5 path stays as an explicit local override (`--engine uvr5`).

## Architecture (§4)

```
User / ext  --> run.py (validate+normalize) --> .portable/ scope (python, yt-dlp, ffmpeg+ffprobe, engine)
Flow: normalize watch ID -> yt-dlp best-audio download -> demucs four-stems -> ffmpeg mix -> tag/rename
run.py self-provisions .portable/ on first run and reuses tools already installed.
```

### Command shapes (§7)

**yt-dlp** (native lossless container; pass it straight through, no re-encode):

```
yt-dlp <url> --extract-audio --audio-format best --audio-quality 0 \
    --ffmpeg-location <dir-with-ffmpeg.exe-and-ffprobe.exe> -o '%(title)s.%(ext)s'
```

**Demucs four-stems** (reusing UVR5's bundled weights):

```
python -m audio_separator.demucs.separate \
    --model htdemucs --name 955717e8-8726e21a.th --repo <models/Demucs_Models/v3_v4_repo> \
    --four-stems --input <audio> -o <workdir> [--device cuda] [--threads N]
```

**ffmpeg mix of non-drum stems:**

```
ffmpeg -i bass.wav -i other.wav -i vocals.wav -shortest -mixer_channels 2 <workdir>/no_drums.wav
```

## Environment findings on this machine (§2)

> Findings for **this box**; the portable design (`§10`) does not depend on them — `bootstrap.py`
> self-provisions missing tools into `.portable/`. Useful as ground truth for local testing.

| Item | Finding |
|------|---------|
| System Python | `C:\Users\Kurt\anaconda3\python.exe` (v3.13) — no yt-dlp / audio-separator / torch here |
| yt-dlp binary | Standalone exe at `C:\Users\Kurt\AppData\Roaming\yt-dlp_win_x86\yt-dlp`. Custom-build quirks: `--no-subs` is invalid → omit it; `-x/--extract-audio` needs an ffmpeg/ffprobe location and this build is intermittent about detecting them (only `ffmpeg.exe` present). Verify success exit code + output path before relying on it. |
| UVR5 install | Flat PyInstaller layout — `.pyd` files live at the install root; runtime data in `lib_v5\`, models in `models\`. No bundled python/site-packages. |
| UVR5 models present | `MDX_Net_Models` (incl. DrumSep model), `Demucs_Models\v3_v4_repo` (`htdemucs.yaml` + `.th` weights → model `955717e8`), `VR_Models`. |
| GPU backend | **RTX 4070 SUPER (12GB)** confirmed → CUDA available & preferred. UVR5 also ships torch_directml as fallback. |
| audio-separator | Headless package driving UVR5's Demucs/MDXC models. Recognizes UVR5's bundled `.th` weights — reuse the installed HTDemucs weight directly, no canonical `.pt` re-download. |
| ffmpeg / ffprobe | Bundled at `C:\Users\Kurt\AppData\Local\Programs\Ultimate Vocal Remover\ffmpeg.exe`. No `ffprobe.exe` reachable anywhere; point yt-dlp's `--ffmpeg-location` at a dir with BOTH, or verify exit code + output path on intermittent ffmpeg-only success. |

## Durable / portable scope (§10)

A single folder that self-heals on any host: reuse what is installed, download & install the rest
into its own private `.portable/` scope so it "just works" anywhere (Windows primary; macOS/Linux/ARM as stretch). No global installs required.

- `bootstrap.py`: detect/provision each dependency into `<project>/.portable/<os>/` — Python, yt-dlp, ffmpeg+ffprobe (one static build shipping both), and the inference engine (ONNX Runtime + htdemucs `.onnx`, or audio-separator reusing UVR5 weights). **Reuse-first**: every check hits PATH/existing-installs first; only missing pieces self-provision.
- Everything needed lives under `.portable/` (gitignored) — no pollution of the system or UVR5 install. Network access is a deliberate capability; some videos may need yt-dlp cookies/oauth (`cookies.txt`).

## §11 — Binary / config discovery (§ next step 2)

### Findings on this box

| Tool | Reusable source found here | Portable? | Notes |
| --- | --- | --- | --- |
| ffmpeg + ffprobe | `AppData\Local\Programs\Shotcut\` (both, **shared** MSYS2 build n9.0.2) — good locally but **not self-portable** to another host without Shotcut; also anaconda pkgs (old 4.3.1), `%APPDATA%\yt-dlp_win_x86\`, `File Converter\` (ffmpeg only, no ffprobe). | Low for third-party apps | Prefer a **static build shipping both** into `.portable/` for true portability; reuse only when the host is identical to this one. |
| UVR5 + HTDemucs weights | Flat PyInstaller install at `AppData\Local\Programs\Ultimate Vocal Remover`; repo `models\Demucs_Models\v3_v4_repo\` ships **6 `.th` weights incl. `955717e8-8726e21a.th`** (the canonical HTDemucs v4). | High — pure model files, no python | Reuse the bundled weight directly; no re-download needed on this or any box with UVR5. |
| yt-dlp | Official standalone at `AppData\Roaming\yt-dlp_win_x86\yt-dlp.exe` (custom build: drop `--no-subs`; extract-audio needs an ffmpeg location). | Medium — per-OS, self-contained exe | Reuse if same OS/arch; otherwise download the official per-OS standalone into `.portable/<os>/`. |

### Design

1. **Curated known-install roots** (`core.py` / `bootstrap.py`, pure stdlib): probe PATH first, then a curated list of well-known app locations — `%APPDATA%\yt-dlp_win_x86`, `AppData\Local\Programs\{Shotcut,DaVinci Resolve,OBS Studio,Blender,Kdenlive,Avidemux,HandBrake,PotPlayer}` (apps that ship ffmpeg/ffprobe), UVR5 roots, plus `.portable/<os>/`. This is a *best-effort* probe; it does not need to find every copy on this box — it needs to cover the common cases cross-machine.
2. **Persist discovered paths in `auto_convert.json`** (repo root; records existing binaries + user overrides). On first run / re-provision, write what was found so future runs reuse without rescanning and can report *reused vs downloaded*. A missing/absent file → empty queue on load (already handled by `_input_queue.from_path`).
3. **Reuse-first provisioning**: `bootstrap.py` checks config + known roots first; only when absent (or `--provision`) does it download into `.portable/<os>/`: official yt-dlp standalone, a static ffmpeg+ffprobe build, and the ONNX Runtime HTDemucs export. The script then points yt-dlp's `--ffmpeg-location` at the discovered dir.
4. **FFmpeg nuance**: a directory with only `ffmpeg.exe` (no ffprobe) is usable for some yt-dlp builds but not this one — keep the "needs BOTH" rule, and treat third-party shared ffmpeg as *local-reuse-only* (not portable). Open question: whether to also detect a system-wide ffmpeg/ffprobe via `%PATH%` on Windows.

> §11 discovery is implemented (`b0d2603`) with 16 tests; the remaining piece — wiring `ConfigStore`
> into `provision.py` so future runs reuse recorded paths without rescanning and report *reused vs*
> downloaded (reuse-first provisioning, §4 item 3) — is now done (`f6f949e`): `record_provision()`
> stores freshly-extracted ffmpeg/ffprobe paths relative to scope; `resolve_recorded_tools()`
> returns recorded locations still on disk as a reuse fast-path in `run._resolve_run_env()`, falling
> back to the PATH/app scan when nothing is recorded. Now 126 tests total.

## §12 — Developer experience: VSCode config / extensions (§ next step 1)

No `.vscode/` scaffolding exists yet. Add, so contributors get linting + type-checking + pytest-debug out of the box:

- **`.vscode/extensions.json`** — recommended extensions (Python, Pylance, `ms-python.vscode-pylance`, `pytest-test-selector`, a linter such as `charliermarsh.ruff` or `MS-python.black`/isort formatters, `streetsidesoftware.code-spell-checker`, and `ms-vscode.remote-ssh` for testing the portable layout on another host).
- **`.vscode/settings.json`** — Python type-checking (pyright), lint/format-on-save (black + isort via ruff or the black extension), exclude `.portable/`, `_work/`, build/output dirs, and ASCII-only source enforcement.
- **`.vscode/launch.json`** — commented-out pytest-debug task (`python -m pytest tests`) plus `test_server.py` live-server run; a single `run` task for the CLI.
- Wire linting/type-check into CI (GitHub Actions) so both runners stay green. Reference from `AGENTS.md`/`README.md`.

## §13 — Browser userscript & background-extension (next step)

§11 discovery + §12 VSCode dev-config are implemented; this is the next deliverable. The server already
emits a `/status` SSE stream with per-item terminal records (`last_completed`) and `pending`/`done`
counts, plus a queue-file schema (`_input_queue`) — both clients below build on that shared contract.

Implement the browser UI described in the original UX plan: a local server streams `/status` SSE progress to a popup; the client reads/writes a queue file and posts to `POST /add`.

- **Userscript** (`auto_convert.user.js`, Tampermonkey/Greasemonkey manifest): `GM_addStyle` UI, `@grant GM_xmlhttpRequest`/fetch to poll `/status` (bounded reads), read/write the local queue JSON, show progress + completion alerts with links. Zero-install: run alongside `python -m auto_convert.server`.
- **Background-extension variant** sharing one protocol (`GET /status` SSE + `POST /add`) — a small server that writes to the same queue file; lets users enqueue from an always-on background page instead of the CLI process.
- **Shared contract**: both clients use the same `/status` JSON shape (per-item terminal records in `last_completed`; `pending`/`done` counts) and the same queue-file schema (`_input_queue`). The server already emits this; this step is the client side + a test that drives the real SSE stream from an HTTP client.
- Keep it read-only to the project scope (never write outside `.portable/` or the queue file); alert on completion with download links.
