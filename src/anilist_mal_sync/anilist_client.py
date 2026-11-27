"""AniList API client."""

import logging
from typing import Optional

from .base_client import AuthenticationError, BaseAPIClient
from .config import Settings, get_settings
from .models import AnimeEntry, WatchStatus

logger = logging.getLogger(__name__)

# HTTP Status Code Constants
HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403


class AniListClient(BaseAPIClient):
    """Client for AniList GraphQL API."""

    BASE_URL = "https://graphql.anilist.co"

    def __init__(self, access_token: str, settings: Settings | None = None):
        """Initialize AniList client with access token."""
        super().__init__(
            access_token=access_token,
            base_url=self.BASE_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        self.settings = settings or get_settings()
    
    @staticmethod
    def _safe_title(title: str) -> str:
        """Return a console-safe title string (avoid encoding errors on Windows)."""
        if not title:
            return ""
        return title.encode("ascii", "replace").decode("ascii")

    def _query(self, query: str, variables: Optional[dict] = None) -> dict:
        """Execute a GraphQL query."""
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        response = self.session.post(self.base_url, json=payload, timeout=(5, 30))

        if response.status_code != HTTP_OK:
            self._handle_auth_error(response, "AniList")
            if response.status_code not in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
                logger.error(f"AniList API error: {response.status_code}")
                logger.error(f"Response: {response.text}")
        
        response.raise_for_status()

        data = response.json()
        if "errors" in data:
            logger.error(f"GraphQL errors: {data['errors']}")
            raise Exception(f"GraphQL errors: {data['errors']}")

        return data.get("data", {})

    def get_user_anime_list(self, username: Optional[str] = None) -> list[AnimeEntry]:
        """Fetch user's anime list from AniList."""
        viewer = self._query("query { Viewer { name } }").get("Viewer")
        account = viewer.get("name") if viewer else None
        if not account:
            raise ValueError("AniList did not return the authenticated user's name")
        if username and username.casefold() != account.casefold():
            raise ValueError(f"AniList token belongs to {account}, expected {username}")
        username = account
        logger.info("Syncing AniList account %s", username)

        query = """
        query ($userName: String!, $chunk: Int!) {
          MediaListCollection(userName: $userName, type: ANIME, chunk: $chunk) {
            hasNextChunk
            lists {
              entries {
                id
                status
                score(format: POINT_10_DECIMAL)
                progress
                progressVolumes
                repeat
                notes
                startedAt { year month day }
                completedAt { year month day }
                updatedAt
                media {
                  id
                  idMal
                  isFavourite
                  title { romaji english native }
                  episodes
                }
              }
            }
          }
        }
        """

        entries = []
        seen_entry_ids = set()
        chunk = 1
        while True:
            data = self._query(query, {"userName": username, "chunk": chunk})
            collection = data.get("MediaListCollection")
            if collection is None:
                raise ValueError(f"AniList did not return an anime list for {username}")
            for list_group in collection.get("lists") or []:
                for entry in list_group.get("entries") or []:
                    entry_id = entry["id"]
                    if entry_id not in seen_entry_ids:
                        entries.append(self._parse_entry(entry))
                        seen_entry_ids.add(entry_id)
            if not collection.get("hasNextChunk"):
                break
            chunk += 1

        logger.info(f"Fetched {len(entries)} anime entries from AniList")
        return entries

    def search_anime(self, title: str, limit: int = 5) -> list[dict]:
        """Search AniList anime by title to find IDs."""
        query = """
        query ($search: String, $limit: Int) {
            Page(perPage: $limit) {
                media(search: $search, type: ANIME) {
                    id
                    idMal
                    title { romaji english native }
                }
            }
        }
        """

        variables = {"search": title, "limit": limit}
        data = self._query(query, variables)
        return data.get("Page", {}).get("media", [])

    def _parse_entry(self, entry: dict) -> AnimeEntry:
        """Parse AniList entry to common model."""
        media = entry.get("media", {})
        title_data = media.get("title", {})
        title = title_data.get("romaji") or title_data.get("english") or title_data.get("native")

        # Map AniList status to common status
        status_map = {
            "CURRENT": WatchStatus.WATCHING,
            "REPEATING": WatchStatus.COMPLETED,
            "COMPLETED": WatchStatus.COMPLETED,
            "PAUSED": WatchStatus.ON_HOLD,
            "DROPPED": WatchStatus.DROPPED,
            "PLANNING": WatchStatus.PLAN_TO_WATCH,
        }
        
        # Parse updated_at timestamp (Unix timestamp from AniList)
        updated_at = None
        if entry.get("updatedAt"):
            from datetime import datetime, timezone
            updated_at = datetime.fromtimestamp(entry["updatedAt"], tz=timezone.utc)

        return AnimeEntry(
            anilist_id=media.get("id"),
            mal_id=media.get("idMal"),
            title=title,
            status=status_map.get(entry.get("status"), WatchStatus.UNKNOWN),
            is_rewatching=entry.get("status") == "REPEATING",
            score=entry.get("score"),
            episodes_watched=entry.get("progress", 0),
            total_episodes=media.get("episodes"),
            notes=entry.get("notes"),
            rewatched=entry.get("repeat", 0),
            is_favorite=media.get("isFavourite", False),
            updated_at=updated_at,
        )

    def update_anime(self, entry: AnimeEntry, *, fields: list[str] | None = None) -> bool:
        """Update an anime entry on AniList."""
        if not entry.anilist_id:
            logger.warning(f"Cannot update AniList entry without anilist_id: {self._safe_title(entry.title)}")
            return False

        # Reverse map status
        status_map = {
            WatchStatus.WATCHING: "CURRENT",
            WatchStatus.COMPLETED: "COMPLETED",
            WatchStatus.ON_HOLD: "PAUSED",
            WatchStatus.DROPPED: "DROPPED",
            WatchStatus.PLAN_TO_WATCH: "PLANNING",
        }

        candidates = {
            "status": ("status", "MediaListStatus", status_map.get(entry.status)),
            "episodes_watched": ("progress", "Int", entry.episodes_watched),
            "rewatched": ("repeat", "Int", entry.rewatched),
            "notes": ("notes", "String", entry.notes or ""),
        }
        if self.settings.score_sync_mode == "auto":
            normalized_score = entry.score
            if normalized_score is not None and normalized_score <= 10:
                normalized_score *= 10
            candidates["score"] = (
                "scoreRaw", "Int",
                int(round(normalized_score)) if normalized_score is not None else 0,
            )
        variables = {"mediaId": entry.anilist_id}
        declarations = ["$mediaId: Int"]
        arguments = ["mediaId: $mediaId"]
        for field, (name, graphql_type, value) in candidates.items():
            if fields is None or field in fields:
                variables[name] = value
                declarations.append(f"${name}: {graphql_type}")
                arguments.append(f"{name}: ${name}")

        mutation = f"""
        mutation ({', '.join(declarations)}) {{
          SaveMediaListEntry({', '.join(arguments)}) {{
            id
          }}
        }}
        """

        try:
            self._query(mutation, variables)
            logger.info(f"Updated AniList entry: {self._safe_title(entry.title)} (episodes: {entry.episodes_watched})")
            return True
        except AuthenticationError:
            raise
        except Exception as e:
            logger.error(f"Failed to update AniList entry {self._safe_title(entry.title)}: {e}")
            return False
