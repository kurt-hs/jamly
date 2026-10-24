# Technical Plan: YouTube → UVR5 Drum/No-Drum Audio Pipeline

## 0. Assumptions / Scope / Status

**Status: PLAN ONLY — implementation NOT started.** Only research + read-only tool testing has been done; no code committed to production use yet.

### Assumptions (confirm before coding)
- **A1.** Pipeline runs **natively on Windows** (cmd/PowerShell), not inside git-bash, so subprocess calls to `yt-dlp.exe`, the audio-separator venv python, and `ffmpeg.exe` work cleanly; CUDA torch detects the RTX 4070 there. (We develop from a git-bash shell but target Windows.)
- **A2.** Highest-quality audio = yt-dlp's native best container (`--audio-format best --audio-quality 0`); keep that container and pass it straight through UVR5 rather than forcing WAV/FLAC. (User chose "let yt-dlp decide." — this is the decision.)
- **A3.** The custom yt-dlp audio-extraction step must be made reliable (ffprobe present + deterministic success) before production use; do not silently accept intermittent failures.
- **A4.** CUDA torch-CUDA installed in a dedicated venv for `audio-separator`/Demucs inference. UVR5 remains untouched and unmodified.

### Scope
- **In scope:** input normalization → yt-dlp best-audio download → Demucs-v4 four-stems (reusing UVR5's bundled HTDemucs weights via audio-separator) → ffmpeg mix of non-drum stems into `drums.wav` + `no_drums.wav`. CLI entrypoint. Optional HTTP server mode for the browser extension.
- **Out of scope now:** batch/playlist processing, audio DSP/post-processing, GUI, packaging/installer, model benchmarking (HTDemucs default).

### Open decisions already resolved by user
- Separation route: **ROUTE A — Demucs v4 four-stems**.
- GPU backend: **CUDA preferred** (RTX 4070 present; DirectML fallback).
- Output container: **let yt-dlp decide** the native best lossless container.
- Inference engine default: **ONNX Runtime Demucs v4 HTDemucs** for portability (no torch, single `.onnx`, CPU/DirectML/CUDA/CoreML). Reuse UVR5's bundled weights via audio-separator only as a *local fast path* when that app is already installed + a CUDA torch venv exists (§10).


- **Portability goal (stretch):** target a single script/folder that *just works* on any machine

  (Windows primarily; macOS/Linux/ARM as stretch). Prefer reusing already-installed tools and only

  self-provisioning missing ones into a private `.portable/` scope. Keep UVR5 optional: reuse its models

  when present, but do NOT require the full UVR5 app to run.
## 1. Goal

Given a YouTube link / partial URL / watch ID, download the highest-quality available
audio with `yt-dlp`, split it into stems, and emit two files — **drums-only** and
**no-drums** — via a Demucs-v4 four-stem model. Designed to be maximally portable: prefer
reusing tools already on the machine and self-provision the rest into a private scope so it
"just works" anywhere (UVR5 becomes optional rather than required).
`C:\Users\Kurt\AppData\Local\Programs\Ultimate Vocal Remover`) to produce two files:

- **drums.wav** — only drums
- **no-drums.wav** — the mix with drums removed

Language/runtime: **Python 3** for the orchestration (yt-dlp + audio separation).
PowerShell is available but Python is cleaner here because both `yt-dlp` and the UVR5
stack are already Python-based.

---

---

## 2. Environment facts discovered on this machine

