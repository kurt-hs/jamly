# Technical Plan — audio-separation pipeline

YouTube link → highest-quality audio (yt-dlp) → Demucs v4 four-stems (`htdemucs`) → two WAV stems.
See `README.md` for usage and `AGENTS.md` for contributing conventions. This doc is the design +
the findings that shaped it.

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
