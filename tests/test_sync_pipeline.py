"""Regression coverage for sync planning, conflicts, retries, and token renewal."""

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests
from click.testing import CliRunner

from anilist_mal_sync import cli
from anilist_mal_sync.anilist_client import AniListClient
from anilist_mal_sync.base_client import AuthenticationError
from anilist_mal_sync.mal_client import MALClient
from anilist_mal_sync.models import AnimeEntry, SyncResult, WatchStatus
from anilist_mal_sync.oauth import TokenManager
from anilist_mal_sync.sync_engine import SyncEngine
from anilist_mal_sync.sync_service import (
    _acquire_process_sync_lock,
    _release_process_sync_lock,
    execute_sync,
    print_sync_results,
)


def settings(tmp_path, *, score_sync_mode="auto"):
    return SimpleNamespace(
        anilist_username="test-user",
        score_sync_mode=score_sync_mode,
        token_file=tmp_path / "tokens.json",
        anilist_access_token="",
        mal_access_token="",
        mal_refresh_token="",
    )


def entry(
    mal_id,
    *,
    anilist_id=None,
    title="Example Anime",
    status=WatchStatus.WATCHING,
    episodes=3,
    score=8,
    updated_at=None,
    notes=None,
):
    return AnimeEntry(
        mal_id=mal_id,
        anilist_id=anilist_id,
        title=title,
        status=status,
        episodes_watched=episodes,
        score=score,
        updated_at=updated_at,
        notes=notes,
    )


class FakeClient:
    def __init__(self, entries=(), search_results=(), update_result=True):
        self.entries = list(entries)
        self.search_results = list(search_results)
        self.update_result = update_result
        self.updates = []

    def get_user_anime_list(self, username=None):
        return list(self.entries)

    def update_anime(self, anime_entry, *, fields=None):
        self.updates.append(anime_entry)
        return self.update_result

    def search_anime(self, title, limit=5):
        return self.search_results[:limit]

    def find_anime_by_mal_id(self, mal_id):
        return [item for item in self.search_results if item.get("idMal") == mal_id]


def test_dry_run_emits_stable_structured_diff_without_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "anilist_mal_sync.sync_engine.get_settings",
        lambda: settings(tmp_path),
    )
    anilist = FakeClient(
        [
            entry(3, anilist_id=103, title="New Anime"),
            entry(1, anilist_id=101, title="Unchanged Anime"),
            entry(
                2,
                anilist_id=102,
                title="Updated Anime",
                status=WatchStatus.COMPLETED,
                episodes=12,
            ),
        ]
    )
    mal = FakeClient(
        [
            entry(1, title="Unchanged Anime"),
            entry(2, title="Updated Anime", episodes=4),
        ]
    )
    engine = SyncEngine(anilist, mal, dry_run=True, settings=settings(tmp_path))

    result = engine.sync("anilist-to-mal")

    assert result.success
    assert result.entries_synced == 0
    assert result.planned_changes == 2
    assert [change.mal_id for change in result.additions] == [3]
    assert [change.mal_id for change in result.updates] == [2]
    assert result.updates[0].fields == ["status", "episodes_watched"]
    assert result.updates[0].differences == {
        "status": {"from": "watching", "to": "completed"},
        "episodes_watched": {"from": 4, "to": 12},
    }
    assert [change.title for change in result.skipped] == ["Unchanged Anime"]
    assert anilist.updates == []
    assert mal.updates == []

    output = []
    monkeypatch.setattr("anilist_mal_sync.sync_service.click.echo", output.append)
    print_sync_results(result, "anilist-to-mal")
    rendered = json.loads(output[0])
    assert rendered["counts"] == {
        "additions": 1,
        "failures": 0,
        "planned_changes": 2,
        "skipped": 1,
        "skipped_conflicts": 0,
        "updates": 1,
    }
    assert rendered["updates"][0]["mal_id"] == 2


