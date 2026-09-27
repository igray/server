"""Parsers that turn LivePhish API objects into Music Assistant models."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import AlbumType, ImageType, MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
)

from music_assistant.helpers.util import infer_album_type, parse_title_and_version

from .constants import IMAGE_BASE_URL


def image_url(path: str | None) -> str | None:
    """
    Return an absolute image url for an image path from any of the LivePhish APIs.

    :param path: Absolute url or a path relative to the LivePhish asset host.
    """
    if not path:
        return None
    if path.startswith("http"):
        return path
    return f"{IMAGE_BASE_URL}{path}"


def parse_year(date_str: str | None) -> int | None:
    """
    Return the year from an ISO (2024-04-18T00:00:00) or US (7/24/2026) date string.

    :param date_str: Date string as returned by the LivePhish APIs.
    """
    if not date_str:
        return None
    part = date_str.split("-")[0] if "-" in date_str else date_str.split("/")[-1]
    return int(part) if part.isdigit() and len(part) == 4 else None


def parse_artist(provider: str, domain: str, artist_obj: dict[str, Any]) -> Artist:
    """
    Parse a LivePhish artist object (legacy or catalog API).

    :param provider: Instance id of the provider.
    :param domain: Domain of the provider.
    :param artist_obj: Artist object from the API.
    """
    # artistImage is the same nugs placeholder for every artist, so metadata providers supply art
    artist_id = str(artist_obj.get("artistID") or artist_obj.get("id"))
    return Artist(
        item_id=artist_id,
        provider=provider,
        name=str(artist_obj.get("artistName") or artist_obj.get("name")),
        provider_mappings={
            ProviderMapping(item_id=artist_id, provider_domain=domain, provider_instance=provider)
        },
    )


def parse_release(provider: str, domain: str, release: dict[str, Any]) -> Album:
    """
    Parse a release object from the catalog or stash API.

    :param provider: Instance id of the provider.
    :param domain: Domain of the provider.
    :param release: Release object (show or album).
    """
    album = _new_album(provider, domain, str(release["id"]), str(release["title"]))
    if (artist := release.get("artist")) and artist.get("id"):
        album.artists.append(parse_artist(provider, domain, artist))
    if url := image_url((release.get("image") or {}).get("url") or release.get("coverImage")):
        album.metadata.add_image(_image(provider, url))
    album.year = parse_year(release.get("performanceDate") or release.get("albumReleaseDate"))
    album.album_type = (
        AlbumType.LIVE
        if release.get("type") == "show"
        else infer_album_type(album.name, album.version)
    )
    return album


def parse_container(provider: str, domain: str, container: dict[str, Any]) -> Album:
    """
    Parse a container object from the legacy API or the catalog shows endpoint.

    :param provider: Instance id of the provider.
    :param domain: Domain of the provider.
    :param container: Container object (show or album).
    """
    album = _new_album(
        provider, domain, str(container["containerID"]), str(container["containerInfo"]).strip()
    )
    if container.get("artistID"):
        album.artists.append(
            parse_artist(
                provider,
                domain,
                {"id": container["artistID"], "name": container["artistName"]},
            )
        )
    if url := image_url((container.get("img") or {}).get("url")):
        album.metadata.add_image(_image(provider, url))
    album.year = parse_year(container.get("performanceDate")) or parse_year(
        container.get("releaseDateFormatted", "").replace("/", "-")
    )
    album.album_type = (
        AlbumType.LIVE
        if container.get("containerTypeStr") == "Show"
        else infer_album_type(album.name, album.version)
    )
    return album


def parse_container_track(
    provider: str,
    domain: str,
    track_obj: dict[str, Any],
    album: Album,
) -> Track:
    """
    Parse a track (or song) object that belongs to a container.

    :param provider: Instance id of the provider.
    :param domain: Domain of the provider.
    :param track_obj: Track object from a container.
    :param album: The album the track belongs to.
    """
    track = _new_track(provider, domain, str(track_obj["trackID"]), str(track_obj["songTitle"]))
    track.artists.extend(album.artists)
    track.album = album
    for image in album.metadata.images or []:
        track.metadata.add_image(image)
    if duration := track_obj.get("totalRunningTime"):
        track.duration = int(duration)
    track.disc_number = int(track_obj.get("discNum") or 0)
    track.track_number = int(track_obj.get("trackNum") or 0)
    if not track.artists:
        raise InvalidDataError("Track is missing artists")
    return track


def parse_playlist_track(provider: str, domain: str, track_obj: dict[str, Any]) -> Track:
    """
    Parse an item from the playlist-tracks endpoint.

    :param provider: Instance id of the provider.
    :param domain: Domain of the provider.
    :param track_obj: Playlist track object.
    """
    track = _new_track(provider, domain, str(track_obj["trackId"]), str(track_obj["name"]))
    artist = track_obj.get("artist") or {}
    if not artist.get("id"):
        raise InvalidDataError("Track is missing artists")
    track.artists.append(
        ItemMapping(
            media_type=MediaType.ARTIST,
            item_id=str(artist["id"]),
            provider=provider,
            name=artist["name"],
        )
    )
    if track_obj.get("releaseId"):
        venue = (track_obj.get("venue") or {}).get("title") or ""
        track.album = ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=str(track_obj["releaseId"]),
            provider=provider,
            name=track_obj.get("albumTitle") or venue or str(track_obj["releaseId"]),
        )
    if url := image_url((track_obj.get("image") or {}).get("url")):
        track.metadata.add_image(_image(provider, url))
    if duration := track_obj.get("durationSeconds"):
        track.duration = int(duration)
    return track


def parse_playlist(provider: str, domain: str, playlist_obj: dict[str, Any]) -> Playlist:
    """
    Parse a user playlist object from the stash API.

    :param provider: Instance id of the provider.
    :param domain: Domain of the provider.
    :param playlist_obj: Playlist object.
    """
    playlist_id = str(playlist_obj["id"])
    playlist = Playlist(
        item_id=playlist_id,
        provider=provider,
        name=str(playlist_obj["name"]).strip(),
        provider_mappings={
            ProviderMapping(item_id=playlist_id, provider_domain=domain, provider_instance=provider)
        },
        is_editable=False,
    )
    if url := image_url(playlist_obj.get("imageUrl")):
        playlist.metadata.add_image(_image(provider, url))
    return playlist


def _new_album(provider: str, domain: str, item_id: str, title: str) -> Album:
    name, version = parse_title_and_version(title)
    return Album(
        item_id=item_id,
        provider=provider,
        name=name,
        version=version,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain=domain, provider_instance=provider)
        },
    )


def _new_track(provider: str, domain: str, item_id: str, title: str) -> Track:
    name, version = parse_title_and_version(title)
    return Track(
        item_id=item_id,
        provider=provider,
        name=name,
        version=version,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=domain,
                provider_instance=provider,
                available=True,
            )
        },
    )


def _image(provider: str, url: str) -> MediaItemImage:
    return MediaItemImage(
        type=ImageType.THUMB, path=url, provider=provider, remotely_accessible=True
    )
