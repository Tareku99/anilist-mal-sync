"""MyAnimeList API client."""

import logging

from .base_client import AuthenticationError, BaseAPIClient
from .config import Settings, get_settings
from .models import AnimeEntry, WatchStatus

logger = logging.getLogger(__name__)


class MALClient(BaseAPIClient):
    """Client for MyAnimeList API v2."""

    BASE_URL = "https://api.myanimelist.net/v2"

    def __init__(self, access_token: str, settings: Settings | None = None):
        """Initialize MAL client with access token."""
        super().__init__(access_token=access_token, base_url=self.BASE_URL)
        self.settings = settings or get_settings()
    
    @staticmethod
    def _safe_title(title: str) -> str:
        """Return a console-safe title string (avoid encoding errors on Windows)."""
        if not title:
            return ""
        return title.encode("ascii", "replace").decode("ascii")

    def get_user_anime_list(self, username: str = "@me") -> list[AnimeEntry]:
        """Fetch user's anime list from MyAnimeList."""
        expected = username if username != "@me" else self.settings.mal_username
        profile = self.session.get(f"{self.base_url}/users/@me", timeout=(5, 30))
        self._handle_auth_error(profile, "MyAnimeList")
        profile.raise_for_status()
        account = profile.json().get("name")
        if not account:
            raise ValueError("MyAnimeList did not return the authenticated user's name")
        if expected and expected.casefold() != account.casefold():
            raise ValueError(f"MyAnimeList token belongs to {account}, expected {expected}")
        logger.info("Syncing MyAnimeList account %s", account)
        entries = []
        url = f"{self.base_url}/users/@me/animelist"
        params = {
            "fields": "list_status{status,score,num_episodes_watched,is_rewatching,updated_at,comments,num_times_rewatched},num_episodes",
            "limit": 1000,
        }

        while url:
            response = self.session.get(url, params=params, timeout=(5, 30))
            self._handle_auth_error(response, "MyAnimeList")
            response.raise_for_status()
            data = response.json()

            for item in data.get("data", []):
                entries.append(self._parse_entry(item))

            # Pagination
            url = data.get("paging", {}).get("next")
            params = {}  # Next URL already contains params

        logger.info(f"Fetched {len(entries)} anime entries from MyAnimeList")
        return entries

    def _parse_entry(self, item: dict) -> AnimeEntry:
        """Parse MAL entry to common model."""
        node = item.get("node", {})
        list_status = item.get("list_status", {})

        # Map MAL status to common status
        status_map = {
            "watching": WatchStatus.WATCHING,
            "completed": WatchStatus.COMPLETED,
            "on_hold": WatchStatus.ON_HOLD,
            "dropped": WatchStatus.DROPPED,
            "plan_to_watch": WatchStatus.PLAN_TO_WATCH,
        }

        # MAL uses 0-10 integer scores, convert to float
        score = list_status.get("score")
        if score is not None:
            score = float(score)
        
        # Parse updated_at timestamp
        updated_at = None
        if list_status.get("updated_at"):
            from datetime import datetime
            updated_at = datetime.fromisoformat(list_status["updated_at"].replace("Z", "+00:00"))

        return AnimeEntry(
            mal_id=node.get("id"),
            title=node.get("title"),
            status=status_map.get(list_status.get("status"), WatchStatus.UNKNOWN),
            is_rewatching=list_status.get("is_rewatching") is True,
            score=score,
            episodes_watched=list_status.get("num_episodes_watched", 0),
            total_episodes=node.get("num_episodes"),
            notes=list_status.get("comments"),
            rewatched=list_status.get("num_times_rewatched", 0),
            is_favorite=list_status.get("is_favoriting", False),
            updated_at=updated_at,
        )

    def update_anime(self, entry: AnimeEntry, *, fields: list[str] | None = None) -> bool:
        """Update an anime entry on MyAnimeList."""
        if not entry.mal_id:
            logger.warning(f"Cannot update MAL entry without mal_id: {self._safe_title(entry.title)}")
            return False

        # Reverse map status
        status_map = {
            WatchStatus.WATCHING: "watching",
            WatchStatus.COMPLETED: "completed",
            WatchStatus.ON_HOLD: "on_hold",
            WatchStatus.DROPPED: "dropped",
            WatchStatus.PLAN_TO_WATCH: "plan_to_watch",
        }

        url = f"{self.base_url}/anime/{entry.mal_id}/my_list_status"
        data = {
            "status": status_map.get(entry.status),
            "num_watched_episodes": entry.episodes_watched,
        }

        # Handle score syncing based on configuration
        if self.settings.score_sync_mode == "auto":
            # Normalize score: AniList can use 100-point scale, MAL only accepts 0-10
            normalized_score = entry.score
            if normalized_score is not None and normalized_score > 10:
                normalized_score = normalized_score / 10.0
            data["score"] = int(round(normalized_score)) if normalized_score is not None else 0
        # If score_sync_mode == "disabled", skip score field entirely

        data["comments"] = entry.notes or ""
        data["num_times_rewatched"] = entry.rewatched
        if fields is not None:
            parameter_names = {
                "status": "status",
                "episodes_watched": "num_watched_episodes",
                "rewatched": "num_times_rewatched",
                "notes": "comments",
                "score": "score",
            }
            selected = {parameter_names[field] for field in fields}
            data = {name: value for name, value in data.items() if name in selected}

        try:
            response = self.session.patch(url, data=data, timeout=(5, 30))
            self._handle_auth_error(response, "MyAnimeList")
            response.raise_for_status()
            logger.info(f"Updated MAL entry: {self._safe_title(entry.title)} (episodes: {entry.episodes_watched})")
            return True
        except AuthenticationError:
            raise
        except Exception as e:
            logger.error(f"Failed to update MAL entry {self._safe_title(entry.title)}: {e}")
            return False

    def search_anime(self, title: str, limit: int = 5) -> list[dict]:
        """Search for anime by title to find MAL ID."""
        url = f"{self.base_url}/anime"
        params = {"q": title, "limit": limit}

        response = self.session.get(url, params=params, timeout=(5, 30))
        self._handle_auth_error(response, "MyAnimeList")
        response.raise_for_status()
        return response.json().get("data", [])