@pytest.mark.parametrize(
    ("anilist_status", "anilist_episodes", "anilist_time", "mal_status", "mal_episodes", "mal_time", "winner"),
    [
        (
            WatchStatus.COMPLETED,
            12,
            datetime(2026, 1, 2, tzinfo=timezone.utc),
            WatchStatus.WATCHING,
            4,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "anilist",
        ),
        (
            WatchStatus.WATCHING,
            4,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            WatchStatus.COMPLETED,
            12,
            datetime(2026, 1, 2, tzinfo=timezone.utc),
            "mal",
        ),
    ],
)
def test_newer_timestamp_wins_bidirectional_status_and_episode_conflict(
    tmp_path,
    monkeypatch,
    anilist_status,
    anilist_episodes,
    anilist_time,
    mal_status,
    mal_episodes,
    mal_time,
    winner,
):
    monkeypatch.setattr(
        "anilist_mal_sync.sync_engine.get_settings",
        lambda: settings(tmp_path),
    )
    anilist_entry = entry(
        7,
        anilist_id=70,
        status=anilist_status,
        episodes=anilist_episodes,
        updated_at=anilist_time,
    )
    mal_entry = entry(
        7,
        status=mal_status,
        episodes=mal_episodes,
        updated_at=mal_time,
    )
    anilist = FakeClient([anilist_entry])
    mal = FakeClient([mal_entry])
    engine = SyncEngine(anilist, mal, dry_run=True, settings=settings(tmp_path))

    result = engine.sync("bidirectional")

    assert result.success
    assert result.skipped_conflicts == []
    assert result.planned_changes == 1
    change = (result.updates or result.additions)[0]
    assert change.source == winner
    assert set(change.fields) == {"status", "episodes_watched"}
    assert anilist.updates == []
    assert mal.updates == []


@pytest.mark.parametrize("timestamps", ["equal", "missing"])
def test_ambiguous_bidirectional_status_conflict_is_reported_and_skipped(
    tmp_path,
    monkeypatch,
    timestamps,
):
    monkeypatch.setattr(
        "anilist_mal_sync.sync_engine.get_settings",
        lambda: settings(tmp_path),
    )
    timestamp = (
        datetime(2026, 1, 1, tzinfo=timezone.utc) if timestamps == "equal" else None
    )
    anilist = FakeClient(
        [entry(7, anilist_id=70, status=WatchStatus.COMPLETED, episodes=12, updated_at=timestamp)]
    )
    mal = FakeClient(
        [entry(7, status=WatchStatus.WATCHING, episodes=4, updated_at=timestamp)]
    )
    engine = SyncEngine(anilist, mal, dry_run=True, settings=settings(tmp_path))

    result = engine.sync("bidirectional")

    assert result.success
    assert result.planned_changes == 0
    assert len(result.skipped_conflicts) == 1
    assert result.skipped_conflicts[0].fields == ["status", "episodes_watched"]
    assert result.skipped_conflicts[0].differences == {
        "status": {"anilist": "completed", "mal": "watching"},
        "episodes_watched": {"anilist": 12, "mal": 4},
    }
    assert anilist.updates == []
    assert mal.updates == []


def test_missing_timestamps_merge_only_higher_episode_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "anilist_mal_sync.sync_engine.get_settings",
        lambda: settings(tmp_path),
    )
    anilist = FakeClient([entry(7, anilist_id=70, episodes=4, updated_at=None)])
    mal = FakeClient([entry(7, episodes=8, updated_at=None)])
    result = SyncEngine(
        anilist,
        mal,
        dry_run=True,
        settings=settings(tmp_path),
    ).sync("bidirectional")

    assert len(result.updates) == 1
    assert result.updates[0].source == "mal"
    assert result.updates[0].fields == ["episodes_watched"]
    assert result.skipped_conflicts == []


def test_failed_addition_is_not_counted_as_synced(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "anilist_mal_sync.sync_engine.get_settings",
        lambda: settings(tmp_path),
    )
    anilist = FakeClient([entry(7, anilist_id=70)])
    mal = FakeClient(update_result=False)

    result = SyncEngine(anilist, mal, settings=settings(tmp_path)).sync("bidirectional")

    assert not result.success
    assert result.entries_synced == 0
    assert result.entries_failed == 1
    assert len(result.additions) == 1


