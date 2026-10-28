"""jamly — yt-dlp audio-separation tool.

A small package that turns a yt-dlp source into drums and no-drums outputs:
  - downloads the highest-quality audio (yt-dlp),
  - separates it into four Demucs v4 HTDemucs stems (torch `demucs` CLI, model htdemucs),
  - mixes everything-but-drums (bass + other + vocals) into ``<title>_no_drums.wav`` and writes
    ``<title>_drums.wav``.

See DESIGN.md for the pipeline, queue, server, and browser-client contracts.
"""

__version__ = "0.1.0"