| Item | Finding |
|------|---------|
| System Python | `C:\Users\Kurt\anaconda3\python.exe` (v3.13) — no yt-dlp / audio-separator / torch modules installed here |
| yt-dlp binary | Standalone exe at `C:\Users\Kurt\AppData\Roaming\yt-dlp_win_x86\yt-dlp` (subprocess, not a python module). **Custom-build quirks** (v2026.10.21): `--no-subs` is NOT valid → omit it; `-x/--extract-audio` needs an ffmpeg/ffprobe location to mux webm→audio and the build is intermittent about detecting them (only `ffmpeg.exe` present: sometimes succeeds, default container `.opus`, sometimes errors "Postprocessing: ffprobe and ffmpeg not found"). Confirm success exit code + output path before relying on it. |
| UVR5 install | Flat PyInstaller-style layout — packages/`.pyd` files live **at the install root**, no `site-packages`, no bundled `python.exe`, no `_MEI*`. Runtime data in `lib_v5\{mixer.ckpt, vr_network}` and models in `models\`. |
| UVR5 models present | `MDX_Net_Models` (incl. `aufr33-jarredou_DrumSep_model_mdx23c_ep_141...ckpt`, kuielab drums/bass onnx, roformer VR), `Demucs_Models\v3_v4_repo` (`htdemucs.yaml` + `.th` weights → model `955717e8`), `VR_Models`. |
| GPU backend | **RTX 4070 SUPER (12GB)** confirmed → CUDA available & preferred. UVR5 also ships torch_directml as fallback. |
| audio-separator | Headless package driving UVR5's Demucs/MDXC models. **Recognizes UVR5's bundled `.th` weights** (Issue #161: lists "Demucs v4: htdemucs", maps `955717e8-*.th`, uses `htdemucs.yaml`) - reuse the installed HTDemucs weight directly, no canonical `.pt` re-download. |
| Execution env | git-bash (MINGW64) on native Windows — Windows binaries can be invoked directly; pipeline should still run natively for robustness + CUDA detection. |
| ffmpeg / ffprobe | Bundled at `C:\Users\Kurt\AppData\Local\Programs\Ultimate Vocal Remover\ffmpeg.exe`. | **No `ffprobe.exe` anywhere reachable.** yt-dlp postprocessing wants both; recommend pointing `--ffmpeg-location` at a dir with BOTH, or accept intermittent ffmpeg-only success but verify exit code + output path in code. |
---

## 3. How "drums" vs "no drums" maps to available tools/models

UVR5 GUI's own **"Separate into stems"** mode literally offers *"drum"* and *"no drum only"*
(original minus drums) — exactly our target output. Under the hood that is **Demucs v4**
(HTDemucs), which produces 4 named stems: `drums.wav`, `bass.wav`, `other.wav`, `vocals.wav`.

Two implementation routes, both using UVR5's models/commands:

### ROUTE A — Demucs v4 four-stems (RECOMMENDED)
- Run audio-separator's Demucs runner with the HTDemucs weights → 4 stems.
- `drums.wav` = the **drums** stem directly.
- `no-drums.wav` = mix of **bass + other + vocals** (one `ffmpeg` concat).
- Pros: deterministic, exactly two output files, matches UVR5 GUI behavior, model is already installed.
- Cons: must load HTDemucs weights in a form audio-separator's Demucs class accepts.

### ROUTE B — MDX23C DrumSep model (`aufr33-jarredou_DrumSep_model_mdx23c...ckpt`)
- This is UVR5's native "DrumSep" model and lives already on disk in `MDX_Net_Models`.
- Run via audio-separator's MDXC runner with `--drumsep` (or full-song mode).
- Risk: on a **full mix** this model historically outputs drum *sub-stems* (kick/snare/hihat) rather than a single "drums" file; behavior varies by audio-separator version. Would need verification that it yields drums-only + non-drums, or post-processing to merge sub-stems back into one drums track and drop them from the rest.
- Pros: uses UVR5's exact DrumSep model, no weight-format conversion needed (native `.ckpt`).

> Decision point: **ROUTE A** gives the cleanest "one file with only drums / one without"
> result and is what UVR5 GUI itself does. Route B is a fallback if we can't load the
> HTDemucs weights cleanly, but needs validation of its output stems.

---


### Inference engine options (ROUTE A)

| Option | Engine | Model | Torch? | Portable across machines? | Notes |
| --- | --- | --- | --- | --- | --- |
| **Default (portable)** | ONNX Runtime + `demucs-onnx` / htdemucs `.onnx` | HTDemucs v4 (316 MB fp32 or 166 MB fp16, single-file) | No | High - pure numpy+onnxruntime; CPU/DirectML/CUDA/CoreML via one provider flag; model auto-downloaded from HF on first use | Same HTDemucs family as UVR5's bundled weight -> essentially identical drums/no-drums results. Best for "just works" anywhere. |
| Local fast path | audio-separator + torch-CUDA (reuse UVR5 `.th`) | UVR5's `95571e8.th`, etc. | Yes (large install) | Low - needs the UVR5 model folders + a CUDA venv carried over | Matches UVR5 GUI behavior exactly; fastest on RTX 4070; NOT portable unless you copy the weights+venv. Optional when UVR5 present. |
| Alternative | spleis (ONNX Runtime) / spleeter (TF, GPU) | various stem models | No (spleis) / yes (spleeter) | High (spleis) but stem set != Demucs; vocal/instrumental-first | Not aligned with the clean 4-stem -> drums/no-drums mapping. Consider only if Demucs unavailable. |

- **Recommendation:** default to ONNX Runtime HTDemucs for the portable target - no torch, one self-contained model, cross-platform GPU via a provider flag, and it is the same HTDemucs family as UVR5's bundled weight so drums/no-drums results are essentially identical. Keep audio-separator/UVR5-weights as an explicit local override when that app is present and you want GUI-identical quality or maximum GPU speed on the RTX 4070. Provider auto-detected (CUDA preferred, DirectML fallback).

## 4. Recommended architecture (ROUTE A)

```
┌─────────────────────┐     ┌───────────────────────────┐     ┌──────────────────────────────┐
│  CLI / browser ext  │ --> │   pipeline.py              │ --> │   dedicated Python venv      │
│  (pass link/watchID)│     │   1. validate + normalize input          audio-separator       │
│                      │     │   2. yt-dlp --extract-audio -f bestaudio  torch (+ DirectML)    │
│                      │     │   3. run demucs four-stems (HTDemucs)    installed here         │
│                      │     │   4. ffmpeg: drums.wav + (bass+other+vocals)=no-drums.wav          │
└─────────────────────┘     └───────────────────────────┘     │   reuse UVR5's model files      │
                                                               └──────────────────────────────┘
