"""Human-readable output prefixes and source metadata."""
from __future__ import annotations

from typing import Optional, Tuple

# The "X - Y" separators we trust (ASCII-only). A bare dash without surrounding spaces is usually
# an em-dash/typo inside a single token and splitting on it would corrupt the artist name.
_SEPARATORS = (" - ", "~")


def parse_title_artist(title: Optional[str]) -> Optional[Tuple[str, str]]:
    """Return ``(artist, songname)`` when ``title`` confidently matches "[Artist] - [Songname]",
    else ``None``.

    The match is deliberately conservative so we never emit a *wrong* name: both halves must be
    non-empty after stripping (so "Song-" with no artist is rejected), the artist may not contain
    a tab/newline and is bounded in length, and only the common " - " / "~" forms split. Titles
    like "Song (LIVE)", "Official Audio", "ft. guest" or bare uploads return ``None`` -> fallback.

    The returned halves are NOT sanitized; callers sanitize before writing to disk."""
    if not title or "\n" in title:
        return None
    t = title.strip()
    for sep in _SEPARATORS:
        parts = t.split(sep, 1)
        artist = parts[0].strip()
        song = parts[1].strip() if len(parts) > 1 else ""
        # both halves required; a tab/whitespace-laden "artist" almost always means the split was wrong
        if not (artist and song) or "\t" in artist:
            continue
        if len(artist) > 80:
            continue
        return (artist, song)
    return None


def stem_prefix(wid: str, parsed: Optional[Tuple[str, str]]) -> str:
    """Build the human-facing prefix for one stem.

    With a confident parse this is ``"<Artist> - <Songname>"``; otherwise it degrades to the bare
    watch id so outputs remain uniquely traceable on every platform (URL-safe, no spaces/specials)."""
    from .core import sanitize_filename

    if parsed:
        artist, song = parsed
        return "%s - %s" % (sanitize_filename(artist), sanitize_filename(song))
    return sanitize_filename(wid)


def build_stem_basename(wid: str, parsed: Optional[Tuple[str, str]], suffix: str) -> str:
    """Full stem filename for one stage (``drums`` / ``no_drums``)."""
    return "%s_%s" % (stem_prefix(wid, parsed), suffix)


def build_metadata_name(
    wid: str,
    parsed: Optional[Tuple[str, str]],
    kind: str,
) -> str:
    """Human-readable display label carrying the stable id and stage tag.

    When parsed -> ``"<Artist> - <Songname> | <vid> (<original>|<nodrums>|<drumonly>)"`` so the human
    name stays distinct from the stable id; when unparsed there is no separate human name."""
    prefix = stem_prefix(wid, parsed)
    tag = {
        "original": "<original>",
        "nodrums": "<nodrums>",
        "drumonly": "<drumonly>",
    }.get(kind, kind)
    if parsed is not None:
        return "%s | %s (%s)" % (prefix, wid[:16], tag)
    return "%s (%s)" % (wid, tag)


def meta_dict(
    *,
    watch_id: str,
    source_url: Optional[str] = None,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    uploader: Optional[str] = None,
    duration: Optional[int] = None,
) -> dict:
    """Source fields for one track's metadata.json.

    Records the parsed metadata when available and always the watch id + source text so outputs
    stay traceable even when YouTube exposes no usable title/artist."""
    return {
        "watch_id": watch_id,
        "source_url": source_url or "",
        "title": title or "",
        "artist": artist or "",
        "uploader": uploader or "",
        "duration_seconds": duration if duration is not None else 0,
    }
