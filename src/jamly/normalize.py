"""Stable filesystem-safe ids with the original media source retained separately."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

WATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class InputError(ValueError):
    pass


@dataclass(frozen=True)
class Input:
    watch_id: str
    source_url: str


def is_valid_watch_id(text: str) -> bool:
    return isinstance(text, str) and WATCH_ID_RE.fullmatch(text.strip()) is not None


def extract_watch_id(input_text: str | None) -> str:
    if not isinstance(input_text, str) or not input_text.strip():
        raise InputError("Expected a non-empty source")
    text = input_text.strip()
    if is_valid_watch_id(text):
        return text
    url = urlsplit(text if "://" in text else "https://" + text)
    host = (url.hostname or "").lower()
    parts = url.path.strip("/").split("/")
    candidate = ""
    if host in ("youtu.be", "www.youtu.be"):
        candidate = parts[0]
    elif host == "youtube.com" or host.endswith(".youtube.com") or host == "www.youtube-nocookie.com":
        if parts[0] == "watch":
            candidate = parse_qs(url.query).get("v", [""])[0]
        elif parts[0] in ("shorts", "embed", "live") and len(parts) == 2:
            candidate = parts[1]
    if not is_valid_watch_id(candidate):
        raise InputError("No YouTube video id in source")
    return candidate


def make_input(input_text: str | None) -> Input:
    if not isinstance(input_text, str) or not input_text.strip():
        raise InputError("Expected a non-empty source string")
    source = input_text.strip()
    if any(ord(char) < 32 for char in source):
        raise InputError("A source must not contain control characters")
    try:
        wid = extract_watch_id(source)
    except InputError:
        wid = "src-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return Input(wid, source)


def normalize_inputs_full(inputs, dedupe: bool = True) -> list[Input]:
    sources = [inputs] if isinstance(inputs, str) else list(inputs or [])
    output = []
    seen = set()
    for source in sources:
        if source is None or isinstance(source, str) and not source.strip():
            continue
        item = make_input(source)
        if not dedupe or item.watch_id not in seen:
            output.append(item)
            seen.add(item.watch_id)
    return output


def normalize_inputs(inputs, dedupe: bool = True) -> list[str]:
    return [item.watch_id for item in normalize_inputs_full(inputs, dedupe)]
