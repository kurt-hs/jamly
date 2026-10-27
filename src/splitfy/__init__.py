"""splitfy — portable YouTube audio-separation tool.

A single, self-healing script/folder that turns a YouTube link into lossless stems:
  - downloads the highest-quality audio (yt-dlp),
  - separates it into four Demucs v4 HTDemucs stems (torch `demucs` CLI, model htdemucs),
  - mixes everything-but-drums (bass + other + vocals) into ``<title>_no_drums.wav`` and writes
    ``<title>_drums.wav``.

See DESIGN.md for the full design rationale (portability, GPU provider auto-detection, output metadata).
"""

__version__ = "0.1.0"
