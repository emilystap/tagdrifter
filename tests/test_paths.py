import unicodedata

from tagdrifter.core import PathMatch, compare_paths, find_case_collisions, is_mismatch


def test_compare_paths() -> None:
    assert compare_paths("A/B.flac", "A/B.flac") is PathMatch.EXACT
    assert compare_paths("A/b.flac", "A/B.flac") is PathMatch.CASE_ONLY
    assert compare_paths("A/C.flac", "A/B.flac") is PathMatch.DIFFERENT


def test_unicode_forms_compare_equal() -> None:
    nfd = unicodedata.normalize("NFD", "Beyoncé/x.flac")
    assert compare_paths(nfd, "Beyoncé/x.flac") is PathMatch.EXACT


def test_case_only_mismatch_follows_setting() -> None:
    assert is_mismatch(PathMatch.CASE_ONLY, case_sensitive=True)
    assert not is_mismatch(PathMatch.CASE_ONLY, case_sensitive=False)
    assert is_mismatch(PathMatch.DIFFERENT, case_sensitive=False)
    assert not is_mismatch(PathMatch.EXACT, case_sensitive=True)


def test_find_case_collisions() -> None:
    names = ["Abbey Road", "Abbey road", "Let It Be", "Help!"]
    assert find_case_collisions(names) == [["Abbey Road", "Abbey road"]]
    assert find_case_collisions(["Help!", "Let It Be"]) == []
