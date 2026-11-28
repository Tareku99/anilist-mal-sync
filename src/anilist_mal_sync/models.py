"""Data models for anime entries."""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class WatchStatus(str, Enum):
    """Anime watch status."""

    WATCHING = "watching"
    COMPLETED = "completed"
    ON_HOLD = "on_hold"
    DROPPED = "dropped"
    PLAN_TO_WATCH = "plan_to_watch"
    UNKNOWN = "unknown"


class AnimeEntry(BaseModel):
    """Common anime entry model."""

    # Identifiers
    anilist_id: Optional[int] = None
    mal_id: Optional[int] = None
    title: str

    # Watch data
    status: WatchStatus
    score: Optional[float] = Field(None, ge=0)  # Clients normalize list scores to the 0-10 scale
    episodes_watched: int = Field(default=0, ge=0)
    total_episodes: Optional[int] = None

    # Dates
    start_date: str | None = None
    finish_date: str | None = None
    unavailable_dates: set[str] = Field(default_factory=set)
    updated_at: Optional[datetime] = None

    # Additional metadata
    notes: Optional[str] = None
    rewatched: int = Field(default=0, ge=0)
    is_rewatching: bool = False
    is_favorite: bool = Field(default=False)
    is_private: bool | None = False


class SyncChange(BaseModel):
    """One deterministic action or skip decision in a sync plan."""

    operation: Literal["addition", "update", "skipped_conflict", "skipped"]
    source: Optional[str] = None
    target: Optional[str] = None
    title: str
    anilist_id: Optional[int] = None
    mal_id: Optional[int] = None
    fields: list[str] = Field(default_factory=list)
    differences: dict[str, dict[str, object]] = Field(default_factory=dict)
    reason: Optional[str] = None
    winner: Optional[str] = None


class SyncResult(BaseModel):
    """Result of a sync operation."""

    success: bool
    entries_synced: int = 0
    entries_failed: int = 0
    errors: list[str] = Field(default_factory=list)
    dry_run: bool = False
    accounts: dict[str, str] = Field(default_factory=dict)
    authentication_required: list[str] = Field(default_factory=list)
    additions: list[SyncChange] = Field(default_factory=list)
    updates: list[SyncChange] = Field(default_factory=list)
    skipped_conflicts: list[SyncChange] = Field(default_factory=list)
    skipped: list[SyncChange] = Field(default_factory=list)

    @property
    def planned_changes(self) -> int:
        """Number of additions and updates in the sync plan."""
        return len(self.additions) + len(self.updates)

    @property
    def entries_skipped(self) -> int:
        """Count entries left alone for a reason other than already being in sync."""
        return sum(change.reason != "already in sync" for change in self.skipped)
