"""Core sync engine for anime list synchronization."""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Literal

from .base_client import AuthenticationError
from .config import Settings, get_settings
from .models import AnimeEntry, SyncChange, SyncResult

logger = logging.getLogger(__name__)

Platform = Literal["anilist", "mal"]


class SyncEngine:
    """Plan and apply deterministic changes between AniList and MAL."""

    SYNC_FIELDS = ("status", "episodes_watched", "rewatched", "notes", "score")

    def __init__(
        self,
        anilist_client,
        mal_client,
        dry_run: bool = False,
        settings: Settings | None = None,
    ):
        """Initialize the engine with API clients and an optional settings snapshot."""
        self.anilist = anilist_client
        self.mal = mal_client
        self.dry_run = dry_run
        self.settings = settings or get_settings()

    @staticmethod
    def _safe_title(title: str) -> str:
        """Return a console-safe title string (avoid encoding errors on Windows)."""
        return (title or "").encode("ascii", "replace").decode("ascii")

    @staticmethod
    def _normalize_score_for_mal(score: float | None) -> int | None:
        """Normalize AniList's 0-100 and MAL's 0-10 scales for comparison."""
        if score is None:
            return None
        if score > 10:
            score /= 10.0
        return int(round(score))

    @staticmethod
    def _timestamp(value: datetime | None) -> datetime | None:
        """Normalize API timestamps; MAL timestamps without zones are assumed UTC."""
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _ordered_entries(cls, entries: list[AnimeEntry]) -> list[AnimeEntry]:
        """Order entries consistently, including entries without external IDs."""
        return sorted(
            entries,
            key=lambda entry: (
                entry.mal_id is None,
                entry.mal_id if entry.mal_id is not None else -1,
                entry.anilist_id if entry.anilist_id is not None else -1,
                entry.title.casefold(),
                entry.title,
            ),
        )

    @classmethod
    def _group_by_mal_id(
        cls, entries: list[AnimeEntry]
    ) -> dict[int, list[AnimeEntry]]:
        grouped: dict[int, list[AnimeEntry]] = defaultdict(list)
        for entry in entries:
            if entry.mal_id is not None:
                grouped[entry.mal_id].append(entry)
        return dict(grouped)

    @staticmethod
    def _field_value(entry: AnimeEntry, field: str):
        value = getattr(entry, field)
        if field == "status":
            return value.value
        return value

    def _changed_fields(
        self,
        source_entry: AnimeEntry,
        target_entry: AnimeEntry,
    ) -> list[str]:
        """Compare only fields written by the clients, in stable display order."""
        changed = []
        if source_entry.status != target_entry.status:
            changed.append("status")
        if source_entry.episodes_watched != target_entry.episodes_watched:
            changed.append("episodes_watched")
        if source_entry.rewatched != target_entry.rewatched:
            changed.append("rewatched")
        if (source_entry.notes or "") != (target_entry.notes or ""):
            changed.append("notes")
        if self.settings.score_sync_mode == "auto":
            if self._normalize_score_for_mal(source_entry.score) != self._normalize_score_for_mal(
                target_entry.score
            ):
                changed.append("score")
        return changed

    def _entry_values(self, entry: AnimeEntry) -> dict[str, object]:
        values = {field: self._field_value(entry, field) for field in self.SYNC_FIELDS}
        if self.settings.score_sync_mode != "auto":
            values.pop("score")
        return values

    def _record(
        self,
        result: SyncResult,
        change: SyncChange,
    ) -> None:
        """Append a change to its category while preserving plan order."""
        target = {
            "addition": result.additions,
            "update": result.updates,
            "skipped_conflict": result.skipped_conflicts,
            "skipped": result.skipped,
        }[change.operation]
        target.append(change)

    def _make_change(
        self,
        operation: Literal["addition", "update", "skipped_conflict", "skipped"],
        entry: AnimeEntry,
        source: str | None = None,
        target: str | None = None,
        fields: list[str] | None = None,
        differences: dict[str, dict[str, object]] | None = None,
        reason: str | None = None,
        winner: str | None = None,
        target_entry: AnimeEntry | None = None,
    ) -> SyncChange:
        if differences is None and fields:
            source_values = self._entry_values(entry)
            target_values = self._entry_values(target_entry) if target_entry else {}
            differences = {
                field: {
                    "from": target_values.get(field),
                    "to": source_values.get(field),
                }
                for field in fields
            }
        return SyncChange(
            operation=operation,
            source=source,
            target=target,
            title=entry.title,
            anilist_id=entry.anilist_id,
            mal_id=entry.mal_id,
            fields=fields or [],
            differences=differences or {},
            reason=reason,
            winner=winner,
        )

    def _skip(
        self,
        result: SyncResult,
        entry: AnimeEntry,
        reason: str,
        *,
        conflict: bool = False,
        fields: list[str] | None = None,
        differences: dict[str, dict[str, object]] | None = None,
        winner: str | None = None,
    ) -> None:
        operation = "skipped_conflict" if conflict else "skipped"
        self._record(
            result,
            self._make_change(
                operation,
                entry,
                fields=fields,
                differences=differences,
                reason=reason,
                winner=winner,
            ),
        )

    def _apply(
        self,
        result: SyncResult,
        source_entry: AnimeEntry,
        target_entry: AnimeEntry | None,
        source: Platform,
        target: Platform,
        target_client,
    ) -> None:
        operation = "addition" if target_entry is None else "update"
        fields = list(self.SYNC_FIELDS) if target_entry is None else self._changed_fields(
            source_entry, target_entry
        )
        if self.settings.score_sync_mode != "auto":
            fields = [field for field in fields if field != "score"]
        if target_entry is not None and not fields:
            self._skip(result, source_entry, "already in sync")
            return

        change = self._make_change(
            operation,
            source_entry,
            source=source,
            target=target,
            fields=fields,
            target_entry=target_entry,
        )
        self._record(result, change)
        if self.dry_run:
            logger.info(
                "[DRY RUN] Would %s %s on %s",
                "add" if operation == "addition" else "update",
                self._safe_title(source_entry.title),
                target,
            )
            return

        try:
            if target_client.update_anime(source_entry):
                result.entries_synced += 1
            else:
                result.entries_failed += 1
                target_name = "AniList" if target == "anilist" else "MAL"
                action = "add" if operation == "addition" else "update"
                result.errors.append(
                    f"Failed to {action} to {target_name}: {source_entry.title}"
                )
        except AuthenticationError as error:
            error.partial_result = result.model_copy(deep=True)
            raise
        except Exception as error:
            result.entries_failed += 1
            result.errors.append(f"{source_entry.title}: {error}")
            logger.exception("Failed to sync %s to %s", source_entry.title, target)

    @staticmethod
    def _record_item_failure(result: SyncResult, entry: AnimeEntry, error: Exception) -> None:
        result.entries_failed += 1
        result.errors.append(f"{entry.title}: {error}")
        logger.error("Could not plan sync for %s: %s", entry.title, error)

    def sync(
        self,
        mode: Literal["anilist-to-mal", "mal-to-anilist", "bidirectional"],
    ) -> SyncResult:
        """Perform sync based on selected mode."""
        logger.info("Starting sync with mode: %s (dry_run=%s)", mode, self.dry_run)
        if mode == "anilist-to-mal":
            return self._sync_one_way("anilist", "mal")
        if mode == "mal-to-anilist":
            return self._sync_one_way("mal", "anilist")
        if mode == "bidirectional":
            return self._sync_bidirectional()
        raise ValueError(f"Unknown sync mode: {mode}")

    def _sync_one_way(self, source: Platform, target: Platform) -> SyncResult:
        result = SyncResult(success=True, dry_run=self.dry_run)
        username = self.settings.anilist_username or None
        if source == "anilist":
            source_entries = self.anilist.get_user_anime_list(username)
            target_entries = self.mal.get_user_anime_list()
            target_client = self.mal
        else:
            source_entries = self.mal.get_user_anime_list()
            target_entries = self.anilist.get_user_anime_list(username)
            target_client = self.anilist

        source_groups = self._group_by_mal_id(source_entries)
        target_groups = self._group_by_mal_id(target_entries)
        by_anilist_id: dict[int, list[AnimeEntry]] = defaultdict(list)
        for entry in target_entries:
            if entry.anilist_id is not None:
                by_anilist_id[entry.anilist_id].append(entry)

        for source_entry in self._ordered_entries(source_entries):
            mal_id = source_entry.mal_id
            if mal_id is None:
                self._skip(result, source_entry, "missing MAL ID; cannot match across services")
                continue
            if len(source_groups.get(mal_id, [])) > 1:
                if source_groups[mal_id][0] is source_entry:
                    self._skip(
                        result,
                        source_entry,
                        f"multiple {source} entries map to MAL ID {mal_id}",
                        conflict=True,
                    )
                continue

            matches = target_groups.get(mal_id, [])
            if len(matches) > 1:
                self._skip(
                    result,
                    source_entry,
                    f"multiple {target} entries map to MAL ID {mal_id}",
                    conflict=True,
                )
                continue

            target_entry = matches[0] if matches else None
            prepared_entry = source_entry
            if target == "anilist":
                if target_entry is not None:
                    prepared_entry = source_entry.model_copy(
                        update={"anilist_id": target_entry.anilist_id}
                    )
                elif source_entry.anilist_id is not None:
                    id_matches = by_anilist_id.get(source_entry.anilist_id, [])
                    if len(id_matches) > 1:
                        self._skip(
                            result,
                            source_entry,
                            f"multiple AniList entries map to AniList ID {source_entry.anilist_id}",
                            conflict=True,
                        )
                        continue
                    if id_matches and id_matches[0].mal_id not in (None, mal_id):
                        self._skip(
                            result,
                            source_entry,
                            "MAL and AniList IDs point to different media entries",
                            conflict=True,
                        )
                        continue
                    target_entry = id_matches[0] if id_matches else None
                else:
                    try:
                        anilist_id, reason, ambiguous = self._find_anilist_id(
                            source_entry.title, source_entry.mal_id
                        )
                    except AuthenticationError as error:
                        error.partial_result = result.model_copy(deep=True)
                        raise
                    except Exception as error:
                        self._record_item_failure(result, source_entry, error)
                        continue
                    if anilist_id is None:
                        self._skip(
                            result,
                            source_entry,
                            reason,
                            conflict=ambiguous,
                        )
                        continue
                    prepared_entry = source_entry.model_copy(update={"anilist_id": anilist_id})

            self._apply(
                result,
                prepared_entry,
                target_entry,
                source,
                target,
                target_client,
            )

        result.success = result.entries_failed == 0
        return result

    def _find_anilist_id(
        self, title: str, mal_id: int | None = None
    ) -> tuple[int | None, str, bool]:
        """Prefer an exact MAL ID, then use a unique exact title without ID conflicts."""
        matches = self.anilist.search_anime(title, limit=10)
        candidates = [match for match in matches if match.get("id") is not None]

        if mal_id is not None:
            mal_matches = {
                match["id"] for match in candidates if match.get("idMal") == mal_id
            }
            if len(mal_matches) == 1:
                return mal_matches.pop(), "", False
            if len(mal_matches) > 1:
                return None, f"multiple AniList results match MAL ID {mal_id}", True
            candidates = [
                match for match in candidates if match.get("idMal") in (None, mal_id)
            ]

        exact_ids = set()
        normalized_title = title.casefold().strip()
        for match in candidates:
            titles = match.get("title") or {}
            if any(
                candidate and candidate.casefold().strip() == normalized_title
                for candidate in titles.values()
            ):
                exact_ids.add(match["id"])
        if len(exact_ids) == 1:
            return exact_ids.pop(), "", False
        if len(exact_ids) > 1:
            return None, "multiple exact AniList title matches", True
        return None, "no exact AniList title match", False

    def _sync_bidirectional(self) -> SyncResult:
        """Sync both ways using conservative, deterministic conflict rules."""
        result = SyncResult(success=True, dry_run=self.dry_run)
        username = self.settings.anilist_username or None
        anilist_entries = self.anilist.get_user_anime_list(username)
        mal_entries = self.mal.get_user_anime_list()
        anilist_groups = self._group_by_mal_id(anilist_entries)
        mal_groups = self._group_by_mal_id(mal_entries)

        for entry in self._ordered_entries(anilist_entries):
            if entry.mal_id is None:
                self._skip(result, entry, "missing MAL ID; cannot match across services")

        for entry in self._ordered_entries(mal_entries):
            if entry.mal_id is None:
                self._skip(result, entry, "missing MAL ID; cannot match across services")

        for mal_id in sorted(set(anilist_groups) | set(mal_groups)):
            anilist_matches = anilist_groups.get(mal_id, [])
            mal_matches = mal_groups.get(mal_id, [])
            if len(anilist_matches) > 1 or len(mal_matches) > 1:
                entry = (anilist_matches or mal_matches)[0]
                self._skip(
                    result,
                    entry,
                    f"duplicate entries for MAL ID {mal_id}; refusing ambiguous sync",
                    conflict=True,
                )
                continue

            anilist_entry = anilist_matches[0] if anilist_matches else None
            mal_entry = mal_matches[0] if mal_matches else None
            if anilist_entry is not None and mal_entry is None:
                self._apply(
                    result,
                    anilist_entry,
                    None,
                    "anilist",
                    "mal",
                    self.mal,
                )
            elif mal_entry is not None and anilist_entry is None:
                try:
                    anilist_id, reason, ambiguous = self._find_anilist_id(
                        mal_entry.title, mal_entry.mal_id
                    )
                except AuthenticationError as error:
                    error.partial_result = result.model_copy(deep=True)
                    raise
                except Exception as error:
                    self._record_item_failure(result, mal_entry, error)
                    continue
                if anilist_id is None:
                    self._skip(result, mal_entry, reason, conflict=ambiguous)
                    continue
                prepared_entry = mal_entry.model_copy(update={"anilist_id": anilist_id})
                self._apply(
                    result,
                    prepared_entry,
                    None,
                    "mal",
                    "anilist",
                    self.anilist,
                )
            elif anilist_entry is not None and mal_entry is not None:
                self._resolve_conflict(result, anilist_entry, mal_entry)

        result.success = result.entries_failed == 0
        return result

    def _conflict_differences(
        self,
        anilist_entry: AnimeEntry,
        mal_entry: AnimeEntry,
        fields: list[str],
    ) -> dict[str, dict[str, object]]:
        differences = {}
        for field in fields:
            differences[field] = {
                "anilist": self._field_value(anilist_entry, field),
                "mal": self._field_value(mal_entry, field),
            }
        return differences

    def _resolve_conflict(
        self,
        result: SyncResult,
        anilist_entry: AnimeEntry,
        mal_entry: AnimeEntry,
    ) -> None:
        """Apply a clear winner or report a conflict rather than silently discarding it."""
        fields = self._changed_fields(anilist_entry, mal_entry)
        if not fields:
            self._skip(result, anilist_entry, "already in sync")
            return

        anilist_time = self._timestamp(anilist_entry.updated_at)
        mal_time = self._timestamp(mal_entry.updated_at)
        if anilist_time is not None and mal_time is not None and anilist_time != mal_time:
            if anilist_time > mal_time:
                self._apply(
                    result,
                    anilist_entry,
                    mal_entry,
                    "anilist",
                    "mal",
                    self.mal,
                )
            else:
                prepared_mal = mal_entry.model_copy(
                    update={"anilist_id": anilist_entry.anilist_id}
                )
                self._apply(
                    result,
                    prepared_mal,
                    anilist_entry,
                    "mal",
                    "anilist",
                    self.anilist,
                )
            return

        # When timestamps cannot establish an order, only monotonically higher
        # progress with the same status is safe to merge automatically.
        if (
            anilist_entry.status == mal_entry.status
            and fields == ["episodes_watched"]
            and anilist_entry.episodes_watched != mal_entry.episodes_watched
        ):
            if anilist_entry.episodes_watched > mal_entry.episodes_watched:
                self._apply(
                    result,
                    anilist_entry,
                    mal_entry,
                    "anilist",
                    "mal",
                    self.mal,
                )
            else:
                prepared_mal = mal_entry.model_copy(
                    update={"anilist_id": anilist_entry.anilist_id}
                )
                self._apply(
                    result,
                    prepared_mal,
                    anilist_entry,
                    "mal",
                    "anilist",
                    self.anilist,
                )
            return

        reason = (
            "different values have equal update timestamps"
            if anilist_time is not None and mal_time is not None
            else "different values cannot be ordered because an update timestamp is missing"
        )
        self._record(
            result,
            self._make_change(
                "skipped_conflict",
                anilist_entry,
                fields=fields,
                differences=self._conflict_differences(anilist_entry, mal_entry, fields),
                reason=reason,
            ),
        )
