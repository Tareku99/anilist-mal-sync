import asyncio

import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPBasicCredentials

from anilist_mal_sync import web


def test_dashboard_requires_password_and_write_header(monkeypatch):
    monkeypatch.setenv("SYNC_DASHBOARD_PASSWORD", "test-password")
    credentials = HTTPBasicCredentials(username="admin", password="test-password")
    post = Request({"type": "http", "method": "POST", "headers": []})

    with pytest.raises(HTTPException) as denied:
        web._check_dashboard_access(post, None)
    assert denied.value.status_code == 401
    with pytest.raises(HTTPException) as missing_header:
        web._check_dashboard_access(post, credentials)
    assert missing_header.value.status_code == 403
    post = Request({
        "type": "http", "method": "POST", "headers": [(b"x-sync-dashboard", b"1")],
    })
    web._check_dashboard_access(post, credentials)


def test_dashboard_masks_secrets_and_rejects_invalid_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    original = "anilist:\n  client_secret: test-secret\nsync:\n  mode: bidirectional\n"
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(web, "_get_config_path", lambda: path)
    monkeypatch.setattr(web, "reload_settings", lambda: None)

    response = asyncio.run(web.get_config())
    assert "test-secret" not in response["config"]
    invalid = response["config"].replace("bidirectional", "typo")
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(web.update_config(web.ConfigUpdate(config=invalid)))
    assert rejected.value.status_code == 400
    assert path.read_text(encoding="utf-8") == original

    updated = response["config"].replace("bidirectional", "anilist-to-mal")
    asyncio.run(web.update_config(web.ConfigUpdate(config=updated)))
    assert "test-secret" in path.read_text(encoding="utf-8")
