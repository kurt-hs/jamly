"""Input normalization for any source that ``yt-dlp`` understands.

Pure standard-library Python (no third-party deps) so it is trivially portable
across Windows, macOS and Linux and easy to unit-test without spawning anything.

YouTube inputs keep their 11-character video id. Other URLs and extractor expressions receive a
stable hash-based id while retaining the original source text for yt-dlp.

Accepted forms -> stable id:
    bare id                 dQw4w9WgXcQ            (11 chars from [A-Za-z0-9_-])
    youtube.com/watch?v=ID  https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=...
    youtu.be/ID             https://youtu.be/dQw4w9WgXcQ
    shorts                  https://youtube.com/shorts/dQw4w9WgXcQ
    embed                   https://www.youtube.com/embed/dQw4w9WgXcQ
    partial path            youtube.com/watch?v=dQw4w9WgXcQ
    any host                https://vimeo.com/123456789
    extractor expression    ytsearch1:artist song

A single input (str) or an iterable of inputs may be passed. Duplicates are removed and order is
preserved; blank entries are dropped.
"""
from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass
from typing import List, Optional, Sequence, Union

# A YouTube video id is exactly 11 chars from [A-Za-z0-9_-]. Casing is preserved.
WATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# YouTube-specific URL forms. Other hosts are passed through instead of pretending their ids use
# YouTube's fixed 11-character format.
YT_URL_RE = re.compile(
    r"(?:https?:)?"
    r"(?:www\.|music\.)?(?:youtube\.com|youtu\.be)/"
    r"(?:watch\?(?:[^&#]*&|#)?v=|shorts?\/|embeds?\/)?"
    r"([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)


class InputError(ValueError):
    """Raised when an input is blank or otherwise unusable."""


@dataclass(frozen=True)
class Input:
    """One normalized input.

    ``watch_id`` is the short, URL-safe token used for output filenames and title fallbacks.
    ``source_url`` is the cleaned original text (full URL / partial path / bare id as given),
    preserved verbatim so it can be handed straight to yt-dlp regardless of host -- that is what
    lets any source yt-dlp understands work with this tool, not just YouTube.
    """

    watch_id: str
    source_url: str


def is_valid_watch_id(text: str) -> bool:
    return isinstance(text, str) and WATCH_ID_RE.match(text.strip()) is not None


def extract_watch_id(input_text: Optional[str]) -> str:
    """Normalize one string (URL / partial path / bare id) to its 11-char watch id.

    Raises InputError if the input does not contain a recognizable watch id. Kept as a standalone
    helper so callers that only need the id (e.g. the resumable queue, which keys on ids) can keep
    using it unchanged.
    """
    if input_text is None:
        raise InputError("empty/None input")
    text = input_text.strip()
    if not text:
        raise InputError("blank input")
    if WATCH_ID_RE.match(text):
        return text
    m = YT_URL_RE.search(text)
    if not m:
        raise InputError(
            "could not extract a watch id from %r" % (text,)
        )
    return m.group(1)


def make_input(input_text: Optional[str]) -> Input:
    """Build one :class:`Input` while retaining arbitrary yt-dlp sources."""
    if input_text is None:
        raise InputError("empty/None input")
    src = (input_text or "").strip()
    if not src:
        raise InputError("blank input")
    try:
        input_id = extract_watch_id(src)
    except InputError:
        digest = hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]
        input_id = "src-%s" % digest
    return Input(watch_id=input_id, source_url=src)


def normalize_inputs(
    inputs: Union[str, Sequence[Optional[str]], None],
    dedupe: bool = True,
) -> List[str]:
    """Normalize one or many inputs into an ordered list of stable ids.

    A single string is treated as one input (not a sequence). Blank/None items are skipped.
    With dedupe=True duplicates are removed while preserving first-seen order.

    Returns bare ids by design: the resumable queue keys on ids and resumes without needing the
    original source URLs, so this keeps that contract stable."""
    if inputs is None:
        return []
    if isinstance(inputs, str):
        seq = [inputs]
    else:
        seq = list(inputs)

    out: List[str] = []
    seen: set = set()
    for item in seq:
        # skip None and blank strings per the documented contract
        if item is None or (isinstance(item, str) and not item.strip()):
            continue
        if not isinstance(item, str):
            raise InputError("non-string input in %r" % (seq,))
        wid = make_input(item).watch_id
        if dedupe and wid in seen:
            continue
        out.append(wid)
        if dedupe:
            seen.add(wid)
    return out


def normalize_inputs_full(
    inputs: Union[str, Sequence[Optional[str]], None],
    dedupe: bool = True,
) -> List[Input]:
    """Like :func:`normalize_inputs`, but also keep the original source for each item.

    Used by the execution path so yt-dlp receives a full URL / partial path (not just a bare id),
    while output filenames stay rooted on the stable id."""
    if inputs is None:
        return []
    if isinstance(inputs, str):
        seq = [inputs]
    else:
        seq = list(inputs)

    out: List[Input] = []
    seen: set = set()
    for item in seq:
        # skip None and blank strings per the documented contract
        if item is None or (isinstance(item, str) and not item.strip()):
            continue
        if not isinstance(item, str):
            raise InputError("non-string input in %r" % (seq,))
        inp = make_input(item)
        if dedupe and inp.watch_id in seen:
            continue
        out.append(inp)
        if dedupe:
            seen.add(inp.watch_id)
    return out


if __name__ == "__main__":
    # quick smoke test when run directly
    samples = [
        "dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&t=30s",
        "https://youtu.be/dQw4w9WgXcQ",
        "youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "  https://www.youtube.com/watch?v=dQw4w9WgXcQ  ",
        "https://vimeo.com/123456789",
    ]
    print("single:")
    for s in samples:
        print("   %r -> %s" % (s, make_input(s).watch_id))
    print("multi (dedupe):")
    ids = ["dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ", None, "  youtube.com/shorts/DQW4W9WGXCQ  "]
    print("   ", normalize_inputs(ids))
