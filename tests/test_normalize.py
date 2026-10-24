"""Unit tests for normalize.py -- pure-Python, no network/subprocess."""
import os
import sys
import unittest


# src-layout: make the ``auto_convert`` package importable for both `pytest` and
# `python -m unittest discover -s tests` without an editable install. Inserted before any
# absolute-package import so it always runs first.
import sys as _shim_sys  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from auto_convert.normalize import (  # noqa: E402
    InputError, WATCH_ID_RE, YT_URL_RE, extract_watch_id, is_valid_watch_id,
    normalize_inputs,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
