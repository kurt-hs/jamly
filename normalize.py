"""Input normalization for YouTube links / partial URLs / watch IDs.

Pure standard-library Python (no third-party deps) so it is trivially portable
across Windows, macOS and Linux and easy to unit-test without spawning anything.

Accepted forms -> normalized 11-char watch id:
    bare id                 dQw4w9WgXcQ
    youtube.com/watch?v=ID  https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=...
    youtu.be/ID             https://youtu.be/dQw4w9WgXcQ
    shorts                  https://youtube.com/shorts/dQw4w9WgXcQ
    embed                   https://www.youtube.com/embed/dQw4w9WgXcQ
    partial path            youtube.com/watch?v=dQw4w9WgXcQ

A single input (str) or an iterable of inputs may be passed. Duplicates are removed,
order preserved; invalid/blank entries raise ValueError / are dropped respectively.
"""
from __future__ import annotations

import re
from typing import List, Optional, Sequence, Union

# A YouTube watch id is exactly 11 chars from [A-Za-z0-9_-]. Casing is preserved as-is
# (case-insensitive matching only) so repeated runs stay stable.
WATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# URL forms: optional scheme, then EITHER "://" OR a host (youtube.com/youtu.be),
# then an OPTIONAL watch?v=/shorts-/embed-/ prefix and the 11-char id. The path is
# optional so bare `youtu.be/<id>` and host-only partial paths also match; requiring
# the host means a bare "watch?v=..." without a host does NOT match.
YT_URL_RE = re.compile(
    r"(?:https?:)?"
    r"(?:(?://)|(?:www\.)?(?:youtube\.com|youtu\.be)/)"
    r"(?:watch\?(?:[^&#]*&|#)?v=|shorts?/|embeds?/)?"
    r"([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)


class InputError(ValueError):
    """Raised when a single input cannot be normalized to a watch id."""


def is_valid_watch_id(text: str) -> bool:
    return isinstance(text, str) and WATCH_ID_RE.match(text.strip()) is not None


def extract_watch_id(input_text: Optional[str]) -> str:
    """Normalize one string (URL / partial path / bare id) to its 11-char watch id.

    Raises InputError if the input does not contain a recognizable YouTube id.
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
            "could not extract a YouTube watch id from %r" % (text,)
        )
    return m.group(1)


def normalize_inputs(
    inputs: Union[str, Sequence[Optional[str]], None],
    dedupe: bool = True,
) -> List[str]:
    """Normalize one or many inputs into an ordered list of watch ids.

    A single string is treated as one input (not a sequence). Blank/None items are
    skipped; invalid items raise InputError. With dedupe=True duplicates are removed
    while preserving first-seen order.
    """
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


if __name__ == "__main__":
    # quick smoke test when run directly
    samples = [
        "dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&t=30s",
        "https://youtu.be/dQw4w9WgXcQ",
        "youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "  https://www.youtube.com/watch?v=dQw4w9WgXcQ  ",
    ]
    print("single:")
    for s in samples:
        print("   %r -> %s" % (s, extract_watch_id(s)))
    print("multi (dedupe):")
    ids = ["dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ", None, "  youtube.com/shorts/DQW4W9WGXCQ  "]
    print("   ", normalize_inputs(ids))
