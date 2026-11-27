"""
Web UI for AniList–MAL Sync
Provides a simple dashboard for monitoring and controlling the sync service.
"""

import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Annotated

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ValidationError

from .config import Config, get_settings, reload_settings
from .models import SyncResult
from .sync_service import execute_sync, print_sync_results

logger = logging.getLogger(__name__)

_basic_auth = HTTPBasic(auto_error=False)
_SECRET_MASK = "<stored secret>"


def _check_dashboard_access(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic_auth)],
):
    password = os.environ.get("SYNC_DASHBOARD_PASSWORD")
    if password and (
        credentials is None
        or credentials.username != "admin"
        or not secrets.compare_digest(credentials.password.encode(), password.encode())
    ):
        raise HTTPException(401, "Dashboard sign-in required", headers={"WWW-Authenticate": "Basic"})
    if request.method == "POST" and request.headers.get("X-Sync-Dashboard") != "1":
        raise HTTPException(403, "Dashboard write header required")


app = FastAPI(
    title="AniList–MAL Sync", version="0.1.0",
    dependencies=[Depends(_check_dashboard_access)],
)


def _mask_config_secrets(config: dict, existing: dict | None = None) -> None:
    """Mask existing secrets on read and restore untouched secrets on save."""
    for service in ("anilist", "mal", "myanimelist"):
        section = config.get(service)
        if not isinstance(section, dict) or "client_secret" not in section:
            continue
        if existing is None and section["client_secret"]:
            section["client_secret"] = _SECRET_MASK
        elif existing is not None and section["client_secret"] == _SECRET_MASK:
            stored = (existing.get(service) or {}).get("client_secret")
            if not stored:
                raise ValueError("No saved secret exists for this service")
            section["client_secret"] = stored

# Global state for sync status
sync_status = {
    "running": False,
    "last_sync": None,
    "last_successful_sync": None,
    "next_sync": None,
    "last_result": None,
    "total_syncs": 0,
    "preview": None,
    "last_manual_success": None,
}

# Threading lock to prevent concurrent syncs
_sync_lock = threading.Lock()

# Store CLI sync mode and dry_run (set by CLI when web UI starts)
_cli_sync_mode = None
_cli_dry_run = None


def _get_config_path() -> Path:
    """Get config file path based on environment."""
    if os.path.exists("/.dockerenv"):
        return Path("/app/data/config.yaml")
    return Path("data/config.yaml")


class SyncStatus(BaseModel):
    """Sync status response model"""
    running: bool
    last_sync: str | None = None
    last_successful_sync: str | None = None
    next_sync: str | None = None
    last_result: str | None = None
    total_syncs: int
    sync_in_progress: bool = False  # True if any sync (scheduled or manual) is running
    preview_only: bool = False
    last_manual_success: bool | None = None
    preview: SyncResult | None = None


