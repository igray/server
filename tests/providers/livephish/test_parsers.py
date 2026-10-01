"""Test the LivePhish parsers."""

from __future__ import annotations

from music_assistant.providers.livephish.parsers import (
    parse_container,
    parse_container_track,
    parse_playlist_track,
    split_track_item_id,
)

CONTAINER = {
    "containerID": 1234,
    "containerInfo": "Junta",
    "artistID": 62,
    "artistName": "Phish",
    "releaseDateFormatted": "7/24/2026",
}


def test_container_year_from_us_release_date() -> None:
    """A container without a performance date takes its year from the release date."""
    assert parse_container("lp", "livephish", CONTAINER).year == 2026


def test_container_year_with_null_release_date() -> None:
    """A null release date yields no year instead of raising."""
    container = {**CONTAINER, "releaseDateFormatted": None}
    assert parse_container("lp", "livephish", container).year is None


def test_container_track_id_carries_album_id() -> None:
    """Container tracks can be resolved back to their album and LivePhish track id."""
    album = parse_container("lp", "livephish", CONTAINER)
    track = parse_container_track("lp", "livephish", {"trackID": 99, "songTitle": "Fee"}, album)
    assert track.item_id == "1234_99"
    assert split_track_item_id(track.item_id) == ("1234", "99")


def test_playlist_track_without_release_keeps_bare_id() -> None:
    """A playlist track without a release id keeps the bare LivePhish track id."""
    track = parse_playlist_track(
        "lp", "livephish", {"trackId": 99, "name": "Fee", "artist": {"id": 62, "name": "Phish"}}
    )
    assert track.item_id == "99"
    assert split_track_item_id(track.item_id) == (None, "99")
