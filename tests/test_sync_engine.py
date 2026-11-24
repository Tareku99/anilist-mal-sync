"""Regression tests for bidirectional synchronization."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import anilist_mal_sync.sync_engine as sync_engine_module
from anilist_mal_sync.anilist_client import AniListClient
from anilist_mal_sync.mal_client import MALClient
from anilist_mal_sync.models import AnimeEntry, WatchStatus
from anilist_mal_sync.sync_engine import SyncEngine


@pytest.fixture
def sync_clients(monkeypatch):
    settings = SimpleNamespace(anilist_username=None, score_sync_mode="auto")
    monkeypatch.setattr(sync_engine_module, "get_settings", lambda: settings)

    anilist = Mock(spec=AniListClient)
    mal = Mock(spec=MALClient)
    anilist.get_user_anime_list.return_value = []
    mal.get_user_anime_list.return_value = []
    return SyncEngine(anilist, mal), anilist, mal


def anime_entry(**overrides):
    values = {
        "mal_id": 123,
        "title": "Example Anime",
        "status": WatchStatus.WATCHING,
        "episodes_watched": 4,
    }
    values.update(overrides)
    return AnimeEntry(**values)


def test_bidirectional_sync_resolves_anilist_id_for_mal_only_entry(sync_clients):
    engine, anilist, mal = sync_clients
    mal_entry = anime_entry()
    mal.get_user_anime_list.return_value = [mal_entry]
    anilist.search_anime.return_value = [{"id": 456, "title": {"english": "Example Anime"}}]
    anilist.update_anime.return_value = True

    result = engine.sync("bidirectional")

    assert result.success is True
    assert result.entries_synced == 1
    assert result.entries_failed == 0
    anilist.update_anime.assert_called_once()
    synced_entry = anilist.update_anime.call_args.args[0]
    assert synced_entry.anilist_id == 456


def test_bidirectional_sync_prefers_matching_mal_id(sync_clients):
    engine, anilist, mal = sync_clients
    mal.get_user_anime_list.return_value = [anime_entry()]
    anilist.search_anime.return_value = [
        {
            "id": 999,
            "idMal": 999,
            "title": {"english": "Example Anime"},
        },
        {
            "id": 456,
            "idMal": 123,
            "title": {"english": "Different English Title"},
        },
    ]
    anilist.update_anime.return_value = True

    result = engine.sync("bidirectional")

    assert result.success is True
    assert result.entries_synced == 1
    synced_entry = anilist.update_anime.call_args.args[0]
    assert synced_entry.anilist_id == 456


def test_bidirectional_sync_reports_rejected_anilist_update(sync_clients):
    engine, anilist, mal = sync_clients
    mal.get_user_anime_list.return_value = [anime_entry()]
    anilist.search_anime.return_value = [{"id": 456, "title": {"romaji": "Example Anime"}}]
    anilist.update_anime.return_value = False

    result = engine.sync("bidirectional")

    assert result.success is False
    assert result.entries_synced == 0
    assert result.entries_failed == 1
    assert "Failed to add to AniList" in result.errors[0]


def test_bidirectional_sync_skips_mal_entry_without_anilist_match(sync_clients):
    engine, anilist, mal = sync_clients
    mal.get_user_anime_list.return_value = [anime_entry()]
    anilist.search_anime.return_value = [
        {
            "id": 999,
            "idMal": 999,
            "title": {"english": "Unrelated Anime"},
        }
    ]

    result = engine.sync("bidirectional")

    assert result.success is True
    assert result.entries_synced == 0
    assert result.entries_failed == 0
    anilist.update_anime.assert_not_called()


def test_bidirectional_sync_skips_write_when_only_timestamps_differ(sync_clients):
    engine, anilist, mal = sync_clients
    anilist_entry = anime_entry(
        anilist_id=456,
        score=80,
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    mal_entry = anime_entry(
        score=8,
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    anilist.get_user_anime_list.return_value = [anilist_entry]
    mal.get_user_anime_list.return_value = [mal_entry]

    result = engine.sync("bidirectional")

    assert result.success is True
    anilist.update_anime.assert_not_called()
    mal.update_anime.assert_not_called()


def test_bidirectional_sync_still_propagates_newer_field_changes(sync_clients):
    engine, anilist, mal = sync_clients
    anilist_entry = anime_entry(
        anilist_id=456,
        episodes_watched=5,
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    mal_entry = anime_entry(
        episodes_watched=4,
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    anilist.get_user_anime_list.return_value = [anilist_entry]
    mal.get_user_anime_list.return_value = [mal_entry]
    mal.update_anime.return_value = True

    result = engine.sync("bidirectional")

    assert result.success is True
    mal.update_anime.assert_called_once_with(anilist_entry)