def test_duplicate_mal_ids_are_reported_as_conflicts_not_silently_overwritten(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "anilist_mal_sync.sync_engine.get_settings",
        lambda: settings(tmp_path),
    )
    anilist = FakeClient(
        [
            entry(7, anilist_id=70, title="First Match"),
            entry(7, anilist_id=71, title="Second Match"),
        ]
    )
    mal = FakeClient()

    result = SyncEngine(
        anilist,
        mal,
        dry_run=True,
        settings=settings(tmp_path),
    ).sync("anilist-to-mal")

    assert result.planned_changes == 0
    assert len(result.skipped_conflicts) == 1
    assert "multiple anilist entries" in result.skipped_conflicts[0].reason
    assert mal.updates == []


def test_anilist_search_failure_is_counted_not_reported_as_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "anilist_mal_sync.sync_engine.get_settings",
        lambda: settings(tmp_path),
    )
    anilist = FakeClient()

    def failed_search(title, limit=5):
        raise requests.ConnectionError("AniList search unavailable")

    anilist.search_anime = failed_search
    mal = FakeClient([entry(7, title="Unmatched Anime")])
    result = SyncEngine(anilist, mal, settings=settings(tmp_path)).sync("mal-to-anilist")

    assert not result.success
    assert result.entries_failed == 1
    assert result.skipped == []
    assert anilist.updates == []


def test_title_only_anilist_candidate_is_reported_for_review(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "anilist_mal_sync.sync_engine.get_settings",
        lambda: settings(tmp_path),
    )
    anilist = FakeClient(
        search_results=[{"id": 70, "title": {"english": "Example Anime"}}]
    )
    mal = FakeClient([entry(7, title="Example Anime")])

    result = SyncEngine(
        anilist,
        mal,
        dry_run=True,
        settings=settings(tmp_path),
    ).sync("bidirectional")

    assert result.additions == []
    assert "AniList ID 70" in result.skipped_conflicts[0].reason
    assert anilist.updates == []
    assert mal.updates == []


def test_anilist_direct_mal_id_lookup_ignores_different_titles(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "anilist_mal_sync.sync_engine.get_settings",
        lambda: settings(tmp_path),
    )
    anilist = FakeClient(
        search_results=[
            {"id": 70, "idMal": 700, "title": {"english": "Shared Title"}},
            {"id": 71, "idMal": 701, "title": {"english": "Different Title"}},
        ]
    )
    mal = FakeClient([entry(701, title="Shared Title")])

    result = SyncEngine(
        anilist,
        mal,
        dry_run=True,
        settings=settings(tmp_path),
    ).sync("bidirectional")

    assert len(result.additions) == 1
    assert result.additions[0].anilist_id == 71


def test_anilist_direct_lookup_filters_by_mal_id(monkeypatch, tmp_path):
    client = AniListClient("test-token", settings=settings(tmp_path))
    matches = [{"id": 456, "idMal": 123}, {"id": 789, "idMal": 123}]

    def query(document, variables):
        assert "media(idMal: $malId, type: ANIME)" in document
        assert "Page(perPage: 2)" in document
        assert variables == {"malId": 123}
        return {"Page": {"media": matches}}

    monkeypatch.setattr(client, "_query", query)
    assert client.find_anime_by_mal_id(123) == matches


def test_anilist_search_rejects_exact_title_with_conflicting_mal_id(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "anilist_mal_sync.sync_engine.get_settings",
        lambda: settings(tmp_path),
    )
    anilist = FakeClient(
        search_results=[
            {"id": 70, "idMal": 700, "title": {"english": "Shared Title"}},
        ]
    )
    mal = FakeClient([entry(701, title="Shared Title")])

    result = SyncEngine(
        anilist,
        mal,
        dry_run=True,
        settings=settings(tmp_path),
    ).sync("bidirectional")

    assert result.additions == []
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "no AniList entry for MAL ID 701 or exact title suggestion"


