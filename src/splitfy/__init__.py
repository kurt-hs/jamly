"""splitfy — portable YouTube audio-separation tool.

A single, self-healing script/folder that turns a YouTube link into lossless stems:
  - downloads the highest-quality audio (yt-dlp),
  - separates it into four Demucs v4 HTDemucs stems (audio-separator package or UVR5 weights),
  - mixes everything-but-drums into ``no_drums.wav`` and writes ``drums.wav`` directly.

See DESIGN.md for the full design rationale (portability, GPU provider auto-detection, output metadata).
"""

__version__ = "0.1.0"
