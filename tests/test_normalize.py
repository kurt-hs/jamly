"""Unit tests for normalize.py -- pure-Python, no network/subprocess."""
import unittest


from jamly.normalize import (
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
        self._ok("youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ")
        self._ok("youtube.com/shorts/DQW4W9WGXCQ", "DQW4W9WGXCQ")

    def test_shorts_and_embed(self):
        self._ok("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ")
        self._ok("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ")

    def test_completed_live_urls_use_the_same_video_id(self):
        self._ok("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ")

    def test_non_youtube_hosts_cannot_borrow_a_youtube_id(self):
        with self.assertRaises(InputError):
            extract_watch_id("https://youtube.com.example.test/watch?v=dQw4w9WgXcQ")

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
            "https://youtu.be/dQw4w9WgXcQ",          # same case-sensitive video id
            None,                                      # skipped
            "",                                        # skipped
            "  youtube.com/shorts/DQW4W9WGXCQ  ",     # second unique id
        ])
        self.assertEqual(ids, ["dQw4w9WgXcQ", "DQW4W9WGXCQ"])

    def test_single_string_is_one_input(self):
        self.assertEqual(normalize_inputs("https://youtu.be/dQw4w9WgXcQ"), ["dQw4w9WgXcQ"])

    def test_none_returns_empty(self):
        self.assertEqual(normalize_inputs(None), [])

    def test_non_youtube_sources_receive_stable_ids(self):
        first = normalize_inputs(["https://soundcloud.com/artist/song"])
        second = normalize_inputs(["https://soundcloud.com/artist/song"])
        self.assertEqual(first, second)
        self.assertTrue(first[0].startswith("src-"))

    def test_non_string_raises(self):
        with self.assertRaises(InputError):
            normalize_inputs([123])


class MultiHostExtractionTests(unittest.TestCase):
    """Non-YouTube sources pass through with a stable, filesystem-safe id."""

    def test_other_hosts_use_hash_ids(self):
        for source in (
            "https://vimeo.com/123456789",
            "https://soundcloud.com/artist/song-name",
            "ytsearch1:artist song",
        ):
            inp = make_input(source)
            self.assertTrue(inp.watch_id.startswith("src-"))
            self.assertEqual(inp.source_url, source)

    def test_same_source_has_same_id(self):
        source = "https://bandcamp.com/track/example"
        self.assertEqual(make_input(source).watch_id, make_input(source).watch_id)

    def test_full_url_passthrough_source(self):
        inp = make_input("  https://vimeo.com/12345678901  ")
        self.assertTrue(inp.watch_id.startswith("src-"))
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
                (make_input("https://vimeo.com/12345678901").watch_id,
                 "https://vimeo.com/12345678901"),
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
