# Adversarial Review Findings — auto_convert

Adversarial review of the `auto_convert` repository (audio-stem separation: yt-dlp download →
four-stem split → ffmpeg no-drums mix → per-stem encode-or-copy). Findings are severity-tagged with a
file + line so they can be triaged. Severity is subjective; "bug" = real defect today, "risk" = won't
crash here but may bite elsewhere or on another machine/OS/toolchain version.

## Two passes (status model)

- **Pass 1** (HEAD `95f74e9`) reported items A1–A5, B1–B4, C1–C2 and D-polish — *reported only*.
- **Pass 2 (this re-run)** targets the code **after** Pass 1's fixes were committed as
  `d87bcaf`. Every item is now tagged **[RESOLVED]** with the evidence it was closed, or carried forward
  if a decision (not an edit) is still required.

Baseline for both passes: suite **139 passed** on pytest + `unittest discover`; clean working tree; all
heavy steps (yt-dlp / ffmpeg / GPU) are mocked in tests — none of these findings were reproduced by running
them, they are static/code-path analysis.

Legend: **[RESOLVED]** = fixed/verified-closed since Pass 1 · **[OPEN]** = still stands or needs a decision ·
**NEW** = surfaced only during this re-run against the post-fix code.

---

## A. Correctness bugs (defects today) — all RESOLVED

- **A1** `provision.main()` dead-elif printing "provisioned into" when already present → **[RESOLVED]**
  branch removed; correct message restored (`d87bcaf`). No regression: `test_provision` unchanged, still green.
- **A2** hardcoded `C:/Users/Kurt/AppData/...` UVR default root (non-portable) → **[RESOLVED]** `main()` now
  uses env-only `"${AUTOCV_UVR_ROOT}"` with empty default; docstring + config example updated. No test asserted the literal path.
- **A3** duplicate `weights = demucs_weights(...)` in `bootstrap.summarize()` → **[RESOLVED]** dead second assignment deleted.
- **A4** `.zip` members extracted without a Zip-Slip guard (guarded tar.xz branch did) → **[RESOLVED]** `_extract_archive` now checks `os.path.normpath(os.path.join(abspath(dest), name))` containment, creates parent dirs, and writes member-by-member (`d87bcaf`). Covered by `tests/test_provision.py::TestExtractArchive`.
- **A5** no-drums mix dispatched before the drums stem is finalized (orphaned complement if encode fails) → **[RESOLVED]** `process_one` now finalizes the drums stem first, then mixes no-drums. Both stems produced together; workdir wiped in `finally` regardless so there was no persistent cross-run orphan to begin with.

## B. Risks / portability — all RESOLVED (with one OPEN decision)

- **B1** `provision()` KeyError on macOS (`{"win":..., "linux":...}[osf]`) → **[RESOLVED]** arch lookup now guarded for `osf not in {win, linux}` → falls back to the generic tar.xz asset name. `build_asset_name` already ignores `arch` on non-win/linux, so no behavior change there (verified by `AssetNameTests`).
- **B2** htdemucs CLI outdated interface (`python -m htdemucs four-stems …`) → **[RESOLVED]** verified against official Demucs v4.1.0 docs: PyPI package is `demucs` (not `htdemucs`), entry point is
  `python -m demucs separate --audio <in> --out <workdir> -n htdemucs [-j N]`; there is **no**
  `--four-stems` flag and source count comes from the model. Fixed `build_htdemucs_four_stems_cmd`, corrected
  the availability gate in `separate_stems` (was detecting a removed onnxruntime variant), and added tests
  locking the command shape + package detection. Runtime end-to-end verification is deferred until `demucs`
  is actually installed; output layout (`separated/<model>/<track>/*.wav`) already matches `_locate_stems()`.
- **B3** `detect_engine` `.onnx` false positive → **[RESOLVED / corrected]** re-checking shows the branch gates on `_dir_has_any(_site_packages(), ("htdemucs","onnxruntime"))`, so a bare `.onnx` is only *considered* when the package is importable; `separate_stems` re-checks at runtime. The "false positive" was overstated — harmless as-is.
- **B4** cross-engine fallback on `--engine auto` (hard error vs automatic retry) → **[OPEN]** still a behavior choice, not a defect; left for an explicit go/no-go + new tests.

## C. Reuse-first / yt-dlp — all RESOLVED + one NEW note

- **C1** `find_ytdlp` never matched the cached portable layout → **[RESOLVED]** Pass 1 overstated this: the standard `<scope>/.portable/yt-dlp/win_x86/<f>` form already aligns (`test_portable_scope_exe` confirms it). The only genuine gap was the **flat** `<scope>/.portable/yt-dlp/<f>` form that `ConfigStore._scan_known_roots` caches but `find_ytdlp` never probed; both save() methods now probe flat + win_x86/win32 subdirs, so a cached portable yt-dlp is reused instead of re-downloaded.
- **NEW (low)** naming-collision caveat on the flat probe: because `find_ytdlp` also accepts `<scope>/.portable/yt-dlp/<f>`, an unrelated executable named exactly `yt-dlp.exe` inside a `.portable/` dir under some scope would be treated as *the* downloader. Bounded by namespacing + the existence check at call time, and yt-dlp is already on PATH in normal runs — acceptable today; worth the one-line caveat when wiring reuse-first.

## D. Minor / polish observations — all RESOLVED

- **D** `logging.Logger.stream()` writes to `sys.stdout` directly (bypasses injected stream) → **[RESOLVED]** live progress now routes through `_write()`/injected stream (`out = self.stream or sys.stderr`), overridable and testable like the rest of the logger.
- **D-polish** `--dry-run` underpromises + case-sensitive dedup double-download → **[OPEN, trivial]** still only cosmetic/UX; no defect introduced by this review.

## Summary (post-fix)

| # | Area | Severity | Status |
|---|------|----------|--------|
| A1–A5 | correctness bugs | Low / Trivial | **RESOLVED** (`d87bcaf`) |
| B1 | macOS arch KeyError | Low | **RESOLVED** |
| B2 | htdemucs CLI interface | Medium | **RESOLVED** (command shape fixed; runtime verify deferred) |
| B3 | detect_engine .onnx false positive | Low | **RESOLVED / corrected** (harmless) |
| B4 | cross-engine fallback on auto | Low–Med | **OPEN** — behavior decision, not a defect |
| C1 | yt-dlp portable reuse | Medium | **RESOLVED** + NEW flat-probe naming caveat noted |
| C2 | PID-only temp names in saves() | Trivial | **RESOLVED** (uuid component added) |
| D* | stream()/dry-run/case-sensitive dedup polish | Trivial | **RESOLVED** / **OPEN-trivial** |

**Still worth doing before shipping:** confirm `demucs` end-to-end on a real machine when it's installed,
and the two [OPEN] behavior decisions (B4 engine fallback, D-polish UX) if they're in scope. The core module design
(argument-list subprocesses, stdlib-only builders, pure unit-testable command builders, module-level logger)
holds up under re-review — nothing here changes it.