class ConfigUpdate(BaseModel):
    """Config update request model"""
    config: str


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Main dashboard page"""
    html_content = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AniList–MAL Sync Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        [hidden] { display: none !important; }
        body {
            min-height: 100vh;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%) fixed;
            padding: 15px;
            display: flex;
            flex-direction: column;
        }
        .container { 
            width: min(100%, 1300px);
            margin: 0 auto; 
            padding: 0 10px; 
            flex: 1;
            display: flex;
            flex-direction: column;
            min-height: 0;
        }
        .header {
            text-align: center;
            color: white;
            margin-bottom: 15px;
            flex-shrink: 0;
        }
        .header h1 { font-size: 2em; margin-bottom: 8px; }
        .header p { opacity: 0.9; font-size: 0.95em; }
        .cards {
            display: grid;
            grid-template-columns: minmax(0, 1fr) minmax(0, 2fr);
            gap: 20px;
            margin-bottom: 15px;
            flex: 1;
            min-height: 0;
        }
        @media (max-width: 1024px) {
            .cards { grid-template-columns: minmax(0, 1fr); }
        }
        .card {
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
            display: flex;
            flex-direction: column;
            height: fit-content;
        }
        .card.config-card {
            min-height: 25rem;
            display: flex;
            flex-direction: column;
        }
        .card h2 {
            color: #667eea;
            margin-bottom: 15px;
            font-size: 1.3em;
        }
        .status-item {
            display: flex;
            justify-content: space-between;
            gap: 0.75rem;
            padding: 10px 0;
            border-bottom: 1px solid #eee;
        }
        .status-item:last-child { border-bottom: none; }
        .status-label { font-weight: 600; color: #555; }
        .status-value { color: #333; text-align: right; overflow-wrap: anywhere; }
        .status-running { color: #10b981; font-weight: bold; }
        .status-stopped { color: #ef4444; font-weight: bold; }
        .btn {
            display: inline-block;
            padding: 12px 24px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 16px;
            font-weight: 600;
            width: 100%;
            margin-top: 10px;
        }
        .btn:hover:not(:disabled) { background: #5568d3; }
        .btn:disabled {
            background: #ccc;
            cursor: not-allowed;
            opacity: 0.6;
        }
        .btn-secondary { background: #764ba2; }
        .btn-secondary:hover:not(:disabled) { background: #643a8a; }
        .config-editor {
            width: 100%;
            flex: 1;
            min-height: 18rem;
            font-family: 'Courier New', monospace;
            font-size: 15px;
            line-height: 1.6;
            padding: 15px;
            border: 2px solid #e5e7eb;
            border-radius: 6px;
            resize: none;
            overflow-y: auto;
            overflow-x: hidden;
            white-space: pre-wrap;
            word-wrap: break-word;
            box-sizing: border-box;
        }
        .config-editor:focus {
            outline: none;
            border-color: #667eea;
        }
        .message {
            padding: 12px;
            border-radius: 6px;
            margin-top: 15px;
            display: none;
        }
        .message.success {
            background: #d1fae5;
            color: #065f46;
            border: 1px solid #10b981;
        }
        .message.error {
            background: #fee2e2;
            color: #991b1b;
            border: 1px solid #ef4444;
        }
        .message.show { display: block; }
        .info-note {
            padding: 12px;
            background: #eff6ff;
            color: #1e3a8a;
            border: 1px solid #3b82f6;
            border-radius: 6px;
            margin-top: 15px;
            font-size: 14px;
            line-height: 1.5;
        }
        .footer {
            text-align: center;
            color: white;
            margin-top: 10px;
            opacity: 0.8;
            flex-shrink: 0;
        }
        .footer a:hover {
            opacity: 1;
            text-decoration: underline;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🔄 AniList–MAL Sync</h1>
            <p>Monitor and control your anime list synchronization</p>
        </div>

        <div class="cards">
            <div class="card">
                <h2>📊 Status</h2>
                <div class="status-item">
                    <span class="status-label">Scheduler:</span>
                    <span class="status-value" id="status-running">Loading...</span>
                </div>
                <div class="status-item">
                    <span class="status-label">Last Attempt:</span>
                    <span class="status-value" id="status-last-sync">-</span>
                </div>
                <div class="status-item">
                    <span class="status-label">Last Successful Sync:</span>
                    <span class="status-value" id="status-last-success">-</span>
                </div>
                <div class="status-item">
                    <span class="status-label">Next Sync:</span>
                    <span class="status-value" id="status-next-sync">-</span>
                </div>
                <div class="status-item">
                    <span class="status-label">Last Result:</span>
                    <span class="status-value" id="status-last-result">-</span>
                </div>
                <div class="status-item">
                    <span class="status-label">Total Attempts:</span>
                    <span class="status-value" id="status-total-syncs">0</span>
                </div>
                <button class="btn btn-secondary" id="sync-now-btn" onclick="triggerSync(false)" disabled>▶️ Sync Now</button>
                <button class="btn" id="preview-btn" onclick="triggerSync(true)" disabled>Preview Sync</button>
                <button class="btn" id="refresh-btn" onclick="refreshStatus()">🔄 Refresh Status</button>
                <div class="message" id="sync-message" role="status"></div>
                <div class="message" id="status-message" role="status"></div>
                <details id="preview-details" hidden>
                    <summary>Last preview</summary>
                    <pre id="preview-content" style="white-space: pre-wrap; max-height: 18rem; overflow: auto"></pre>
                </details>
            </div>

            <div class="card config-card">
                <h2>⚙️ Configuration</h2>
                <div style="flex: 1; display: flex; flex-direction: column; min-height: 0;">
                    <textarea class="config-editor" id="config-editor" aria-label="YAML configuration" placeholder="Loading configuration..." disabled></textarea>
                </div>
                <button class="btn" id="reload-config-btn" onclick="loadConfig()">📥 Reload Config</button>
                <button class="btn btn-secondary" id="save-config-btn" onclick="saveConfig()" disabled>💾 Save Config</button>
                <div class="message" id="config-message" role="status"></div>
                <div class="info-note">
                    ℹ️ <strong>Note:</strong> Most configuration changes are automatically reloaded. CLI parameters (interval, port, host) require a manual restart.
                </div>
            </div>
        </div>

        <div class="footer">
            <p>AniList–MAL Sync v0.1.0 | Made with ❤️</p>
            <p style="margin-top: 10px;">
                <a href="https://ko-fi.com/tareku" target="_blank" rel="noopener noreferrer" style="color: white; text-decoration: none; opacity: 0.9; transition: opacity 0.2s;">
                    ☕ Support the project on <strong>Ko-fi</strong>
                </a>
            </p>
        </div>
    </div>

    <script>
        const byId = (id) => document.getElementById(id);
        let currentStatus = null;
        let statusRequest = null;
        let pollTimer;
        let action = null;
        let starting = false;
        let configBusy = false;
        let configLoaded = false;

        async function requestJson(url, options = {}) {
            const response = await fetch(url, {
                cache: 'no-store', signal: AbortSignal.timeout(10000), ...options
            });
            const data = await response.json().catch(() => null);
            if (!response.ok || !data) {
                throw new Error(data?.detail || 'Request failed (' + response.status + ')');
            }
            return data;
        }

        function showMessage(id, message, type = '') {
            const element = byId(id);
            element.textContent = message;
            element.className = message ? 'message show ' + type : 'message';
        }

        function renderButtons() {
            const busy = Boolean(action || currentStatus?.sync_in_progress);
            const syncBtn = byId('sync-now-btn');
            const previewBtn = byId('preview-btn');
            syncBtn.hidden = Boolean(currentStatus?.preview_only);
            syncBtn.disabled = !currentStatus || busy;
            previewBtn.disabled = !currentStatus || busy;
            syncBtn.textContent = action === 'sync' ? 'Syncing…' : 'Sync Now';
            previewBtn.textContent = action === 'preview' ? 'Previewing…' : 'Preview Sync';
        }

        function renderStatus(data) {
            byId('status-running').textContent = data.running ? 'Running' : 'Inactive';
            byId('status-running').className = 'status-value ' + (data.running ? 'status-running' : 'status-stopped');
            byId('status-last-sync').textContent = data.last_sync || '—';
            byId('status-last-success').textContent = data.last_successful_sync || '—';
            byId('status-next-sync').textContent = data.next_sync || '—';
            byId('status-last-result').textContent = data.last_result || '—';
            byId('status-total-syncs').textContent = data.total_syncs;

            const completed = action && !starting && !data.sync_in_progress ? action : null;
            if (completed) {
                action = null;
                const failed = data.last_manual_success === false;
                showMessage('sync-message', data.last_result || 'Finished', failed ? 'error' : 'success');
            } else if (!action && data.sync_in_progress) {
                showMessage('sync-message', 'A sync is already running.');
            } else if (!action && currentStatus?.sync_in_progress) {
                showMessage('sync-message', '');
            }

            const details = byId('preview-details');
            details.hidden = !data.preview;
            if (data.preview) {
                const lines = [];
                for (const [label, entries] of [
                    ['Additions', data.preview.additions], ['Updates', data.preview.updates],
                    ['Conflicts', data.preview.skipped_conflicts],
                    ['Other skips', data.preview.skipped.filter(change => change.reason !== 'already in sync')],
                ]) {
                    if (!entries.length) continue;
                    lines.push(label + ' (' + entries.length + ')');
                    for (const change of entries) {
                        const direction = change.source && change.target ? ' (' + change.source + ' → ' + change.target + ')' : '';
                        const explanation = change.reason || Object.entries(change.differences).map(([field, values]) =>
                            field + ': ' + JSON.stringify(values.from) + ' → ' + JSON.stringify(values.to)
                        ).join(', ');
                        lines.push('• ' + change.title + direction + ': ' + explanation);
                    }
                    lines.push('');
                }
                byId('preview-content').textContent = lines.concat(data.preview.errors).join('\n').trim() || 'No changes planned.';
                if (completed === 'preview') details.open = true;
            }
        }

        function updateStatus() {
            if (statusRequest) return statusRequest;
            clearTimeout(pollTimer);
            statusRequest = (async () => {
                try {
                    const data = await requestJson('/api/status');
                    renderStatus(data);
                    currentStatus = data;
                    showMessage('status-message', '');
                } catch (error) {
                    showMessage('status-message', 'Could not refresh status. Retrying automatically.', 'error');
                } finally {
                    statusRequest = null;
                    renderButtons();
                    pollTimer = setTimeout(updateStatus, action || currentStatus?.sync_in_progress ? 700 : 5000);
                }
            })();
            return statusRequest;
        }

        async function refreshStatus() {
            const button = byId('refresh-btn');
            button.disabled = true;
            button.textContent = 'Refreshing…';
            try {
                await updateStatus();
            } finally {
                button.disabled = false;
                button.textContent = 'Refresh Status';
            }
        }

        function setConfigBusy(operation) {
            configBusy = Boolean(operation);
            byId('config-editor').disabled = configBusy || !configLoaded;
            byId('save-config-btn').disabled = configBusy || !configLoaded;
            byId('reload-config-btn').disabled = configBusy;
            byId('reload-config-btn').textContent = operation === 'load' ? 'Loading…' : 'Reload Config';
            byId('save-config-btn').textContent = operation === 'save' ? 'Saving…' : 'Save Config';
        }

        async function loadConfig() {
            if (configBusy) return;
            setConfigBusy('load');
            try {
                const data = await requestJson('/api/config');
                byId('config-editor').value = data.config;
                configLoaded = true;
                showMessage('config-message', '');
            } catch (error) {
                showMessage('config-message', 'Could not load configuration: ' + error.message, 'error');
            } finally {
                setConfigBusy(null);
            }
        }

        async function saveConfig() {
            if (configBusy || !configLoaded) return;
            setConfigBusy('save');
            try {
                await requestJson('/api/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Sync-Dashboard': '1' },
                    body: JSON.stringify({ config: byId('config-editor').value })
                });
                // Reload the masked version so pasted secrets do not stay in the editor.
                byId('config-editor').value = '';
                configLoaded = false;
                setConfigBusy(null);
                await loadConfig();
                if (configLoaded) showMessage('config-message', 'Configuration saved.', 'success');
                else showMessage('config-message', 'Configuration saved. Reload the editor to see it.', 'error');
            } catch (error) {
                showMessage('config-message', error.message, 'error');
            } finally {
                setConfigBusy(null);
            }
        }

        async function triggerSync(preview) {
            if (action || !currentStatus || currentStatus.sync_in_progress) return;
            action = preview ? 'preview' : 'sync';
            starting = true;
            renderButtons();
            showMessage('sync-message', preview ? 'Preparing preview…' : 'Starting sync…');
            if (preview) byId('preview-details').hidden = true;

            try {
                await requestJson('/api/sync/trigger?preview=' + preview, {
                    method: 'POST', headers: { 'X-Sync-Dashboard': '1' }
                });
                if (statusRequest) await statusRequest;
                starting = false;
                await updateStatus();
            } catch (error) {
                action = null;
                starting = false;
                showMessage('sync-message', error.message, 'error');
                renderButtons();
                await updateStatus();
            }
        }

        window.addEventListener('DOMContentLoaded', () => {
            updateStatus();
            loadConfig();
        });
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html_content)


@app.get("/api/status")
async def get_status() -> SyncStatus:
    """Get current sync status"""
    in_progress = is_sync_running()
    status_dict = sync_status.copy()
    status_dict["sync_in_progress"] = in_progress
    status_dict["preview_only"] = bool(_cli_dry_run)
    if not status_dict["preview_only"]:
        try:
            status_dict["preview_only"] = get_settings().dry_run
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            # Keep status and config editing available while configuration is invalid.
            pass
    return SyncStatus(**status_dict)


@app.get("/api/config")
async def get_config():
    """Get current configuration"""
    try:
        config_path = _get_config_path()
        if not config_path.exists():
            raise HTTPException(status_code=404, detail="Config file not found")
        
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        _mask_config_secrets(config)
        return {"config": yaml.safe_dump(config, sort_keys=False)}
    except HTTPException:
        raise
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        logger.error("Failed to load dashboard configuration")
        raise HTTPException(status_code=500, detail="Could not read configuration")


@app.post("/api/config")
async def update_config(data: ConfigUpdate):
    """Update configuration file"""
    config_path = _get_config_path()
    temp_path = None
    try:
        config = yaml.safe_load(data.config)
        if not isinstance(config, dict):
            raise ValueError("Configuration must be a YAML mapping")
        existing = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        _mask_config_secrets(config, existing or {})
        Config.model_validate(config)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=config_path.parent, delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            yaml.safe_dump(config, temp, sort_keys=False)
        temp_path.replace(config_path)
        reload_settings()
        return {"message": "Configuration updated successfully and reloaded"}
    except ValidationError as error:
        fields = ", ".join(".".join(map(str, item["loc"])) for item in error.errors())
        raise HTTPException(status_code=400, detail=f"Invalid configuration fields: {fields}")
    except (ValueError, yaml.YAMLError):
        raise HTTPException(status_code=400, detail="Invalid configuration YAML or secret placeholder")
    except OSError:
        logger.error("Failed to save dashboard configuration")
        raise HTTPException(status_code=500, detail="Could not save configuration")
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


@app.post("/api/sync/trigger")
async def trigger_sync(preview: bool = False):
    """Trigger an immediate sync"""
    if not acquire_sync_lock():
        raise HTTPException(status_code=409, detail="Sync already in progress")
    
    try:
        settings = get_settings()
        mode = _cli_sync_mode if _cli_sync_mode else settings.sync_mode
        dry_run = preview or bool(_cli_dry_run) or settings.dry_run
        if dry_run:
            sync_status["preview"] = None
        sync_status["last_manual_success"] = None
        
        def run_manual_sync():
            try:
                logger.info(f"[INFO] Manual sync started (mode: {mode})")
                success, result = execute_sync(mode, dry_run=dry_run, settings=settings)
                sync_status["last_manual_success"] = bool(success and result and result.success)
                if dry_run:
                    sync_status["preview"] = result
                
                if success and result:
                    if result.dry_run:
                        print_sync_results(result, mode)
                        result_msg = (
                            f"Dry run: {result.planned_changes} planned, "
                            f"{len(result.skipped_conflicts)} conflicts (Manual)"
                        )
                        logger.info("[INFO] Manual sync completed: %s", result_msg)
                    else:
                        total = result.entries_synced + result.entries_failed
                        if result.success:
                            result_msg = f"{result.entries_synced}/{total} synced, 0 failed"
                            logger.info(f"[INFO] Manual sync completed: {result_msg}")
                        elif result.entries_synced > 0:
                            result_msg = f"{result.entries_synced}/{total} synced, {result.entries_failed} failed"
                            logger.warning(f"[WARNING] Manual sync completed: {result_msg}")
                        else:
                            result_msg = f"0/{total} synced, {result.entries_failed} failed"
                            logger.error(f"[ERROR] Manual sync failed: {result_msg}")
                        result_msg += f", {len(result.skipped_conflicts)} conflicts (Manual)"
                    
                    update_sync_status(
                        last_sync=time.strftime('%Y-%m-%d %H:%M:%S %Z'),
                        last_result=result_msg,
                        successful=result.success and not result.dry_run,
                    )
                else:
                    if result is not None:
                        print_sync_results(result, mode)
                        result_msg = (
                            f"Failed: {result.entries_synced} writes, "
                            f"{result.entries_failed} errors (Manual)"
                        )
                        logger.error("[ERROR] Manual sync failed: %s", result_msg)
                    else:
                        logger.error("[ERROR] Manual sync failed: Could not execute sync")
                        result_msg = "Failed (Manual)"
                    update_sync_status(
                        last_sync=time.strftime('%Y-%m-%d %H:%M:%S %Z'),
                        last_result=result_msg
                    )
            except Exception as e:
                sync_status["last_manual_success"] = False
                logger.error(f"[ERROR] Manual sync failed: {e}")
                update_sync_status(
                    last_sync=time.strftime('%Y-%m-%d %H:%M:%S %Z'),
                    last_result=f"Error: {str(e)} (Manual)"
                )
            finally:
                _sync_lock.release()
        
        threading.Thread(target=run_manual_sync, daemon=True).start()
        return {"message": "Sync triggered successfully"}
    except Exception as e:
        _sync_lock.release()
        logger.error(f"Failed to trigger sync: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def update_sync_status(
    running: bool | None = None, last_sync: str | None = None,
    next_sync: str | None = None, last_result: str | None = None,
    successful: bool = False,
):
    """Update the global sync status (called from CLI)"""
    if running is not None:
        sync_status["running"] = running
    if last_sync:
        sync_status["last_sync"] = last_sync
        if successful:
            sync_status["last_successful_sync"] = last_sync
    if next_sync:
        sync_status["next_sync"] = next_sync
    if last_result:
        sync_status["last_result"] = last_result
        sync_status["total_syncs"] += 1


def set_cli_sync_params(mode: str | None, dry_run: bool):
    """Set the sync mode and dry_run from CLI (called when web UI starts)"""
    global _cli_sync_mode, _cli_dry_run
    _cli_sync_mode = mode
    _cli_dry_run = dry_run


def is_sync_running() -> bool:
    """Check if any sync (scheduled or manual) is currently running"""
    return _sync_lock.locked()


def acquire_sync_lock() -> bool:
    """Try to acquire the sync lock. Returns True if acquired, False if already locked."""
    return _sync_lock.acquire(blocking=False)
