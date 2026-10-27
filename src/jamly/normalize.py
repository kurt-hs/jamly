"""Input normalization for any source URL that ``yt-dlp`` understands.

Pure standard-library Python (no third-party deps) so it is trivially portable
across Windows, macOS and Linux and easy to unit-test without spawning anything.

The tool accepts whatever ``yt-dlp`` can fetch audio from -- not just YouTube. Each input
normalizes to a short, URL-safe **watch id** used for stable output filenames, while the
original source text is kept verbatim (as-is) so it can be handed straight to yt-dlp for any
host it understands.

Accepted forms -> normalized watch id:
    bare id                 dQw4w9WgXcQ            (11 chars from [A-Za-z0-9_-])
    youtube.com/watch?v=ID  https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=...
    youtu.be/ID             https://youtu.be/dQw4w9WgXcQ
    shorts                  https://youtube.com/shorts/dQw4w9WgXcQ
    embed                   https://www.youtube.com/embed/dQw4w9WgXcQ
    partial path            youtube.com/watch?v=dQw4w9WgXcQ
    any host                https://vimeo.com/123456789   (and any other yt-dlp source)

A single input (str) or an iterable of inputs may be passed. Duplicates are removed, order
preserved; invalid/blank entries raise ValueError / are dropped respectively.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Union

# A watch id is exactly 11 chars from [A-Za-z0-9_-]. Casing is preserved as-is
# (case-insensitive matching only) so repeated runs stay stable. This format is shared by many
# hosts (YouTube, Vimeo, ...); it is deliberately host-agnostic so the tool follows yt-dlp's own
# notion of "a short id in a URL" rather than hard-coding any single provider.
WATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# URL forms: optional scheme, then ANY host (letters/digits/dots/dashes), a slash after it,
# an OPTIONAL known path prefix (watch?v= / shorts / embed), then the 11-char id. The host is
# intentionally unrestricted so Vimeo/Bandcamp/any yt-dlp-supported site resolves; requiring a
# slash means a bare "watch?v=..." without a host does NOT match here (a bare id is handled
# separately by WATCH_ID_RE).
YT_URL_RE = re.compile(
    r"(?:https?:)?"
    r"[A-Za-z0-9.\-_]+/"
    r"(?:watch\?(?:[^&#]*&|#)?v=|shorts?\/|embeds?\/)?"
    r"([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)


class InputError(ValueError):
    """Raised when a single input cannot be normalized to a watch id."""


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
    """Build one :class:`Input` from raw text, pairing the extracted watch id with the original
    source. The source is stripped but otherwise passed through verbatim so yt-dlp can resolve it
    for any host."""
    if input_text is None:
        raise InputError("empty/None input")
    src = (input_text or "").strip()
    return Input(watch_id=extract_watch_id(src), source_url=src)


def normalize_inputs(
    inputs: Union[str, Sequence[Optional[str]], None],
    dedupe: bool = True,
) -> List[str]:
    """Normalize one or many inputs into an ordered list of watch ids.

    A single string is treated as one input (not a sequence). Blank/None items are skipped; invalid
    items raise InputError. With dedupe=True duplicates are removed while preserving first-seen order.

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
        wid = extract_watch_id(item)  # raises InputError on failure
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
    while output filenames stay rooted on the short watch id."""
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
        inp = make_input(item)  # raises InputError on failure
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
        print("   %r -> %s" % (extract_watch_id(s), make_input(s).watch_id))
    print("multi (dedupe):")
    ids = ["dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ", None, "  youtube.com/shorts/DQW4W9WGXCQ  "]
    print("   ", normalize_inputs(ids))