```

### 4.1 Input handling (flexible, extensible to a browser extension)
- Accept: full URL (`https://www.youtube.com/watch?v=...`), partial path
  (`youtube.com/watch`, `youtu.be/ID`), or bare watch ID (`dQw4w9WgXcQ`).
- Normalization: strip query/hash, extract the 11-char `v=` parameter as the watch ID.
- Design so a browser extension can call it two ways (see §6):
  - **CLI mode**: `python pipeline.py <url-or-id> [-o OUTDIR]`
  - **HTTP mode** (for extensions without CLI access to the host machine): a tiny local Flask/
    http.server endpoint that takes `{youtube_id}` and returns JSON of the two output file paths.
    This is the recommended path for a browser extension, since an extension can't shell out to
    `yt-dlp` on the user's PC directly — it needs a small server process in that environment.

### 4.2 yt-dlp (highest-quality audio)
- Command: `yt-dlp <url> --extract-audio --audio-format best --audio-quality 0 \
    --ffmpeg-location <dir-with-ffmpeg.exe-and-ffprobe.exe> -o '%(title)s.%(ext)s'`
- `--audio-format best` + `--audio-quality 0` → lossless native container (FLAC/ALAC/WAV) decided by
  the stream; pass it straight through UVR5 unchanged (§0-A2). Use `-F` first to log the best format if
  quality needs pinning.
- yt-dlp is invoked as the standalone exe (not a python module) via `subprocess`. See §7 for this box's
  custom-build quirks (`--no-subs` invalid; intermittent ffmpeg/ffprobe detection).

### 4.3 UVR5 / audio-separator separation (reusing its models)
- Create a **dedicated CUDA venv** with: `pip install torch (CUDA build) audio-separator ffmpeg-python`
  (or just shell out to the standalone `ffmpeg.exe`). Use the CUDA provider for Demucs inference on the
  RTX 4070 SUPER; keep DirectML only as an override fallback.
- Copy UVR5's model folders into the venv's models dir so we reuse *the models it uses*:
  - `MDX_Net_Models` → audio-separator MDXC models (`aufr33-jarredou_DrumSep_model_mdx23c...ckpt`).
  - `Demucs_Models/v3_v4_repo/955717e8.th` → inspect; if loadable as HTDemucs weights, use for four-stems. If not directly usable, fall back to ROUTE B or download the canonical `htdemucs.pt`.
