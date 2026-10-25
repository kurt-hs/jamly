# Design — audio-separation pipeline

YouTube link → highest-quality audio (yt-dlp) → Demucs v4 four-stems (`htdemucs`) → two WAV stems.
See `README.md` for usage and `AGENTS.md` for contributing conventions. It records the design,
decisions, and machine-specific findings below.

## Overview (§0)

The Python package lives under `src/splitfy/` (stdlib-only):
`normalize.py` (URL→watch-id), `core.py` (subprocess runners for yt-dlp/Demucs/ffmpeg),
`bootstrap.py` (reuse-first dependency detection), `provision.py` (self-provisioning into
`.portable/`), `watch_queue.py` (`WatchQueue`, resumable + persisted done-set), `run.py` (CLI
orchestrator with queue integration) and `server.py` (streaming `/status` SSE status server). Binary/config discovery is a cache-aware probe: `find_known_roots()` plus the new path-backed
`splitfy/config_state.py` (`ConfigStore`, atomic persistence, empty-if-missing/invalid),
and `tests/test_bootstrap_extra.py` (16 tests covering the probe/cache/persistence contracts). The
probe is made robust on hosts where a git-bash-style env can make `os.walk` yield odd-shaped tuples.

Test suite is green under **both** runners — `python -m unittest discover -s tests` → **129** OK,
`pytest` → **129** passed.

### Developer experience — VSCode config / extensions (§12)

`.vscode/` scaffolding gives contributors linting + type-checking + pytest-debug out of the box:
`.vscode/extensions.json` (Python, Pylance, pytest-test-selector, black/ruff formatter, code-spell-checker,
remote-ssh), `.vscode/settings.json` (pyright `basic`, format-on-save via black, ASCII-only source),
and `.vscode/launch.json` (modern schema: top-level object with default `pytest-debug` task + named
per-task tasks each tagged with a unique `taskSig`; debug tasks set `PYTHONPATH=<root>/src` so no editable
install is needed). Dropped the interpreter-pinned `.code-workspace` at user request. The AGENTS.md /
README.md references are in place; there is no CI host, so lint/type-check are enforced locally via
the VSCode dev-config rather than GitHub Actions.

### Friendly run-time output (§14)

A terse/friendly per-stage log is emitted through the module logger and unit-tested via
`tests/test_logging.py`. Per raw input it produces: one line flagged `(blank)`/`(invalid)`, a dedup
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

### Output naming (§7)

All three outputs for one item share the **watch id** as their common prefix, matching the yt-dlp
extract (`<wid>.<ext>`) so every file for an item is traceable to a single name:

- extract:  `<workdir>/<wid>.<ext>`           (build_download_cmd)
- drums:    `<output_dir>/<wid>_drums.<fmt>`   (+ `.meta.json` sidecar, named but not written by code)
- no-drums: `<output_dir>/<wid>_no_drums.<fmt>`

The id is URL-safe, unique per video, and known before extraction -- a stable filename basis on every
platform (no title to rely on yet; no unicode/special chars; no truncation). `res.title` stays a display/logging name only.

The **no-drums mix excludes the drum stem by construction**: ffmpeg blends bass+other+vocals into
`nodrums_out`, never mixing in `<wid>_drums.<fmt>`. Output format (`--audio-format`) defaults to lossy MP3;
raw WAV is verbatim (copy, no encoder).

## Environment findings on this machine (§2)

> Findings for **this box**; the portable design (`§10`) does not depend on them — `bootstrap.py`
> self-provisions missing tools into `.portable/`. Useful as ground truth for local testing.

