"""Unit tests for naming.py's pure title-parse + stem-naming helpers."""
import os
import shutil
import tempfile
import unittest

from jamly.naming import (
    build_metadata_name,
    build_stem_basename,
    meta_dict,
    parse_title_artist,
    stem_prefix,
)
from jamly import run as run_mod


class ParseTitleArtistTests(unittest.TestCase):
    """Best-effort [Artist] - [Songname] parsing; None when unconfident (fail-safe)."""

    def test_band_dash_song(self):
        self.assertEqual(
            parse_title_artist("Between the Suried and Me - Extremophile Elite (LIVE)"),
            ("Between the Suried and Me", "Extremophile Elite (LIVE)"),
        )

    def test_tilde_separator(self):
        self.assertEqual(parse_title_artist("Solo Artist ~ Remix"), ("Solo Artist", "Remix"))

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(parse_title_artist("   A - B   "), ("A", "B"))

    def test_single_token_has_no_separator(self):
        self.assertIsNone(parse_title_artist("Just a title no separator"))

    def test_paren_only_suffix_without_dash_is_none(self):
        # "(LIVE)" / "(Official Audio)" style: no trusted separator -> fallback, never a wrong name
        self.assertIsNone(parse_title_artist("Song (Live)"))
        self.assertIsNone(parse_title_artist("Official Audio"))

    def test_ft_guest_and_version_tags_without_dash_are_none(self):
        self.assertIsNone(parse_title_artist("ft. Guest Version"))
        self.assertIsNone(parse_title_artist("Remastered 2024"))

    def test_empty_and_missing_input_is_none(self):
        self.assertIsNone(parse_title_artist(""))
        self.assertIsNone(parse_title_artist(None))


class StemPrefixTests(unittest.TestCase):
    """Human prefix: parsed name when confident, bare watch id otherwise."""

    def test_parsed_name_join(self):
        self.assertEqual(stem_prefix("dQw4w9WgXcQ", ("A Band", "Song")), "A Band - Song")

    def test_fallback_is_bare_watch_id(self):
        self.assertEqual(stem_prefix("dQw4w9WgXcQ", None), "dQw4w9WgXcQ")

    def test_special_chars_are_stripped_for_safety(self):
        # a quote in the parsed artist must not survive into the filename
        self.assertEqual(stem_prefix("x", parse_title_artist('A"B - Song')), "AB - Song")


class StemBasenameTests(unittest.TestCase):
    """Full stem filename (no extension; callers append ``.<fmt>``)."""

    def test_watch_id_fallback(self):
        self.assertEqual(build_stem_basename("dQw4w9WgXcQ", None, "drums"), "dQw4w9WgXcQ_drums")

    def test_named_prefix_with_suffix(self):
        self.assertEqual(
            build_stem_basename("vid123", ("Band", "Song"), "no_drums"),
            "Band - Song_no_drums",
        )


class BuildMetadataNameTests(unittest.TestCase):
    """Human-readable output filename carrying the machine key + stage tag."""

    def test_named_with_stage_tag(self):
        name = build_metadata_name("dM8WjgRA6o8", ("Between the Suried and Me", "Extremophile Elite"), "original")
        self.assertEqual(name, "Between the Suried and Me - Extremophile Elite | dM8WjgRA6o8 (<original>)")

    def test_fallback_keeps_bare_id_and_tag(self):
        self.assertEqual(build_metadata_name("dQw4w9WgXcQ", None, "nodrums"), "dQw4w9WgXcQ (<nodrums>)")

    def test_fallback_filename_is_bare_id_and_tag_only(self):
        # no parse -> just the bare id + stage tag (no duplicated machine key)
        self.assertEqual(build_metadata_name("vid", None, "drumonly"), "vid (<drumonly>)")


class MetaDictTests(unittest.TestCase):
    """Source metadata remains traceable when title fields are unavailable."""

    def test_parses_recorded_fields(self):
        d = meta_dict(watch_id="vid1", source_url="https://youtu.be/vid1", title="A - B", artist="A")
        self.assertEqual(d["watch_id"], "vid1")
        self.assertEqual(d["title"], "A - B")
        self.assertEqual(d["artist"], "A")

    def test_unknown_fields_default_to_empty_or_zero(self):
        # unknowns normalize to empty string / zero (never invented values); the raw source stays
        d = meta_dict(watch_id="vid1")
        self.assertEqual(d["source_url"], "")
        self.assertEqual(d["title"], "")
        self.assertEqual(d["artist"], "")
        self.assertEqual(d["duration_seconds"], 0)


class StemOutputPathsNamedTests(unittest.TestCase):
    """End-to-end naming through run.stem_output_paths with a parsed title (real code path)."""

    def test_named_stems_and_meta_in_tempdir(self):
        # isolated temp dir -> no host cwd state; asserts the metadata filename format end to end
        out = tempfile.mkdtemp()
        try:
            drums, nodrums, meta = run_mod.stem_output_paths(out, "vid1", "mp3", parsed=("Band", "Song"))
            self.assertEqual(os.path.basename(drums), "Band - Song [vid1]_drums.mp3")
            self.assertEqual(os.path.basename(nodrums), "Band - Song [vid1]_no_drums.mp3")
            self.assertEqual(os.path.basename(meta), "metadata.json")
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_long_title_never_truncates_stable_id(self):
        drums, nodrums, _meta = run_mod.stem_output_paths(
            "/out",
            "dQw4w9WgXcQ",
            "wav",
            parsed=("A" * 120, "B" * 120),
        )
        self.assertIn("[dQw4w9WgXcQ]", os.path.basename(drums))
        self.assertIn("[dQw4w9WgXcQ]", os.path.basename(nodrums))


if __name__ == "__main__":
    unittest.main()