- Run Demucs four-stems: `audio_separator ... --model htdemucs --four-stems -o <workdir> [--directml]`
  → yields `drums.wav`, `bass.wav`, `other.wav`, `vocals.wav`.

### 4.4 Output assembly (ffmpeg)
- `no-drums.wav` = mix of bass + other + vocals stems (equal gain, summed). ffmpeg:
  `ffmpeg -i bass.wav -i other.wav -i vocals.wav -shortest -mixer_channels 2 out/no_drums.wav`
- Rename/copy to final `drums.wav` and `no-drums.wav` in the user's output dir.

---

## 5. Browser-extension integration design

Extensions cannot run arbitrary local processes, so two supported modes:

1. **Local server mode (recommended)** — The pipeline venv runs a small HTTP endpoint
   (`/api/separate?youtube_id=...`) that accepts the watch ID and returns `{drums, no_drums}` paths.
   The extension's popup reads the user's last-used output dir from `chrome.storage`/`localStorage`,
   opens the two files in a new tab (or copies them to clipboard / downloads). The server is started
   once and kept alive while the browser session runs.

2. **CLI mode** — If the extension can trigger a desktop app/command, pass the normalized watch ID:
   `python pipeline.py <watch_id>`. Simpler, but requires an installed launcher (e.g. a small Electron/
   native bridge). Less ideal than the server for in-extension use.

Flow: popup → send `{youtube_id}` to endpoint → stream progress / status JSON → on completion, open
both files. Add `--headless` so no GUI window is shown; print/stream progress lines.

---

## 6. Open decisions (resolved) & future work

- **GPU backend**: RESOLVED - RTX 4070 SUPER (12GB) present -> CUDA preferred, DirectML fallback. Provider auto-detected on first run; manual override flag for both the ONNX default and any torch/audio-separator path.
- **Model loading for HTDemucs**: RESOLVED (as an option) - two interchangeable sources:
  1. Portable default: ONNX Runtime + htdemucs `.onnx`, auto-downloaded from HF into the scope; no torch, cross-platform GPU via provider flag (§3/§10).
  2. Local fast path: audio-separator reusing UVR5's bundled weights (`95571e8-8726e21a.th` + `htdemucs.yaml`, Issue #161) - identical HTDemucs results, GUI-identical quality on the RTX 4070. Only used when that app/weights are present (§3/§10.1).
- **Output container**: RESOLVED - let yt-dlp decide the native best lossless container; pass it straight through to separation unchanged. On the ONNX path there is no re-encode at all.
- **Future work (later, now drafted in §11):** queue-based batching - add/remove/reorder videos from the extension/userscript, bounded concurrency, per-item progress + completion alerts with links; resume/retry on transient download/separation failures.

---
---

## 7. Confirmed command shapes

### Input normalization (pure Python, cross-platform)
- Full URL → extract `v=` param; partial/`youtu.be/ID` → the ID directly.
- Validate: exactly 11 chars `[A-Za-z0-9-_]{11}`.

### yt-dlp (highest-quality audio; container decided by yt-dlp)

> NOTE: this box ships a **custom yt-dlp build** (v2026.10.21). During testing we found:
> - `--no-subs` is **not** a valid flag on it → omit it.
> - `-x/--extract-audio` still needs an ffmpeg/ffprobe location to mux webm→audio, and this build is **intermittent** about detecting them (only `ffmpeg.exe` present: sometimes succeeds, default container `.opus`; sometimes errors "Postprocessing: ffprobe and ffmpeg not found").
> - Confirm the success exit code + output path before relying on it (§0-A3).

```
yt-dlp <url> --extract-audio --audio-format best --audio-quality 0 \
    --ffmpeg-location <dir-with-ffmpeg.exe-and-ffprobe.exe> -o '%(title)s.%(ext)s'
```
- `--audio-format best` + `--audio-quality 0` → lossless native container (FLAC/ALAC/WAV) decided by the stream; pass it straight through UVR5 unchanged.
- If no ffprobe is available, point `--ffmpeg-location` at a dir with BOTH binaries (or accept intermittent ffmpeg-only success but verify exit code + output path in code).

