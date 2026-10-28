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

This MIT license covers the original `jamly` application code (the CLI,
HTTP status server, input queue, tool discovery/provisioning helpers and pure
orchestration builders under `src/jamly/`) and the bundled browser
extension + userscript assets.

It does **not** cover third-party tools this project drives at runtime — each keeps
its own license:

- **Demucs / htdemucs** (Meta) — installed as a Python dependency and invoked with
  `python -m demucs`; model weights are downloaded by Demucs and are not committed here.
- **PyTorch** — installed transitively by Demucs.
- **ffmpeg / ffprobe** (FFmpeg) — system tools invoked as subprocesses.
- **yt-dlp** — installed as a Python dependency and invoked as a subprocess.

If you redistribute our code, keep this LICENSE.md and the copyright notice above.