def test_mal_rate_limit_429_retries_then_completes():
    request_count = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
            payload = {"name": "tester"} if self.path == "/users/@me" else {"data": [], "paging": {}}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = MALClient("test-token", settings=SimpleNamespace(score_sync_mode="auto", mal_username="tester"))
        client.base_url = f"http://127.0.0.1:{server.server_port}"
        assert client.get_user_anime_list() == []
        assert request_count == 3
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_anilist_rate_limit_429_retries_graphql_post():
    request_count = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal request_count
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request_count += 1
            if request_count == 1:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
            body = json.dumps({"data": {"viewer": {"id": 1}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = AniListClient("test-token", settings=SimpleNamespace(score_sync_mode="auto"))
        client.base_url = f"http://127.0.0.1:{server.server_port}"
        assert client._query("query { viewer { id } }") == {"viewer": {"id": 1}}
        assert request_count == 2
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_anilist_write_uses_raw_100_point_score(monkeypatch):
    client = AniListClient("test-token", settings=SimpleNamespace(score_sync_mode="auto"))
    captured = {}

    def capture_mutation(query, variables=None):
        captured.update(query=query, variables=variables)
        return {"SaveMediaListEntry": {"id": 1}}

    monkeypatch.setattr(client, "_query", capture_mutation)

    assert client.update_anime(entry(7, anilist_id=70, score=8.5))
    assert captured["variables"]["scoreRaw"] == 85
    assert "scoreRaw: $scoreRaw" in captured["query"]
    assert "score: $score" not in captured["query"]


def test_anilist_write_requires_confirmation(monkeypatch, tmp_path):
    client = AniListClient("fixture-token", settings(tmp_path))
    monkeypatch.setattr(client, "_query", lambda *args: {"SaveMediaListEntry": None})
    assert not client.update_anime(entry(7, anilist_id=70), fields=["episodes_watched"])


def test_auth_error_during_sync_refreshes_mal_and_restarts_from_fresh_lists(
    tmp_path,
    monkeypatch,
):
    config = settings(tmp_path)
    token_manager = TokenManager(config.token_file)
    token_manager.set_tokens("mal", "expired-token", "refresh-token", 3600)
    mal_tokens = []
    mal_entries = []
    fail_second_entry_once = True
    update_ids = []

    class TestAniListClient(FakeClient):
        def __init__(self, access_token, settings):
            super().__init__(
                [entry(7, anilist_id=70, title="First Anime"), entry(8, anilist_id=80, title="Second Anime")]
            )

    class TestMALClient(FakeClient):
        def __init__(self, access_token, settings):
            super().__init__()
            mal_tokens.append(access_token)
            self.entries = mal_entries

        def update_anime(self, anime_entry, *, fields=None):
            nonlocal fail_second_entry_once
            self.updates.append(anime_entry)
            update_ids.append(anime_entry.mal_id)
            if anime_entry.mal_id == 8 and fail_second_entry_once:
                fail_second_entry_once = False
                response = requests.Response()
                response.status_code = 401
                response.url = "https://api.myanimelist.net/v2/anime/8/my_list_status"
                raise AuthenticationError("MyAnimeList", response)
            if not any(existing.mal_id == anime_entry.mal_id for existing in mal_entries):
                mal_entries.append(anime_entry)
            return True

    monkeypatch.setattr("anilist_mal_sync.sync_service.AniListClient", TestAniListClient)
    monkeypatch.setattr("anilist_mal_sync.sync_service.MALClient", TestMALClient)

    def refresh(service, refresh_settings, manager):
        manager.set_tokens(service, "fresh-token", "rotated-refresh-token", 3600)

    monkeypatch.setattr("anilist_mal_sync.sync_service.refresh_mal_token", refresh)

    success, result = execute_sync(
        "bidirectional",
        settings=config,
        anilist_token="anilist-token",
        mal_token="expired-token",
        token_manager=token_manager,
    )

    assert success
    assert result is not None and result.success
    assert mal_tokens == ["expired-token", "fresh-token"]
    assert result.entries_synced == 2
    assert update_ids == [7, 8, 8]
    assert [anime.mal_id for anime in mal_entries] == [7, 8]
    assert [change.mal_id for change in result.skipped] == [7]
    assert token_manager.get_token("mal") == "fresh-token"


def test_anilist_auth_error_reauthenticates_only_anilist_and_restarts(tmp_path, monkeypatch):
    config = settings(tmp_path)
    token_manager = TokenManager(config.token_file)
    anilist_tokens = []
    oauth_services = []
    fail_once = True

    class TestAniListClient(FakeClient):
        def __init__(self, access_token, settings):
            super().__init__()
            anilist_tokens.append(access_token)

        def find_anime_by_mal_id(self, mal_id):
            return [{"id": 70, "idMal": mal_id}]

        def update_anime(self, anime_entry, *, fields=None):
            nonlocal fail_once
            self.updates.append(anime_entry)
            if fail_once:
                fail_once = False
                response = requests.Response()
                response.status_code = 401
                response.url = "https://graphql.anilist.co"
                raise AuthenticationError("AniList", response)
            return True

    class TestMALClient(FakeClient):
        def __init__(self, access_token, settings):
            super().__init__([entry(7, title="Example Anime")])

    monkeypatch.setattr("anilist_mal_sync.sync_service.AniListClient", TestAniListClient)
    monkeypatch.setattr("anilist_mal_sync.sync_service.MALClient", TestMALClient)

    def reauthorize(service, oauth_settings, manager):
        oauth_services.append(service)
        manager.set_tokens(service, "fresh-anilist-token", expires_in=3600)
        return True

    monkeypatch.setattr("anilist_mal_sync.sync_service.run_oauth_flow", reauthorize)
    success, result = execute_sync(
        "bidirectional",
        settings=config,
        anilist_token="expired-anilist-token",
        mal_token="mal-token",
        token_manager=token_manager,
    )

    assert success
    assert result is not None and result.success
    assert anilist_tokens == ["expired-anilist-token", "fresh-anilist-token"]
    assert oauth_services == ["anilist"]
    assert result.entries_synced == 1


def test_failed_refresh_does_not_return_expired_token(tmp_path):
    token_manager = TokenManager(tmp_path / "tokens.json")
    token_manager.set_tokens("mal", "expired-token", "refresh-token", 0)

    def fail_refresh(service, settings, manager):
        raise requests.HTTPError("refresh token rejected")

    assert token_manager.get_valid_token("mal", SimpleNamespace(), fail_refresh) is None


def test_token_expiry_parser_accepts_naive_rfc3339_as_utc(tmp_path):
    token_manager = TokenManager(tmp_path / "tokens.json")
    token_manager.data["tokens"]["mal"] = {
        "access_token": "token",
        "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        ),
    }

    assert not token_manager.is_token_expired("mal")


def test_sync_lock_prevents_overlapping_process_runs(tmp_path):
    config = settings(tmp_path)
    first_lock = _acquire_process_sync_lock(config)
    assert first_lock is not None
    try:
        assert _acquire_process_sync_lock(config) is None
    finally:
        _release_process_sync_lock(first_lock)

    later_lock = _acquire_process_sync_lock(config)
    assert later_lock is not None
    _release_process_sync_lock(later_lock)


def test_dry_run_serialization_is_deterministic(monkeypatch):
    result = SyncResult(success=True, dry_run=True, errors=["Title Ω"])
    output = []
    monkeypatch.setattr(
        "anilist_mal_sync.sync_service.click.echo",
        lambda message: output.append(message.encode("cp1252")),
    )
    print_sync_results(result, "bidirectional")
    print_sync_results(result, "bidirectional")

    assert output[0] == output[1]
    assert json.loads(output[0])["errors"] == ["Title Ω"]


def test_cli_dry_run_flag_requests_plan_and_prints_json(monkeypatch):
    config = SimpleNamespace(dry_run=False, log_level="INFO", sync_mode="bidirectional")
    calls = {}
    monkeypatch.setattr(cli, "get_settings", lambda: config)
    monkeypatch.setattr(cli, "setup_logging", lambda level: None)
    monkeypatch.setattr(cli, "_wait_for_valid_config", lambda path=None: None)

    def fake_execute_sync(mode, dry_run, settings=None, mal_ids=frozenset()):
        calls.update(mode=mode, dry_run=dry_run, settings=settings, mal_ids=mal_ids)
        return True, SyncResult(success=True, dry_run=dry_run)

    monkeypatch.setattr(cli, "execute_sync", fake_execute_sync)
    result = CliRunner().invoke(cli.main, ["run", "--once", "--dry-run", "--mal-id", "7"])

    assert result.exit_code == 0, result.output
    assert calls["dry_run"] is True
    assert calls["mal_ids"] == frozenset({7})
    assert json.loads(result.output)["dry_run"] is True


@pytest.mark.parametrize(
    ("source", "mode"),
    [("anilist", "anilist-to-mal"), ("mal", "mal-to-anilist"), ("mal", "bidirectional")],
)
def test_active_rewatch_is_reported_without_writes(tmp_path, source, mode):
    anilist_entry = entry(7, anilist_id=70)
    mal_entry = entry(7)
    active_entry = anilist_entry if source == "anilist" else mal_entry
    active_entry.is_rewatching = True
    anilist = FakeClient([anilist_entry])
    mal = FakeClient([mal_entry])

    result = SyncEngine(anilist, mal, settings=settings(tmp_path)).sync(mode)

    assert result.entries_synced == 0
    assert result.skipped_conflicts[0].reason == "active rewatch has an unsupported base status"
    assert anilist.updates == mal.updates == []


def test_anilist_rejects_account_mismatch_before_reading_list(tmp_path, monkeypatch):
    client = AniListClient("token", settings=settings(tmp_path))
    queries = []

    def query(document, variables=None):
        queries.append(document)
        return {"Viewer": {"name": "different-user"}}

    monkeypatch.setattr(client, "_query", query)
    with pytest.raises(ValueError, match="belongs to different-user"):
        client.get_user_anime_list("expected-user")
    assert queries == ["query { Viewer { id name } }"]


def test_anilist_note_update_does_not_send_score_or_progress(tmp_path, monkeypatch):
    client = AniListClient("token", settings=settings(tmp_path))
    captured = {}

    def capture(document, variables):
        captured.update(variables)
        return {"SaveMediaListEntry": {"id": 1}}

    monkeypatch.setattr(client, "_query", capture)
    assert client.update_anime(entry(7, anilist_id=70), fields=["notes"])
    assert captured == {"mediaId": 70, "notes": ""}


def test_bidirectional_history_merges_independent_fields_and_suppresses_readdition(tmp_path):
    class ApplyingClient(FakeClient):
        def update_anime(self, anime_entry, *, fields=None):
            success = super().update_anime(anime_entry, fields=fields)
            for field in fields:
                setattr(self.entries[0], field, getattr(anime_entry, field))
            return success

    anilist = ApplyingClient([entry(7, anilist_id=70, episodes=2)])
    mal = ApplyingClient([entry(7, episodes=2)])
    anilist.account_key, mal.account_key = "1", "2"

    def sync(dry_run=False):
        return SyncEngine(
            anilist, mal, dry_run=dry_run, settings=settings(tmp_path),
            history_directory=tmp_path,
        ).sync("bidirectional")

    assert sync().entries_synced == 0
    anilist.entries[0].episodes_watched = 3
    mal.entries[0].notes = "MAL edit"
    preview = sync(dry_run=True)
    assert [(change.target, change.fields) for change in preview.updates] == [
        ("mal", ["episodes_watched"]), ("anilist", ["notes"]),
    ]
    assert sync().entries_synced == 2
    assert anilist.entries[0].notes == mal.entries[0].notes == "MAL edit"
    assert anilist.entries[0].episodes_watched == mal.entries[0].episodes_watched == 3
    assert sync().planned_changes == 0
    mal.entries.clear()
    result = sync()
    assert result.additions == []
    assert "disappeared" in result.skipped_conflicts[0].reason


def test_mal_incomplete_second_page_aborts_before_list_is_used(tmp_path, monkeypatch):
    config = settings(tmp_path)
    config.mal_username = ""
    client = MALClient("token", settings=config)
    pages = iter([
        {"name": "tester"},
        {"data": [], "paging": {"next": client.base_url + "/users/@me/animelist?offset=1000"}},
        {"paging": {}},
    ])

    list_requests = []

    def get(url, **kwargs):
        if "/animelist" in url:
            list_requests.append(kwargs["params"])
        payload = next(pages)
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: payload)

    monkeypatch.setattr(client.session, "get", get)
    with pytest.raises(ValueError, match="incomplete anime list page"):
        client.get_user_anime_list()
    assert list_requests[0]["nsfw"] == "true"
    assert list_requests[1] == {"nsfw": "true"}


def test_anilist_missing_page_metadata_aborts_before_list_is_used(tmp_path, monkeypatch):
    client = AniListClient("token", settings=settings(tmp_path))
    pages = iter([
        {"Viewer": {"id": 1, "name": "tester"}},
        {"MediaListCollection": {"lists": [], "hasNextChunk": True}},
        {"MediaListCollection": {"lists": []}},
    ])
    monkeypatch.setattr(client, "_query", lambda *args: next(pages))
    with pytest.raises(ValueError, match="incomplete anime list chunk"):
        client.get_user_anime_list()


@pytest.mark.parametrize("mode", ["anilist-to-mal", "bidirectional"])
def test_targeted_sync_does_not_write_other_titles(tmp_path, mode):
    anilist = FakeClient([entry(7, anilist_id=70), entry(8, anilist_id=80)])
    mal = FakeClient()
    result = SyncEngine(
        anilist, mal, settings=settings(tmp_path), mal_ids=frozenset({7}),
    ).sync(mode)
    assert result.entries_synced == 1
    assert [updated.mal_id for updated in mal.updates] == [7]


def test_readiness_reports_only_service_still_needing_authorization(tmp_path, monkeypatch):
    config = settings(tmp_path)

    def authorize(service, settings, token_manager):
        if service == "anilist":
            token_manager.set_tokens(service, "fixture-token", expires_in=3600)
            return True
        return False

    monkeypatch.setattr("anilist_mal_sync.sync_service.run_oauth_flow", authorize)
    completed, result = execute_sync("bidirectional", dry_run=True, settings=config)
    assert not completed
    assert result.authentication_required == ["mal"]


@pytest.mark.parametrize(("anilist_status", "mal_status"), [
    ("CURRENT", "watching"),
    ("COMPLETED", "completed"),
    ("PAUSED", "on_hold"),
    ("DROPPED", "dropped"),
    ("PLANNING", "plan_to_watch"),
])
def test_watch_status_round_trip(tmp_path, monkeypatch, anilist_status, mal_status):
    config = settings(tmp_path)
    anilist = AniListClient("fixture-token", config)
    mal = MALClient("fixture-token", config)
    al_entry = anilist._parse_entry({
        "status": anilist_status, "media": {"id": 70, "idMal": 7, "title": {"english": "Test"}},
    })
    mal_entry = mal._parse_entry({
        "list_status": {"status": mal_status}, "node": {"id": 7, "title": "Test"},
    }).model_copy(update={"anilist_id": 70})
    assert al_entry.status == mal_entry.status

    def mutate_anilist(document, variables):
        assert variables["status"] == anilist_status
        return {"SaveMediaListEntry": {"id": 1}}

    def patch_mal(url, *, data, **kwargs):
        assert data["status"] == mal_status
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None)

    monkeypatch.setattr(anilist, "_query", mutate_anilist)
    monkeypatch.setattr(mal.session, "patch", patch_mal)
    assert anilist.update_anime(mal_entry, fields=["status"])
    assert mal.update_anime(al_entry, fields=["status"])


def test_real_clients_round_trip_supported_fields_and_settle(tmp_path, monkeypatch):
    config = settings(tmp_path)
    config.mal_username = ""
    anilist = AniListClient("fixture-token", config)
    mal = MALClient("fixture-token", config)
    al_state = {
        "id": 1, "status": "CURRENT", "progress": 2, "score": 8.6, "notes": "Test Ω",
        "repeat": 1, "media": {"id": 70, "idMal": 7, "title": {"english": "Test"}},
    }
    mal_state = {
        "status": "plan_to_watch", "num_episodes_watched": 0, "score": 0,
        "comments": "", "num_times_rewatched": 0, "is_rewatching": False,
    }
    writes = []

    def response(payload):
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: payload)

    def graphql(url, *, json, **kwargs):
        document = json["query"]
        if "Viewer" in document:
            return response({"data": {"Viewer": {"id": 11, "name": "test-user"}}})
        if "MediaListCollection" in document:
            return response({"data": {"MediaListCollection": {
                "lists": [{"entries": [al_state]}], "hasNextChunk": False,
            }}})
        assert "SaveMediaListEntry" in document
        values = json["variables"].copy()
        assert values.pop("mediaId") == 70
        if "scoreRaw" in values:
            values["score"] = values.pop("scoreRaw") / 10
        al_state.update(values)
        writes.append("anilist")
        return response({"data": {"SaveMediaListEntry": {"id": 1}}})

    def mal_get(url, **kwargs):
        if url.endswith("/users/@me"):
            return response({"id": 22, "name": "mal-tester"})
        assert url.endswith("/users/@me/animelist")
        return response({"data": [{"node": {"id": 7, "title": "Test"}, "list_status": mal_state}], "paging": {}})

    def mal_patch(url, *, data, **kwargs):
        assert url.endswith("/anime/7/my_list_status")
        values = data.copy()
        if "num_watched_episodes" in values:
            values["num_episodes_watched"] = values.pop("num_watched_episodes")
        mal_state.update(values)
        writes.append("mal")
        return response(mal_state)

    monkeypatch.setattr(anilist.session, "post", graphql)
    monkeypatch.setattr(mal.session, "get", mal_get)
    monkeypatch.setattr(mal.session, "patch", mal_patch)
    engine = SyncEngine(anilist, mal, settings=config, history_directory=tmp_path)

    assert engine.sync("anilist-to-mal").entries_synced == 1
    assert mal_state == {
        "status": "watching", "num_episodes_watched": 2, "score": 9,
        "comments": "Test Ω", "num_times_rewatched": 1, "is_rewatching": False,
    }
    assert engine.sync("bidirectional").planned_changes == 0
    assert al_state["score"] == 8.6

    mal_state.update(status="on_hold", num_episodes_watched=3, score=7, comments="", num_times_rewatched=2)
    assert engine.sync("bidirectional").entries_synced == 1
    assert {key: al_state[key] for key in ("status", "progress", "score", "notes", "repeat")} == {
        "status": "PAUSED", "progress": 3, "score": 7, "notes": "", "repeat": 2,
    }
    assert engine.sync("bidirectional").planned_changes == 0
    assert writes == ["mal", "anilist"]