### audio-separator Demucs four-stems (reusing UVR5's `.th` weights)
```
python -m audio_separator.demucs.separate \
    --model htdemucs \
    --name 955717e8-8726e21a.th \
    --repo <models/Demucs_Models/v3_v4_repo> \
    --four-stems \
    --input <audio> -o <workdir> [--device cuda] [--threads N]
```
→ `separated/htdemucs/<track>/drums.wav`, `bass.wav`, `other.wav`, `vocals.wav`.

### Output assembly (ffmpeg.exe mix of non-drum stems)
```
ffmpeg -i bass.wav -i other.wav -i vocals.wav -shortest \
    -map 0 -mixer_channels 2 <workdir>/no_drums.wav
```
→ final `drums.wav` = drums stem; `no_drums.wav` = mixed rest.

---

## 8. Proposed file layout

```
auto_convert/
├── run.py                # entrypoint (CLI + optional --serve); self-provisions first
├── bootstrap.py          # ensure .portable/ scope: python, yt-dlp, ffmpeg+ffprobe, inference engine
├── core.py               # normalize -> download -> separate -> mix -> tag/rename (imported by run/server)
├── server.py             # optional HTTP endpoint for the browser extension (--serve)
├── userscript.js         # portable userscript variant (progress + queue via injected fetch bridge)
├── .portable/            # self-healing scope (gitignored): venv, yt-dlp, ffmpeg, models, cache
│  ├── venv/              # python + audio-separator / torch-directml (+ onnxruntime when chosen) OR reused tools
│  ├── yt-dlp             # standalone exe if not already present
│  └── ffmpeg/            # static ffmpeg+ffprobe build (or symlink to UVR5's bundled ones)
├── README.md             # setup + usage
└── tests/                # input-normalization + CLI smoke tests
```


```
auto_convert/
├── pipeline.py            # main orchestration (yt-dlp -> separation -> ffmpeg)
├── server.py              # optional HTTP endpoint for the browser extension
├── requirements.txt       # torch (CUDA build) + DirectML fallback, audio-separator, ffmpeg-python, Flask (dev)
├── README.md              # setup + usage
└── tests/                 # input-normalization + CLI smoke tests
```

## 9. Build order (portable implementation)

**Bootstrap first (§10):** on a fresh machine the script self-provisions into `.portable/` - Python, yt-dlp, ffmpeg+ffprobe, and the ONNX Runtime + htdemucs model; only downloads what's missing.
1. Input normalization + `yt-dlp --extract-audio --audio-format best` → native lossless container (verify highest quality).
2. Provision scope (§10); confirm yt-dlp extracts audio AND onnxruntime imports headless.
3. Obtain htdemucs `.onnx`; run four-stems on a test clip; verify `drums.wav`.
4. ffmpeg mix of bass+other+vocals → `no-drums.wav`; verify both outputs.
5. Wrap as CLI (`run.py`) with logging, error handling, and clean output dir.
6. Add HTTP server mode + progress reporting (§11) + write extension/userscript integration notes; document GPU provider auto-detection (CUDA preferred).


## 10. Durable / portable implementation

**Goal:** a single folder that self-heals on any host - reuse what is installed, download & install the rest into its own private `.portable/` scope so it "just works" anywhere (Windows primary; macOS/Linux/ARM as stretch). No global installs required; UVR5 is optional.

