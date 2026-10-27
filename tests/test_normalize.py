"""Unit tests for normalize.py -- pure-Python, no network/subprocess."""
import os
import sys
import unittest


# src-layout: make the ``jamly`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from jamly.normalize import (  # noqa: E402
    InputError, WATCH_ID_RE, extract_watch_id,
    make_input, normalize_inputs, normalize_inputs_full,
)


class WatchIdPatternTests(unittest.TestCase):
    def test_valid_ids(self):
        for i in ["dQw4w9WgXcQ", "aB3-_xZ90qR", "0" * 11, "z" * 11]:
            self.assertTrue(WATCH_ID_RE.match(i), i)

    def test_invalid_ids(self):
        for i in ["", "tooShort", "dQw4w9WgXcQx", "has space", "bad?char", "1234567890"]:
            self.assertFalse(WATCH_ID_RE.match(i), i)


class ExtractTests(unittest.TestCase):
    def _ok(self, text, expected):
        self.assertEqual(extract_watch_id(text), expected)

    def test_bare_id_and_case(self):
        self._ok("dQw4w9WgXcQ", "dQw4w9WgXcQ")
        self._ok("DQW4W9WGXCQ", "DQW4W9WGXCQ")  # case preserved

    def test_full_url(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&t=30s"
        self._ok(url, "dQw4w9WgXcQ")

    def test_youtu_be(self):
        self._ok("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ")
        self._ok("http://youtu.be/DQW4W9WGXCQ", "DQW4W9WGXCQ")

    def test_host_only_no_scheme(self):
        # plan lists `youtube.com/watch?v=ID` as an accepted partial path form
        self._ok("youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ")
        self._ok("youtube.com/shorts/DQW4W9WGXCQ", "DQW4W9WGXCQ")

    def test_shorts_and_embed(self):
        self._ok("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ")
        self._ok("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ")

    def test_whitespace_and_trailing(self):
        self._ok("  https://youtu.be/dQw4w9WgXcQ  \n", "dQw4w9WgXcQ")

    def test_invalid_raises(self):
        for bad in [None, "", "   ", "not-a-link-123", "tooShort", "https://example.com/foo"]:
            with self.assertRaises(InputError):
                extract_watch_id(bad)


class NormalizeInputsTests(unittest.TestCase):
    def test_dedupe_preserves_order(self):
        ids = normalize_inputs([
            "dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",          # dup of first (case-insensitive)
            None,                                      # skipped
            "",                                        # skipped
            "  youtube.com/shorts/DQW4W9WGXCQ  ",     # second unique id
        ])
        self.assertEqual(ids, ["dQw4w9WgXcQ", "DQW4W9WGXCQ"])

    def test_single_string_is_one_input(self):
        self.assertEqual(normalize_inputs("https://youtu.be/dQw4w9WgXcQ"), ["dQw4w9WgXcQ"])

    def test_none_returns_empty(self):
        self.assertEqual(normalize_inputs(None), [])

    def test_invalid_item_raises(self):
        with self.assertRaises(InputError):
            normalize_inputs(["good", "not-a-link-123"])

    def test_non_string_raises(self):
        with self.assertRaises(InputError):
            normalize_inputs([123])


class MultiHostExtractionTests(unittest.TestCase):
    """Broader input support: any host yt-dlp understands, plus partial paths without a scheme."""

    def _ok(self, text, expected):
        self.assertEqual(extract_watch_id(text), expected)

    def test_other_hosts_resolve_any_11_char_id(self):
        # the id is host-agnostic -- Vimeo/Bandcamp/any yt-dlp-supported site resolves.
        # 11-char tokens (a real Vimeo id is exactly 11 chars; '123456789' alone would be only 9).
        self._ok("https://vimeo.com/12345678901", "12345678901")
        self._ok("https://bandcamp.com/abcDEF12345", "abcDEF12345")

    def test_partial_path_without_scheme(self):
        # a bare host+path (no http) is accepted, matching how tools are often pasted as plain links.
        # a bare host+path (no http) is accepted, matching how tools are often pasted as plain links.
        self._ok("vimeo.com/12345678901", "12345678901")
        self._ok("bandcamp.com/abcDEF12345", "abcDEF12345")

    def test_no_scheme_still_needs_11_char_id(self):
        # without a recognizable id in the path, an arbitrary host URL is rejected.
        with self.assertRaises(InputError):
            extract_watch_id("https://example.com/some/short/path")

    def test_full_url_passthrough_source(self):
        inp = make_input("  https://vimeo.com/12345678901  ")
        self.assertEqual(inp.watch_id, "12345678901")
        # source keeps the cleaned original so yt-dlp receives a full URL for any host
        self.assertEqual(inp.source_url, "https://vimeo.com/12345678901")

    def test_bare_id_source_passthrough(self):
        inp = make_input("dQw4w9WgXcQ")
        self.assertEqual((inp.watch_id, inp.source_url), ("dQw4w9WgXcQ", "dQw4w9WgXcQ"))

    def test_normalize_inputs_full_carries_source(self):
        full = normalize_inputs_full([
            "https://vimeo.com/12345678901",
            "youtube.com/watch?v=dQw4w9WgXcQ",
            None,                                       # skipped
            "aBcDeFg1234",                              # distinct bare id (dQw4w9WgXcQ would dedup)
        ])
        self.assertEqual(
            [(i.watch_id, i.source_url) for i in full],
            [
                ("12345678901", "https://vimeo.com/12345678901"),
                ("dQw4w9WgXcQ", "youtube.com/watch?v=dQw4w9WgXcQ"),
                ("aBcDeFg1234", "aBcDeFg1234"),
            ],
        )

    def test_normalize_inputs_full_dedupe(self):
        full = normalize_inputs_full([
            "https://youtu.be/dQw4w9WgXcQ",
            "youtube.com/watch?v=dQw4w9WgXcQ",         # same id, dropped
        ])
        self.assertEqual(len(full), 1)
        self.assertEqual(full[0].watch_id, "dQw4w9WgXcQ")
        self.assertEqual(full[0].source_url, "https://youtu.be/dQw4w9WgXcQ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
