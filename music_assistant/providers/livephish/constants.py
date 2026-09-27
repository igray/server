"""Constants for the LivePhish provider."""

from __future__ import annotations

TOKEN_URL = "https://id.livephish.com/connect/token"
CLIENT_ID = "Hg7HuH8736dh6gHy5tYhj7JUd"
SCOPE = "offline_access nugsnet:api nugsnet:legacyapi openid profile email"

CATALOG_URL = "https://catalog.livephish.com/api/v1"
STASH_URL = "https://stash.livephish.com/api/v1"
SUBSCRIPTIONS_URL = "https://subscriptions.livephish.com/api/v3/me/subscriptions"
PLAYBACK_URL = "https://playback.livephish.com/api/v1/tracks/{track_id}/url"
PLAYBACK_SESSION_URL = "https://nugs.azure-api.net/playback/1.0-lp/sessions"
LEGACY_API_URL = "https://streamapi.livephish.com/api.aspx"
IMAGE_BASE_URL = "https://s3.amazonaws.com/static.nugs.net"
WEB_URL = "https://plus.livephish.com"

PAGE_SIZE = 100
TOKEN_EXPIRY_MARGIN = 60

REC_POPULAR = "livephish_popular_shows"
REC_RECENT = "livephish_recent_shows"
REC_RECENTLY_PLAYED = "livephish_recently_played"
