from pathlib import PurePosixPath

import pytest

from tagdrifter.checks import (
    REGISTRY,
    AlbumContext,
    Check,
    Scope,
    TrackContext,
    build_checks,
    register,
)
from tagdrifter.checks.builtin_tags import InconsistentAlbumTags, InvalidMbid, RequiredTags
from tagdrifter.config import Settings
from tagdrifter.core import compute_target

from .conftest import make_meta

SETTINGS = Settings()


def track_ctx(path: str = "Pixies/Doolittle/x.flac", **tags) -> TrackContext:
    meta = make_meta(path, **tags)
    return TrackContext(PurePosixPath(path), meta, compute_target(meta, "Pixies"), SETTINGS)


def test_required_tags_alternatives() -> None:
    check = RequiredTags({"tags": ["albumartist|album artist", "title"]})
    assert list(check.run(track_ctx(**{"album artist": "Pixies", "title": "T"}))) == []
    (issue,) = check.run(track_ctx(title="T"))
    assert issue.details["missing"] == ["albumartist"]


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("44444444-4444-4444-8444-444444444444", True),
        ("44444444-4444-4444-8444-444444444444; 55555555-5555-4555-8555-555555555555", True),
        ("44444444-4444-4444-8444-44444444444", False),
        ("not-a-uuid", False),
    ],
)
def test_invalid_mbid(value, valid) -> None:
    issues = list(InvalidMbid().run(track_ctx(musicbrainz_artistid=value)))
    assert (not issues) == valid


def test_inconsistent_album_tags() -> None:
    a = track_ctx("P/A/1.flac", albumartist="Pixies", album="A")
    b = track_ctx("P/A/2.flac", albumartist="Pixies", album="B")
    album = AlbumContext(PurePosixPath("P/A"), (a, b), (), (), SETTINGS)
    (issue,) = InconsistentAlbumTags().run(album)
    assert issue.details["values"] == {"album": ["A", "B"]}


def test_register_rejects_bad_checks() -> None:
    with pytest.raises(TypeError):

        @register
        class NoId(Check):
            scope = Scope.TRACK

    with pytest.raises(ValueError):

        @register
        class Dup(Check):
            id = "tags.required"
            scope = Scope.TRACK


def test_custom_check_is_picked_up(monkeypatch) -> None:
    monkeypatch.setattr("tagdrifter.checks.base.REGISTRY", dict(REGISTRY))
    from tagdrifter.checks import base

    @base.register
    class Custom(Check):
        id = "custom.example"
        scope = Scope.TRACK

        def run(self, ctx):
            yield self.issue(ctx.rel_path, "hello")

    ids = {c.id for c in base.build_checks(SETTINGS)}
    assert "custom.example" in ids


def test_unknown_disabled_check_is_an_error() -> None:
    with pytest.raises(ValueError):
        build_checks(Settings(disabled_checks=frozenset({"nope"})))
