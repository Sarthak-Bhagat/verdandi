"""Tier 0: pure filename identity parse, exercised against the real library."""

from pathlib import Path

import pytest

from scrobd.tier0 import parse

# 222 filenames captured from the real library on 2026-09-20. It lives under docs/,
# which is deliberately not committed -- it is a list of everything its owner watches.
# These three tests are the strongest evidence tier 0 works, so they skip loudly rather
# than silently pass when the corpus is absent on a fresh clone.
CORPUS = Path(__file__).parent.parent / "docs" / "superpowers" / "library-filenames.txt"
needs_corpus = pytest.mark.skipif(
    not CORPUS.exists(),
    reason=f"real-library corpus not present at {CORPUS} (not committed; see README)",
)


def test_standard_episode():
    r = parse(
        "/m/TV_Shows/Spooky in Love (2026) [tvdbid-466998]/Season 01/"
        "Spooky in Love (2026) [tvdbid-466998] - S01E02 - Episode 2 "
        "[WEBDL-1080p] [Marco].mkv"
    )
    assert r.kind == "episode"
    assert r.ids == {"tvdb": 466998}
    assert (r.season, r.episode, r.absolute) == (1, 2, None)
    assert r.confidence == "exact"
    assert r.source == "filename"


def test_anime_episode_keeps_absolute_number():
    r = parse(
        "Gintama - Mr. Ginpachi's Zany Class (2025) [tvdbid-465543] - S01E12 - 012 - "
        "Old Times Talk [WEBRip-1080p] [Erai-raws].mkv"
    )
    assert (r.season, r.episode, r.absolute) == (1, 12, 12)


def test_specials_are_season_zero():
    r = parse("Dimension 20 (2018) [tvdbid-354216] - S00E69 - Live Viva Más Vegas [SDTV].mp4")
    assert (r.season, r.episode) == (0, 69)
    assert r.kind == "episode"


def test_movie():
    r = parse(
        "/m/Movies/Suzume (2022) [imdbid-tt16428256]/"
        "Suzume (2022) [imdbid-tt16428256] [Bluray-1080p].mkv"
    )
    assert r.kind == "movie"
    assert r.ids == {"imdb": "tt16428256"}
    assert (r.season, r.episode) == (None, None)
    assert r.confidence == "exact"


def test_id_is_read_from_the_folder_when_the_file_lacks_one():
    """A file separated from its folder keeps its own id; the reverse also works."""
    r = parse("/m/Movies/Your Name. (2016) [imdbid-tt5311514]/Your Name. (2016).mkv")
    assert r.ids == {"imdb": "tt5311514"}


def test_flattened_trash_file_still_resolves():
    """Trash from mediactl drops the folder. The id must survive in the filename alone."""
    r = parse(
        "/m/TV_Shows/.Trash-1002/files/"
        "Mushoku Tensei (2021) [tvdbid-361013] - S03E13 - 062 - The Diary [WEBDL-1080p].mkv"
    )
    assert r.ids == {"tvdb": 361013}
    assert (r.season, r.episode, r.absolute) == (3, 13, 62)


def test_empty_tag_is_not_an_id():
    """[imdb-] was a real fault in this library. It must not parse as an id."""
    r = parse("The Affair (2026) [imdb-] - S01E01 - Cheater A [WEBDL-1080p].mkv")
    assert r.ids == {}
    assert r.confidence == "none"


def test_no_tag_at_all_returns_unknown():
    r = parse("Some Random Download 1080p x264.mkv")
    assert r.kind == "unknown"
    assert r.confidence == "none"


def test_episode_numbers_without_an_id_is_not_a_partial_answer():
    r = parse("Some Show - S02E05 - Title.mkv")
    assert r.confidence == "none"


# Two files in the library carry no tag, both verified 2026-09-20:
#   output_2k.mkv                     a 0-byte transcode artefact Radarr never imported
#   "The Affair … -DUSKLiGHT.mkv"     a duplicate Sonarr never imported
# Both sit in folders the index claims, so tier 1 resolves them -- which is
# precisely why tier 1 exists. Named here rather than tolerated by a threshold,
# so a third unmanaged file fails this test and tells you the library drifted.
KNOWN_UNTAGGED = {
    "output_2k.mkv",
    (
        "The Affair Was Just the Beginning (2026) - S01E01 - Cheater A "
        "[WEBDL-1080p][AAC 2.0][KO][h264]-DUSKLiGHT.mkv"
    ),
}


@needs_corpus
def test_every_tagged_library_file_resolves_exactly():
    """The whole point of the rename. If this regresses, tier 0 has stopped working."""
    names = [n for n in CORPUS.read_text().splitlines() if n.strip()]
    assert len(names) >= 200, "corpus missing"
    failures = [n for n in names if n not in KNOWN_UNTAGGED and parse(n).confidence != "exact"]
    assert failures == [], f"{len(failures)} of {len(names)} did not resolve: {failures[:5]}"


@needs_corpus
def test_the_untagged_files_are_still_exactly_the_two_we_know_about():
    names = [n for n in CORPUS.read_text().splitlines() if n.strip()]
    untagged = {n for n in names if parse(n).confidence != "exact"}
    assert untagged == KNOWN_UNTAGGED, f"library drifted: {untagged ^ KNOWN_UNTAGGED}"


@needs_corpus
def test_no_real_file_is_misclassified():
    names = [n for n in CORPUS.read_text().splitlines() if n.strip()]
    for n in names:
        r = parse(n)
        if r.kind == "episode":
            assert "tvdb" in r.ids, n
            assert r.season is not None, n
            assert r.episode is not None, n
        elif r.kind == "movie":
            assert "imdb" in r.ids or "tmdb" in r.ids, n


# --- audit 2026-09-21: the first tag wins, and `absolute` is not any bare number ---


def test_the_first_id_tag_wins_not_the_last():
    """Sonarr writes the real tag right after the series title; later ones are episode text."""
    r = parse("Show [tvdbid-111] - S01E01 - A Tribute to [tvdbid-999].mkv")
    assert r.ids == {"tvdb": 111}
    assert r.confidence == "exact"


def test_the_first_id_tag_in_the_folder_fallback_wins_too():
    """The series folder is the shallowest tagged ancestor, so the earliest match wins."""
    r = parse(
        "/m/TV_Shows/Show (2020) [tvdbid-111]/Season 01/Decoy [tvdbid-999]/ep - S01E01 - t.mkv"
    )
    assert r.ids == {"tvdb": 111}


def test_a_resolution_is_not_an_absolute_number():
    """`[WEBRip-1080p]` contains 1080; a sink keying off absolute would scrobble episode 1080."""
    r = parse("Show (2020) [tvdbid-1] - S01E05 - 1080 - Title [WEBRip-1080p].mkv")
    assert r.absolute is None
    assert (r.season, r.episode) == (1, 5)


def test_a_year_is_not_an_absolute_number():
    r = parse("Show (2020) [tvdbid-1] - S01E05 - 2024 - Title [WEBDL-1080p].mkv")
    assert r.absolute is None


def test_a_4k_resolution_is_not_an_absolute_number():
    r = parse("Show (2020) [tvdbid-1] - S01E05 - 2160 - Title [WEBDL-2160p].mkv")
    assert r.absolute is None


def test_a_real_absolute_number_still_survives_a_quality_tag():
    """The heuristic must not cost the library its 33 genuine absolute numbers."""
    r = parse(
        "Nukitashi THE ANIMATION (2025) [tvdbid-445390] - S01E05 - 005 - "
        "Anti-Copulation Front [WEBRip-1080p] [EMBER].mkv"
    )
    assert r.absolute == 5