### 10.1 Self-provisioning bootstrap (`bootstrap.py`)
- On first run, detect and provision each dependency inside `<project>/.portable/`:
- Python: prefer a system Python on PATH; else download the embeddable `python.zip` and create a venv in `.portable/venv`. Pin versions for reproducibility.
- yt-dlp (auto-obtain): if already present as a python module or an exe on PATH, reuse it; else download the official standalone release for the host OS/arch (`yt-dlp.exe` on Windows 10+, `yt-dlp_linux`, `yt-dlp_macos`; arm64 variants available). Falls back to `pip install yt-dlp` when a python is found.
- ffmpeg + ffprobe (auto-obtain): download one static build archive for the host OS/arch from BtbN/FFmpeg-Builds (`ffmpeg-master-latest-win64-gpl.zip`, `-linux64-`, `-macosx-arm64-…`) which ships BOTH `ffmpeg` and `ffprobe`; place under `.portable/ffmpeg/`. If UVR5's bundled `ffmpeg.exe` is detected, reuse it (and provide a matching ffprobe shim). Fallback: `ffmpeg-static` npm pkg.
- Inference engine: install into the scope - ONNX Runtime + `demucs-onnx` (auto-downloads htdemucs `.onnx`) for the portable default; OR audio-separator (+ torch-directml/CUDA) when reusing UVR5 weights. Detect GPU provider automatically (CUDA preferred, DirectML fallback); let the user override.
- Reuse-first policy: every step checks PATH / existing installs before downloading, so a machine with Python + ffmpeg already installed skips those downloads entirely.

### 10.2 Isolated scope and safe execution
- Everything the script needs lives under `.portable/` (gitignored) - no pollution of the system or UVR5 install.
- yt-dlp, ffmpeg and model I/O all run from within this scope so paths have no space issues and versions are pinned/deterministic.
- Network access is a deliberate capability; document that some videos may require `yt-dlp` cookies/oauth and how to inject them (portable `cookies.txt`).

## 11. Browser userscript / extension UX

**Approach:** ship both a userscript variant (`userscript.js`, zero-install, runs from any page) and an optional extension for a background server and persistent queue; the core protocol is shared.

- Progress reporting back to the popup: long-running work happens in `server.py` (HTTP). Expose incremental status via SSE/streaming JSON (`GET /status`) so the popup polls periodically without a heavy websocket dependency: `{queue:[{id,status,progress_pct,eta}], current:{watch_id,title}, done:[{title,url}]}`. The userscript variant injects a small `fetch` bridge into its own sandbox to reach the same endpoint.
- Queue: users add videos (paste URL or watch id) from any page; items are added to an in-memory + persisted (`queue.json`) queue with order preserved and dedup by watch id. Per-item status icons: queued / downloading / separating / done / error. Reorder/remove via context menu.
- Alerts and completion: on each item finishing, auto-open the finished drums/no-drums files (new tab or download) and show a toast with title + link; mark the queue item done and update the progress bar. Batching processes N items concurrently with bounded overlap.
- UX-improving flags:
  - `--progress-interval <sec>` (default 3, keeps the popup fresh without spamming).
  - `--auto-open-on-done` (open/download finished stems immediately) and stem choice (`drums|no-drums|both`).
  - `--replace-existing` vs skip when an output file already exists.
  - `--concurrency <n>` for queue throughput; `--max-duration-mins` to cap per-item cost.
  - `--headless` is the default here since we drive UVR5's headless stack, not its GUI.
- Server lifecycle: start once and keep alive for the session (auto-shutdown on tab close / idle timeout). Show a small status line in the popup. Support `--serve <host:port>` so it can also run standalone.

## 12. Output metadata and consistent renaming

**Goal:** music files that are easy to browse - consistent, human-friendly names plus embedded tags; do not rely on generic titles like "youtube".

- Source the title: use `yt-dlp`'s extracted metadata (`--print "%(title)s"`, optional uploader/channel) as authoritative when available; fall back to the extension's document title, then `<watch_id>.ext`. Cache per watch id so re-runs are stable.
- Filename scheme: `"<Artist> - <Title>.ext"`; if only a video title exists use it verbatim; if nothing exists use the sanitized watch id. Sanitize path separators and colons for every platform (stretch cross-platform).
- Durable tagging (embedded in the file, so it survives renames/copying):
  - FLAC/ALAC/WAV: Vorbis comments via ffmpeg (`-metadata artist=.. -metadata title=.. -metadata yt_id=..`).
  - MP3 (if chosen as container): ID3v2 tags.
  - Always also write a sidecar `.meta.json` next to each file with `{watch_id, source_title, uploader, made_at, stems:{drums,no_drums}}` for reliable lookup and future features (playlist files).
- Browsing aid: emit a `playlist.m3u8` in the output dir listing all finished items by title - opens cleanly in most media players.
