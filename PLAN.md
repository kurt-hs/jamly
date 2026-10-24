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
- Output container: **let yt-dlp decide**.

---
## 1. Goal

Given a YouTube link / partial URL / watch ID, download the highest-quality available
audio with `yt-dlp`, then use **Ultimate Vocal Remover 5** (installed at
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

- **GPU backend**: RESOLVED - RTX 4070 SUPER (12GB) present -> use CUDA torch; DirectML fallback via `torch-directml` (UVR5-bundled). Auto-detect provider with a manual override flag.
- **Model loading for HTDemucs**: RESOLVED - audio-separator already recognizes UVR5's bundled weights (`955717e8.th` + `htdemucs.yaml`, Issue #161); reuse directly, no `.pt` re-download. See §3/§4.3.
- **Output container**: RESOLVED - let yt-dlp decide the native best container and pass it straight through UVR5 (scope A2).
- **Future work (later)**: batch/playlist processing - queue multiple links, run sequentially with progress + resumable downloads.

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
├── pipeline.py            # main orchestration (yt-dlp -> separation -> ffmpeg)
├── server.py              # optional HTTP endpoint for the browser extension
├── requirements.txt       # torch (CUDA build) + DirectML fallback, audio-separator, ffmpeg-python, Flask (dev)
├── README.md              # setup + usage
└── tests/                 # input-normalization + CLI smoke tests
```

## 9. Build order
1. Input normalization + `yt-dlp --extract-audio --audio-format best` → native lossless container (verify highest quality).
2. Set up dedicated CUDA venv; install audio-separator + CUDA torch (+ DirectML fallback); confirm it imports/runs headless.
3. Load/obtain HTDemucs weights; run four-stems on a test clip; verify `drums.wav` exists.
4. ffmpeg mix of bass+other+vocals → `no-drums.wav`; verify both outputs.
5. Wrap as CLI (`pipeline.py`) with logging, error handling, and clean output dir.
6. Add HTTP server mode + write extension-side integration notes; document DirectML setup for this box.