| Item | Finding |
|------|---------|
| System Python | `%USERPROFILE%\anaconda3\python.exe` (v3.13) — no yt-dlp / audio-separator / torch here |
| yt-dlp binary | Standalone exe at `%USERPROFILE%\AppData\Roaming\yt-dlp_win_x86\yt-dlp`. Custom-build quirks: `--no-subs` is invalid → omit it; `-x/--extract-audio` needs an ffmpeg/ffprobe location and this build is intermittent about detecting them (only `ffmpeg.exe` present). Verify success exit code + output path before relying on it. |
| UVR5 install | Flat PyInstaller layout — `.pyd` files live at the install root; runtime data in `lib_v5\`, models in `models\`. No bundled python/site-packages. |
| UVR5 models present | `MDX_Net_Models` (incl. DrumSep model), `Demucs_Models\v3_v4_repo` (`htdemucs.yaml` + `.th` weights → model `955717e8`), `VR_Models`. |
| GPU backend | **RTX 4070 SUPER (12GB)** confirmed → CUDA available & preferred. UVR5 also ships torch_directml as fallback. |
| audio-separator | Headless package driving UVR5's Demucs/MDXC models. Recognizes UVR5's bundled `.th` weights — reuse the installed HTDemucs weight directly, no canonical `.pt` re-download. |
| ffmpeg / ffprobe | Bundled at `%USERPROFILE%\AppData\Local\Programs\Ultimate Vocal Remover\ffmpeg.exe`. No `ffprobe.exe` reachable anywhere; point yt-dlp's `--ffmpeg-location` at a dir with BOTH, or verify exit code + output path on intermittent ffmpeg-only success. |

## Durable / portable scope (§10)

A single folder that self-heals on any host: reuse what is installed, download & install the rest
into its own private `.portable/` scope so it "just works" anywhere (Windows primary; macOS/Linux/ARM as stretch). No global installs required.

- `bootstrap.py`: detect/provision each dependency into `<project>/.portable/<os>/` — Python, yt-dlp, ffmpeg+ffprobe (one static build shipping both), and the inference engine (ONNX Runtime + htdemucs `.onnx`, or audio-separator reusing UVR5 weights). **Reuse-first**: every check hits PATH/existing-installs first; only missing pieces self-provision.
- Everything needed lives under `.portable/` (gitignored) — no pollution of the system or UVR5 install. Network access is a deliberate capability; some videos may need yt-dlp cookies/oauth (`cookies.txt`).

## §11 — Binary / config discovery

### Findings on this box

| Tool | Reusable source found here | Portable? | Notes |
| --- | --- | --- | --- |
| ffmpeg + ffprobe | `AppData\Local\Programs\Shotcut\` (both, **shared** MSYS2 build n9.0.2) — good locally but **not self-portable** to another host without Shotcut; also anaconda pkgs (old 4.3.1), `%APPDATA%\yt-dlp_win_x86\`, `File Converter\` (ffmpeg only, no ffprobe). | Low for third-party apps | Prefer a **static build shipping both** into `.portable/` for true portability; reuse only when the host is identical to this one. |
| UVR5 + HTDemucs weights | Flat PyInstaller install at `AppData\Local\Programs\Ultimate Vocal Remover`; repo `models\Demucs_Models\v3_v4_repo\` ships **6 `.th` weights incl. `955717e8-8726e21a.th`** (the canonical HTDemucs v4). | High — pure model files, no python | Reuse the bundled weight directly; no re-download needed on this or any box with UVR5. |
| yt-dlp | Official standalone at `AppData\Roaming\yt-dlp_win_x86\yt-dlp.exe` (custom build: drop `--no-subs`; extract-audio needs an ffmpeg location). | Medium — per-OS, self-contained exe | Reuse if same OS/arch; otherwise download the official per-OS standalone into `.portable/<os>/`. |

### Design

1. **Curated known-install roots** (`core.py` / `bootstrap.py`, pure stdlib): probe PATH first, then a curated list of well-known app locations — `%APPDATA%\yt-dlp_win_x86`, `AppData\Local\Programs\{Shotcut,DaVinci Resolve,OBS Studio,Blender,Kdenlive,Avidemux,HandBrake,PotPlayer}` (apps that ship ffmpeg/ffprobe), UVR5 roots, plus `.portable/<os>/`. This is a *best-effort* probe; it does not need to find every copy on this box — it needs to cover the common cases cross-machine.
2. **Persist discovered paths in `splitfy.json`** (repo root; records existing binaries + user overrides). On first run / re-provision, write what was found so future runs reuse without rescanning and can report *reused vs downloaded*. A missing/absent file → empty queue on load (already handled by `watch_queue.from_path`).
3. **Reuse-first provisioning**: `bootstrap.py` checks config + known roots first; only when absent (or `--provision`) does it download into `.portable/<os>/`: official yt-dlp standalone, a static ffmpeg+ffprobe build, and the ONNX Runtime HTDemucs export. The script then points yt-dlp's `--ffmpeg-location` at the discovered dir.
4. **FFmpeg nuance**: a directory with only `ffmpeg.exe` (no ffprobe) is usable for some yt-dlp builds but not this one — keep the "needs BOTH" rule, and treat third-party shared ffmpeg as *local-reuse-only* (not portable). Open question: whether to also detect a system-wide ffmpeg/ffprobe via `%PATH%` on Windows.

> The discovery probe stores freshly-extracted ffmpeg/ffprobe paths relative to scope and reuses any
> recorded locations still on disk as a reuse fast-path in `run._resolve_run_env()`, falling back to the PATH/app
> scan when nothing is recorded. This reuse-first provisioning (§11 Design item 3) means future runs avoid
> rescanning; covered by the probe/cache/persistence tests (suite count **129**, see §0).

## §12 — Developer experience: VSCode config / extensions

`.vscode/` scaffolding gives contributors linting + type-checking + pytest-debug out of the box.

- **`.vscode/extensions.json`** — recommended extensions (Python, Pylance, `ms-python.vscode-pylance`,
  `pytest-test-selector`, a linter such as `charliermarsh.ruff` or `MS-python.black`/isort formatters,
  `streetsidesoftware.code-spell-checker`, and `ms-vscode.remote-ssh` for testing the portable layout on
  another host). The ruff extension is already installed at user level; its recommendation here makes it
  reproducible on a fresh clone.
- **`.vscode/settings.json`** — Python type-checking (pyright), lint/format-on-save, exclude `.portable/`,
  `_work/`, build/output dirs, and ASCII-only source enforcement.
- **`.vscode/launch.json`** — commented-out pytest-debug task (`python -m pytest tests`) plus `test_server.py`
  live-server run; a single `run` task for the CLI. Debug tasks set `PYTHONPATH=<root>/src`, so no editable
  install is needed.
- **Ruleset parity**: a project-level `[tool.ruff]` pins the check codes so VS Code's ruff language server and
  the anaconda `ruff` binary apply identical rules (keep `E4/E7/E9/F`; ignore only line-length/whitespace nits).
- No CI host exists in this repo, so lint/type-check are enforced locally via the dev-config rather than GitHub Actions.

## §13 — Browser userscript & background-extension

Both clients build on the shared
contract the server already emits: a `/status` SSE stream with per-item terminal records
(`last_completed`) and `pending`/`done` counts, plus the `watch_queue` queue-file schema.

- **Userscript** — `browser/userscripts/splitfy.user.js` (Tampermonkey/Greasemonkey manifest):
  `GM_addStyle` UI, `/status` polling via fetch/XHR fallback, read/write of the local queue JSON, and
  progress + completion alerts with download links. Runs alongside `python -m splitfy.server`.
- **Background-extension variant** — MV3 extension in `browser/extension/` (`manifest.json`, a service
  worker that imports the shared DOM-free client via `importScripts`, plus popup/options HTML). Shares one
  protocol with the userscript (`GET /status` SSE + `POST /add`); a small server writes to the same queue
  file so users can enqueue from an always-on background page instead of the CLI process.
- **Shared contract** — both clients use the identical `/status` JSON shape and `watch_queue` schema;
  the shared client (`browser/extension/splitfy_client.js`) is DOM-free, so it ships as a static
  asset in the extension rather than as an `splitfy` module. A contract test drives the parsing logic
  directly; streamed-path tests use bounded reads so they stay deterministic under both runners.
- Read-only to project scope (never writes outside `.portable/` or the queue file); alerts on completion
  with download links.

The browser source lives under `browser/extension/` and `browser/userscripts/` — it is **not** part of the
Python package, tests, or `src/splitfy/`, and its manifest resolves all references relative to its own
folder (so no path edits are needed when files move together).