def test_bidirectional_rewatch_start_uses_status_progress_and_count(tmp_path):
    completed = WatchStatus.COMPLETED
    anilist = FakeClient([entry(7, anilist_id=70, status=completed, episodes=5)])
    mal = FakeClient([entry(7, status=completed, episodes=5)])
    anilist.account_key, mal.account_key = "11", "22"
    engine = SyncEngine(anilist, mal, settings=settings(tmp_path), history_directory=tmp_path)
    assert engine.sync("bidirectional").planned_changes == 0

    anilist.entries[0].is_rewatching = True
    anilist.entries[0].episodes_watched = 1
    result = engine.sync("bidirectional")
    assert result.entries_synced == 1
    assert result.updates[0].fields == ["status", "is_rewatching", "episodes_watched", "rewatched"]
    assert mal.updates[0].is_rewatching


def test_rewatch_transition_conflicts_with_other_side_progress_edit(tmp_path):
    completed = WatchStatus.COMPLETED
    anilist = FakeClient([entry(7, anilist_id=70, status=completed, episodes=5)])
    mal = FakeClient([entry(7, status=completed, episodes=5)])
    anilist.account_key, mal.account_key = "11", "22"
    engine = SyncEngine(anilist, mal, settings=settings(tmp_path), history_directory=tmp_path)
    assert engine.sync("bidirectional").planned_changes == 0

    anilist.entries[0].is_rewatching = True
    anilist.entries[0].episodes_watched = 1
    mal.entries[0].episodes_watched = 4
    result = engine.sync("bidirectional")
    assert result.planned_changes == 0
    assert result.skipped_conflicts[0].fields == ["is_rewatching", "episodes_watched"]
    assert not anilist.updates and not mal.updates


def test_mal_finish_without_increased_count_is_not_written_to_anilist(tmp_path):
    completed = WatchStatus.COMPLETED
    al_entry = entry(7, anilist_id=70, status=completed, episodes=2)
    al_entry.is_rewatching = True
    anilist = FakeClient([al_entry])
    mal = FakeClient([entry(7, status=completed, episodes=5)])
    engine = SyncEngine(anilist, mal, settings=settings(tmp_path))
    result = engine.sync("mal-to-anilist")
    assert not anilist.updates
    assert "without an increased count" in result.skipped_conflicts[0].reason

    mal.entries[0].rewatched = 1
    result = engine.sync("mal-to-anilist")
    assert result.entries_synced == 1
    assert result.updates[0].fields == ["status", "is_rewatching", "episodes_watched", "rewatched"]
    assert anilist.updates[0].rewatched == 1
