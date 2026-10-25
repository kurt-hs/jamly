# MIT License

Copyright (c) 2026 Kurt Galiatsatos

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

## Scope of this license

This MIT license covers the original `auto_convert` application code (the CLI,
HTTP status server, input queue, tool discovery/provisioning helpers and pure
orchestration builders under `src/auto_convert/`) and the bundled browser
extension + userscript assets.

It does **not** cover third-party tools this project drives at runtime — each keeps
its own license:

- **htdemucs / Demucs** (Meta) — invoked as `python -m htdemucs`; ships under its
  own terms. Not bundled in source here; the `.th` weights are downloaded/reused, not committed.
- **audio-separator / UVR5 ("Ultimate Vocal Remover")** — external Python package and an optional local install we detect/reuse.
- **ffmpeg / ffprobe** (FFmpeg) and the BtbN static build it downloads into `.portable/` at runtime.
- **yt-dlp** — standalone extractor invoked as a subprocess; its own license applies to any bundled source you fetch through it.

If you redistribute our code, keep this LICENSE.md and the copyright notice above.
