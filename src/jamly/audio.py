"""Build a sample-aligned drums/drumless pair with one shared clipping-prevention gain."""
from __future__ import annotations

from contextlib import ExitStack
import math
from pathlib import Path
import re
import shutil

import numpy as np
import soundfile as sf

from .core import build_stem_encode_cmd, check_cancelled, run

BLOCK_FRAMES = 65536


def mix_pair(stems: dict[str, str], workdir: Path, cancel_event=None) -> tuple[dict, dict]:
    if "drums" not in stems or len(stems) < 2:
        raise ValueError("A drums stem and at least one non-drum stem are required")
    paths = {"drums": workdir / "pair-drums.wav", "no_drums": workdir / "pair-no-drums.wav"}
    peak = 0.0
    with ExitStack() as stack:
        inputs = {name: stack.enter_context(sf.SoundFile(path)) for name, path in stems.items()}
        reference = inputs["drums"]
        shape = (reference.frames, reference.samplerate, reference.channels)
        if not reference.frames:
            raise ValueError("The separator produced empty audio")
        if any((file.frames, file.samplerate, file.channels) != shape for file in inputs.values()):
            raise ValueError("Separated stems have different lengths, sample rates, or channels")
        outputs = {
            name: stack.enter_context(sf.SoundFile(
                path, "w", samplerate=shape[1], channels=shape[2], subtype="FLOAT"
            )) for name, path in paths.items()
        }
        while True:
            check_cancelled(cancel_event)
            blocks = {name: file.read(BLOCK_FRAMES, dtype="float64", always_2d=True)
                      for name, file in inputs.items()}
            if not len(blocks["drums"]):
                break
            if not all(np.isfinite(block).all() for block in blocks.values()):
                raise ValueError("The separator produced non-finite samples")
            backing = sum(block for name, block in blocks.items() if name != "drums")
            peak = max(peak, float(np.abs(blocks["drums"]).max()), float(np.abs(backing).max()))
            outputs["drums"].write(blocks["drums"])
            outputs["no_drums"].write(backing)
    return paths, {"frames": shape[0], "sample_rate": shape[1], "channels": shape[2],
                   "unscaled_peak": peak}


def write_scaled(source: Path, output: Path, gain: float, cancel_event=None) -> None:
    with sf.SoundFile(source) as incoming, sf.SoundFile(
        output, "w", samplerate=incoming.samplerate, channels=incoming.channels, subtype="FLOAT"
    ) as outgoing:
        for block in incoming.blocks(blocksize=BLOCK_FRAMES, dtype="float64", always_2d=True):
            check_cancelled(cancel_event)
            outgoing.write(block * gain)


def decoded_peak(path: Path, ffmpeg: str, sample_rate: int, cancel_event=None) -> float:
    result = run([
        ffmpeg, "-nostdin", "-hide_banner", "-i", str(path),
        "-af", "aresample=%d,astats=metadata=0:reset=0" % (sample_rate * 4),
        "-f", "null", "-",
    ], check=True, cancel_event=cancel_event)
    values = re.findall(r"Peak level dB:\s*([-+\w.]+)", result.stderr)
    if not values:
        raise RuntimeError("ffmpeg did not report a decoded audio peak")
    levels = [float(value) for value in values]
    if any(math.isnan(value) or value == math.inf for value in levels):
        raise ValueError("Encoded audio has an invalid peak")
    return max(10 ** (value / 20) for value in levels)


def render_pair(stems: dict[str, str], workdir: Path, destinations: dict[str, Path],
                fmt: str, ffmpeg: str, cancel_event=None) -> dict:
    raw, info = mix_pair(stems, workdir, cancel_event)
    ceiling = 10 ** (-1 / 20) if fmt == "mp3" else 0.999
    gain = min(1.0, ceiling / info["unscaled_peak"]) if info["unscaled_peak"] else 1.0
    for _attempt in range(3):
        for name, source in raw.items():
            scaled = workdir / ("scaled-" + name + ".wav")
            write_scaled(source, scaled, gain, cancel_event)
            output = destinations[name]
            if fmt == "wav":
                shutil.copyfile(scaled, output)
            else:
                cmd = build_stem_encode_cmd(ffmpeg, str(scaled), str(output), fmt)
                run(cmd, check=True, cancel_event=cancel_event)
        if fmt == "wav":
            peak = info["unscaled_peak"] * gain
            break
        peak = max(decoded_peak(path, ffmpeg, info["sample_rate"], cancel_event)
                   for path in destinations.values())
        if peak <= ceiling * 1.00001:
            break
        gain *= ceiling / peak * 0.999
    else:
        raise RuntimeError("The MP3 pair could not meet the decoded peak target")
    return dict(info, gain=gain, peak=peak, peak_ceiling=ceiling, format=fmt)
