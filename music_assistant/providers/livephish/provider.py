"""LivePhish music provider for Music Assistant."""

from __future__ import annotations

import asyncio
import base64
import json
from time import time
from typing import TYPE_CHECKING, Any

from aiohttp import ClientTimeout
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    AudioError,
    LoginFailed,
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    Playlist,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER, CONF_PASSWORD, CONF_USERNAME
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    CATALOG_URL,
    CLIENT_ID,
    LEGACY_API_URL,
    PAGE_SIZE,
    PLAYBACK_SESSION_URL,
    PLAYBACK_URL,
    REC_POPULAR,
    REC_RECENT,
    REC_RECENTLY_PLAYED,
    SCOPE,
    STASH_URL,
    SUBSCRIPTIONS_URL,
    TOKEN_EXPIRY_MARGIN,
    TOKEN_URL,
)
from .parsers import (
    parse_artist,
    parse_container,
    parse_container_track,
    parse_playlist,
    parse_playlist_track,
    parse_release,
    split_track_item_id,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.media_items import MediaItemType

TIMEOUT = ClientTimeout(total=30)


class LivePhishProvider(MusicProvider):
    """Provider implementation for LivePhish."""

    throttler: ThrottlerManager
    _access_token: str | None = None
    _refresh_token: str | None = None
    _token_expiry: float = 0
    _token_claims: dict[str, Any]
    _login_lock: asyncio.Lock

    @property
    def max_concurrent_streams(self) -> int:
        """Return the number of source streams Music Assistant may run against LivePhish."""
        # the LivePhish player registers a playback session and stops when another device
        # takes over the account, so only one stream per account can play at a time
        return 1

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return config entries to configure this provider."""
        return (CONF_ENTRY_UNOFFICIAL_PROVIDER,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.throttler = ThrottlerManager(rate_limit=5, period=1)
        self._token_claims = {}
        self._login_lock = asyncio.Lock()
        await self._get_access_token()

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Search LivePhish for artists, shows and song performances.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        result = SearchResults()
        query = search_query.strip()
        artists = await self._get_artists()
        if MediaType.ARTIST in media_types:
            result.artists = [a for a in artists if query.lower() in a.name.lower()][:limit]
        if MediaType.ALBUM not in media_types and MediaType.TRACK not in media_types:
            return result
        songs = await self._search_songs(query)
        filters: dict[str, Any] = {}
        artist_part, _, title_part = query.partition(" - ")
        if not songs and title_part:
            # Music Assistant looks up other versions of a track as "artist - title"
            songs = await self._search_songs(title_part)
            if artist := next(
                (a for a in artists if compare_strings(a.name, artist_part, strict=False)), None
            ):
                filters["artistList"] = artist.item_id
        albums: dict[str, Album] = {}
        tracks: dict[str, Track] = {}
        for title in songs[:limit]:
            if len(albums) >= limit and len(tracks) >= limit:
                break
            containers = await self._legacy(
                "catalog.containersAll",
                songsPlayed=title,
                availType=1,
                limit=limit,
                startOffset=1,
                **filters,
            )
            for container in containers.get("containers") or []:
                album = parse_container(self.instance_id, self.domain, container)
                albums.setdefault(album.item_id, album)
                for song in container.get("songs") or []:
                    if song["songTitle"] == title:
                        track = parse_container_track(self.instance_id, self.domain, song, album)
                        tracks.setdefault(track.item_id, track)
        if MediaType.ALBUM in media_types:
            result.albums = list(albums.values())[:limit]
        if MediaType.TRACK in media_types:
            result.tracks = list(tracks.values())[:limit]
        return result

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve the artists of the shows and albums in the user's stash."""
        seen: set[str] = set()
        async for album in self.get_library_albums():
            for artist in album.artists:
                if isinstance(artist, Artist) and artist.item_id not in seen:
                    seen.add(artist.item_id)
                    yield artist

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve the shows and albums in the user's stash."""
        items = await self._get_all_items(
            f"{STASH_URL}/stash/items",
            itemTypes="audio",
            productTypes="release",
            sorting="purchaseDateDesc",
            includePhysical="true",
        )
        for item in items:
            if release := (item.get("product") or {}).get("release"):
                yield parse_release(self.instance_id, self.domain, release)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve the user's playlists."""
        for item in await self._get_all_items(f"{STASH_URL}/me/playlists", catalogIds="nugs"):
            yield parse_playlist(self.instance_id, self.domain, item)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details by id."""
        for artist in await self._get_artists():
            if artist.item_id == prov_artist_id:
                return artist
        raise MediaNotFoundError(f"Artist {prov_artist_id} not found")

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get all shows and albums for the given artist."""
        items = await self._get_all_items(f"{CATALOG_URL}/releases", artistIds=prov_artist_id)
        return [parse_release(self.instance_id, self.domain, item) for item in items]

    @use_cache(3600 * 24 * 14)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get show or album details by id."""
        container = await self._get_show(prov_album_id)
        return parse_container(self.instance_id, self.domain, container)

    @use_cache(3600 * 24 * 14, allow_expired_cache=True)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all tracks of the given show or album."""
        container = await self._get_show(prov_album_id)
        album = parse_container(self.instance_id, self.domain, container)
        return [
            parse_container_track(self.instance_id, self.domain, item, album)
            for item in container.get("tracks") or []
            if item.get("trackID")
        ]

    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details by id."""
        album_id, _ = split_track_item_id(prov_track_id)
        if album_id:
            album_tracks = await self.get_album_tracks(album_id)
            if track := next((t for t in album_tracks if t.item_id == prov_track_id), None):
                return track
        else:
            cached: Track | None = await self.mass.cache.get(
                self._track_cache_key(prov_track_id), provider=self.instance_id, base_class=Track
            )
            if cached:
                return cached
        raise MediaNotFoundError(f"Track {prov_track_id} not found")

    @use_cache(3600 * 24 * 14)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details by id."""
        response = await self._request("GET", f"{STASH_URL}/me/playlists/{prov_playlist_id}")
        return parse_playlist(self.instance_id, self.domain, response)

    @use_cache(3600, allow_expired_cache=True)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get the tracks of a playlist."""
        if page > 0:
            return []
        response = await self._request(
            "GET", f"{STASH_URL}/me/playlists/{prov_playlist_id}/playlist-tracks/all"
        )
        tracks = []
        for position, item in enumerate(response["items"], 1):
            track = parse_playlist_track(self.instance_id, self.domain, item)
            track.position = position
            tracks.append(track)
        await self._cache_tracks([t for t in tracks if not split_track_item_id(t.item_id)[0]])
        return tracks

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse LivePhish by artist or by year.

        :param path: The path to browse, (e.g. livephish://years/2024).
        """
        parts = path.split("://", 1)[1].split("/") if "://" in path else []
        subpath = parts[0] if parts else ""
        if subpath == "artists":
            return await self._get_artists()
        if subpath == "years" and len(parts) > 1 and parts[1]:
            return await self._get_shows_for_year(parts[1])
        if subpath == "years":
            return [
                BrowseFolder(
                    item_id=year,
                    provider=self.instance_id,
                    path=f"{self.instance_id}://years/{year}",
                    name=year,
                )
                for year in reversed(await self._legacy_cached("catalog.showYearCatalog"))
            ]
        return [
            BrowseFolder(
                item_id="artists",
                provider=self.instance_id,
                path=f"{self.instance_id}://artists",
                name="Artists",
                translation_key="artists",
            ),
            BrowseFolder(
                item_id="years",
                provider=self.instance_id,
                path=f"{self.instance_id}://years",
                name="Browse by Year",
                translation_key="browse_by_year",
            ),
        ]

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's available recommendation rows, without items."""
        return [
            RecommendationFolder(
                name=name, translation_key=key, item_id=key, provider=self.instance_id
            )
            for key, name in (
                (REC_RECENTLY_PLAYED, "Recently Played"),
                (REC_RECENT, "New Releases"),
                (REC_POPULAR, "Most Popular"),
            )
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        folder = await self._get_recommendation_folder(item_id)
        if folder is None:
            return UniqueList()
        return folder.items

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the details needed to stream the given track."""
        subscription = await self._get_subscription()
        if not subscription.get("isContentAccessible"):
            raise AudioError("No active LivePhish subscription found for this account")
        await self._request(
            "POST",
            PLAYBACK_SESSION_URL,
            json_body={
                "userGuid": self._token_claims["legacy_uguid"],
                "userId": int(self._token_claims["sub"]),
                "platform": "web",
            },
        )
        response = await self._request(
            "GET",
            PLAYBACK_URL.format(track_id=split_track_item_id(item_id)[1]),
            params={"formats": "lossy", "forceFlac": "true"},
        )
        if not response.get("url"):
            raise MediaNotFoundError(f"No stream found for track {item_id}")
        if response.get("drm"):
            raise AudioError(f"Track {item_id} is DRM protected")
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            stream_type=StreamType.HTTP,
            path=response["url"],
        )

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Report a finished or stopped track to the LivePhish recently played list."""
        if media_type != MediaType.TRACK or is_playing:
            return
        album_id, track_id = split_track_item_id(prov_item_id)
        if not album_id:
            return
        await self._request(
            "POST",
            f"{CATALOG_URL}/me/recently-played/releases",
            json_body={
                "id": album_id,
                "trackId": track_id,
                "playbackPosition": position,
            },
        )

    async def _get_artists(self) -> list[Artist]:
        """Return all artists in the LivePhish catalog."""
        response = await self._legacy_cached("catalog.artists")
        return [parse_artist(self.instance_id, self.domain, item) for item in response["artists"]]

    @use_cache(3600 * 24)
    async def _get_shows_for_year(self, year: str) -> list[Album]:
        """
        Return all shows performed in the given year, newest first.

        :param year: Four digit year.
        """
        albums: list[Album] = []
        offset = 1
        while True:
            response = await self._legacy(
                "catalog.containersAll",
                showYears=year,
                sortBy="performanceDate",
                sortType="desc",
                limit=PAGE_SIZE,
                startOffset=offset,
            )
            containers = response["containers"] or []
            albums += [parse_container(self.instance_id, self.domain, c) for c in containers]
            if len(containers) < PAGE_SIZE:
                return albums
            offset += PAGE_SIZE

    async def _search_songs(self, query: str) -> list[str]:
        """
        Return the titles of the songs in the LivePhish catalog that match the query.

        :param query: Search query.
        """
        needle = _normalize(query)
        if not needle:
            return []
        titles = [str(song["title"]) for song in await self._legacy_cached("catalog.songCatalog")]
        matches = sorted(
            (title for title in titles if needle in _normalize(title)),
            key=lambda title: (
                _normalize(title) != needle,
                not _normalize(title).startswith(needle),
                len(title),
            ),
        )
        if matches:
            return matches
        response = await self._request("GET", f"{CATALOG_URL}/search", params={"query": query})
        return [song["title"] for song in response["result"].get("songs") or []]

    async def _get_show(self, show_id: str) -> dict[str, Any]:
        """Return the container object of a show or album."""
        response = await self._request("GET", f"{CATALOG_URL}/shows/{show_id}")
        return dict(response["Response"])

    async def _get_subscription(self) -> dict[str, Any]:
        """Return the user's active subscription, or an empty dict when there is none."""
        subscriptions = await self._request("GET", SUBSCRIPTIONS_URL)
        return next((s for s in subscriptions if s.get("isContentAccessible")), {})

    @use_cache(3600 * 4, base_class=RecommendationFolder)
    async def _get_recommendation_folder(self, item_id: str) -> RecommendationFolder | None:
        """
        Fetch a single recommendation row, including its items.

        :param item_id: The item_id of the row (unknown ids yield None).
        """
        folder = next(
            (row for row in await self.get_recommendations() if row.item_id == item_id), None
        )
        if folder is None:
            return None
        if item_id == REC_RECENTLY_PLAYED:
            response = await self._request(
                "GET", f"{CATALOG_URL}/me/recently-played/mixed", params={"limit": 50}
            )
            releases = [i["data"] for i in response["items"] if i.get("contentType") == "release"]
        else:
            endpoint = "releases/popular" if item_id == REC_POPULAR else "releases/recent"
            response = await self._request("GET", f"{CATALOG_URL}/{endpoint}", params={"limit": 50})
            releases = response["items"]
        folder.items = UniqueList(
            [parse_release(self.instance_id, self.domain, release) for release in releases]
        )
        return folder

    async def _cache_tracks(self, tracks: list[Track]) -> None:
        """Cache tracks so get_track can return them later."""
        for track in tracks:
            await self.mass.cache.set(
                self._track_cache_key(track.item_id),
                track.to_dict(),
                expiration=3600 * 24 * 14,
                provider=self.instance_id,
                persistent=True,
            )

    def _track_cache_key(self, track_id: str) -> str:
        return f"livephish_track_{track_id}"

    async def _get_access_token(self) -> str:
        """Return a valid access token, refreshing or logging in when needed."""
        async with self._login_lock:
            if self._access_token and self._token_expiry > time():
                return self._access_token
            token: dict[str, Any] | None = None
            if self._refresh_token:
                token = await self._token_request(
                    {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
                )
            if token is None:
                username = self.get_setup_value(CONF_USERNAME)
                password = self.get_setup_value(CONF_PASSWORD)
                if not username or not password:
                    raise LoginFailed("Missing LivePhish username or password")
                token = await self._token_request(
                    {
                        "grant_type": "password",
                        "username": str(username),
                        "password": str(password),
                        "scope": SCOPE,
                    }
                )
            if token is None:
                raise LoginFailed("Invalid LivePhish username or password")
            self._access_token = str(token["access_token"])
            self._refresh_token = token.get("refresh_token") or self._refresh_token
            self._token_expiry = time() + int(token["expires_in"]) - TOKEN_EXPIRY_MARGIN
            self._token_claims = self._decode_claims(self._access_token)
            return self._access_token

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any] | None:
        """
        Request a token from the LivePhish identity server.

        :param data: Grant specific form fields.
        :return: The token response, or None when the grant was rejected.
        """
        async with self.mass.http_session.post(
            TOKEN_URL, data={**data, "client_id": CLIENT_ID}, timeout=TIMEOUT
        ) as response:
            if response.status in (400, 401):
                return None
            if response.status in (502, 503, 504):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()
            return dict(await response.json())

    def _decode_claims(self, access_token: str) -> dict[str, Any]:
        """Return the claims of a JWT access token without verifying it."""
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return dict(json.loads(base64.urlsafe_b64decode(payload)))

    @use_cache(3600 * 24)
    async def _legacy_cached(self, method: str) -> Any:
        """
        Return the Response object of a parameterless legacy catalog method, cached for a day.

        :param method: The API method, e.g. catalog.songCatalog.
        """
        return await self._legacy(method)

    async def _legacy(self, method: str, **params: Any) -> Any:
        """
        Call the legacy LivePhish catalog API and return its Response object.

        :param method: The API method, e.g. catalog.containersAll.
        :param params: Extra query parameters for the method.
        """
        response = await self._request(
            "GET", LEGACY_API_URL, params={"method": method, **params}, auth=False
        )
        return response["Response"]

    async def _get_all_items(self, url: str, **params: Any) -> list[dict[str, Any]]:
        """
        Return all items of a paginated endpoint that answers with total and items.

        :param url: Endpoint url.
        :param params: Extra query parameters.
        """
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            result = await self._request(
                "GET", url, params={**params, "limit": PAGE_SIZE, "offset": offset}
            )
            items += result["items"]
            offset += PAGE_SIZE
            if offset >= result["total"]:
                return items

    @throttle_with_retries
    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        """
        Perform a throttled request to one of the LivePhish APIs and return the json body.

        :param method: HTTP method.
        :param url: Full request url.
        :param params: Query parameters.
        :param json_body: JSON request body.
        :param auth: Whether to send the user's access token.
        """
        headers = {}
        if auth:
            headers["Authorization"] = f"Bearer {await self._get_access_token()}"
        async with self.mass.http_session.request(
            method, url, params=params, json=json_body, headers=headers, timeout=TIMEOUT
        ) as response:
            if response.status == 404:
                raise MediaNotFoundError(f"{url} not found")
            if response.status == 401 and auth:
                self._access_token = None
                raise ResourceTemporarilyUnavailable("Access token rejected", backoff_time=1)
            if response.status == 429:
                raise RateLimited(
                    backoff_time=parse_retry_after(response.headers.get("Retry-After"))
                )
            if response.status in (502, 503, 504):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()
            return await response.json(content_type=None)


def _normalize(text: str) -> str:
    """Return the text lowercased with everything but letters and digits removed."""
    return "".join(char for char in text.lower() if char.isalnum())
