"""Sync service logic for executing sync operations."""

import json
import logging
import os
from typing import Optional

import click
import requests

from .anilist_client import AniListClient
from .base_client import AuthenticationError
from .config import Settings, get_settings
from .mal_client import MALClient
from .models import SyncResult
from .oauth import TokenManager, refresh_mal_token, run_oauth_flow
from .sync_engine import SyncEngine

logger = logging.getLogger(__name__)


def _acquire_process_sync_lock(settings: Settings):
    """Acquire an OS-backed lock shared by sync processes using this data directory."""
    lock_path = settings.token_file.parent / ".anilist-mal-sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def _release_process_sync_lock(lock_file) -> None:
    """Release the OS-backed sync lock."""
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    lock_file.close()


def authenticate_services(settings: Settings, token_manager: TokenManager) -> tuple[Optional[str], Optional[str]]:
    """Authenticate both services and return tokens.
    
    Returns:
        tuple: (anilist_token, mal_token) or (None, None) if failed
    """
    logger.info("No authentication found - starting automatic OAuth flow...")
    logger.info("")
    
    auth_success = True
    
    for service in ["anilist", "mal"]:
        logger.info(f"Authenticating {service.upper()}...")
        try:
            if not run_oauth_flow(service, settings, token_manager):
                logger.error(f"Failed to authenticate {service.upper()}")
                auth_success = False
        except Exception as auth_error:
            logger.error(f"Error during {service.upper()} authentication: {auth_error}")
            auth_success = False
    
    if not auth_success:
        logger.error("Authentication failed. Please try running: anilist-mal-sync auth")
        return None, None
    
    # Reload tokens after successful auth
    anilist_token = token_manager.get_valid_token("anilist", settings)
    mal_token = token_manager.get_valid_token("mal", settings, refresh_mal_token)
    
    if not anilist_token or not mal_token:
        logger.error("Failed to load tokens after authentication")
        return None, None
    
    logger.info("")
    logger.info("Authentication successful! Starting sync...")
    logger.info("")
    return anilist_token, mal_token


def reauthenticate_services(settings: Settings, token_manager: TokenManager) -> tuple[Optional[str], Optional[str]]:
    """Re-authenticate both services and return tokens.
    
    Returns:
        tuple: (anilist_token, mal_token) or (None, None) if failed
    """
    logger.error("="*60)
    logger.error("AUTHENTICATION FAILED: Tokens are invalid or expired")
    logger.error("="*60)
    logger.error("")
    logger.error("Starting automatic re-authentication...")
    logger.error("Please complete the OAuth flow in your browser.")
    logger.error("")
    
    reauth_success = True
    
    for service in ["anilist", "mal"]:
        logger.info(f"Re-authenticating {service.upper()}...")
        try:
            if not run_oauth_flow(service, settings, token_manager):
                logger.error(f"Failed to re-authenticate {service.upper()}")
                reauth_success = False
        except Exception as auth_error:
            logger.error(f"Error during {service.upper()} re-auth: {auth_error}")
            reauth_success = False
    
    if not reauth_success:
        logger.error("")
        logger.error("="*60)
        logger.error("Re-authentication failed")
        logger.error("Please run manually: anilist-mal-sync auth")
        logger.error("="*60)
        return None, None
    
    logger.info("")
    logger.info("="*60)
    logger.info("Re-authentication successful! Retrying sync...")
    logger.info("="*60)
    
    # Reload tokens after successful re-auth
    anilist_token = token_manager.get_valid_token("anilist", settings)
    mal_token = token_manager.get_valid_token("mal", settings, refresh_mal_token)
    
    return anilist_token, mal_token


def _refresh_rejected_token(
    service: str,
    settings: Settings,
    token_manager: TokenManager,
    anilist_token: Optional[str],
    mal_token: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Refresh the rejected service token, falling back to its OAuth flow."""
    if service == "mal":
        env_refresh_token = getattr(settings, "mal_refresh_token", None)
        if env_refresh_token and not token_manager.get_token("mal", "refresh_token"):
            token_manager.data.setdefault("tokens", {}).setdefault("mal", {})[
                "refresh_token"
            ] = env_refresh_token
            token_manager.save_tokens()
        refreshed = False
        if token_manager.get_token("mal", "refresh_token"):
            try:
                refresh_mal_token("mal", settings, token_manager)
                refreshed = bool(
                    token_manager.get_token("mal", "access_token")
                    and not token_manager.is_token_expired("mal")
                )
            except Exception as error:
                logger.warning("MAL refresh token was rejected; starting OAuth again: %s", error)
        if not refreshed:
            if not run_oauth_flow("mal", settings, token_manager):
                return None, None
        mal_token = token_manager.get_token("mal", "access_token")
    elif service == "anilist":
        if not run_oauth_flow("anilist", settings, token_manager):
            return None, None
        anilist_token = token_manager.get_token("anilist", "access_token")
    else:
        return reauthenticate_services(settings, token_manager)

    return anilist_token, mal_token


def _authentication_failure_result(
    dry_run: bool,
    partial_result: SyncResult | None,
    successful_writes: int,
    service: str,
) -> SyncResult:
    """Preserve partial progress when authentication recovery cannot finish."""
    result = (
        partial_result.model_copy(deep=True)
        if partial_result is not None
        else SyncResult(success=False, dry_run=dry_run)
    )
    result.success = False
    result.entries_synced = successful_writes
    result.entries_failed += 1
    result.errors.append(f"Authentication recovery failed for {service}")
    return result


def _execute_sync_unlocked(
    mode: str,
    dry_run: bool = False,
    settings: Optional[Settings] = None,
    anilist_token: Optional[str] = None,
    mal_token: Optional[str] = None,
    token_manager: Optional[TokenManager] = None,
) -> tuple[bool, Optional[SyncResult]]:
    """Execute a sync operation.
    
    Args:
        mode: Sync mode (anilist-to-mal, mal-to-anilist, bidirectional)
        dry_run: If True, don't make actual changes
        settings: Settings instance (will load if not provided)
        anilist_token: AniList access token (will load if not provided)
        mal_token: MAL access token (will load if not provided)
        token_manager: TokenManager instance (will create if not provided)
    
    Returns:
        tuple: (success, result) - success indicates if sync completed, result is SyncResult or None if failed
    """
    if settings is None:
        settings = get_settings()
    
    if token_manager is None:
        token_manager = TokenManager(settings.token_file)

    env_refresh_token = getattr(settings, "mal_refresh_token", None)
    if env_refresh_token and not token_manager.get_token("mal", "refresh_token"):
        token_manager.data.setdefault("tokens", {}).setdefault("mal", {})[
            "refresh_token"
        ] = env_refresh_token
        token_manager.save_tokens()
    
    # Load tokens if not provided
    if anilist_token is None:
        anilist_token = (
            settings.anilist_access_token 
            or token_manager.get_valid_token("anilist", settings)
        )
    
    if mal_token is None:
        mal_token = (
            settings.mal_access_token 
            or token_manager.get_valid_token("mal", settings, refresh_mal_token)
        )
    
    # Authenticate if tokens are missing
    if not anilist_token or not mal_token:
        anilist_token, mal_token = authenticate_services(settings, token_manager)
        if not anilist_token or not mal_token:
            return False, None
    
    successful_writes_before_retry = 0
    last_partial_result = None
    for attempt in range(2):
        try:
            logger.info("Initializing API clients...")
            anilist_client = AniListClient(anilist_token, settings=settings)
            mal_client = MALClient(mal_token, settings=settings)
            engine = SyncEngine(
                anilist_client,
                mal_client,
                dry_run=dry_run,
                settings=settings,
            )
            result = engine.sync(mode)
            result.entries_synced += successful_writes_before_retry
            return True, result
        except AuthenticationError as error:
            partial_result = error.partial_result
            if partial_result is not None:
                successful_writes_before_retry += partial_result.entries_synced
                last_partial_result = partial_result
            if attempt:
                logger.error(
                    "Sync still failed after refreshing %s authentication; %s earlier writes completed",
                    error.service_name,
                    successful_writes_before_retry,
                )
                return False, _authentication_failure_result(
                    dry_run,
                    partial_result or last_partial_result,
                    successful_writes_before_retry,
                    error.service_name,
                )
            logger.warning(
                "%s rejected its token mid-sync after %s successful writes; "
                "refreshing and restarting from fresh list data",
                error.service_name,
                successful_writes_before_retry,
            )
            anilist_token, mal_token = _refresh_rejected_token(
                error.service_name,
                settings,
                token_manager,
                anilist_token,
                mal_token,
            )
            if not anilist_token or not mal_token:
                return False, _authentication_failure_result(
                    dry_run,
                    last_partial_result,
                    successful_writes_before_retry,
                    error.service_name,
                )
        except requests.HTTPError:
            raise
        except Exception:
            logger.exception("Sync failed with error")
            raise

    return False, None


def execute_sync(
    mode: str,
    dry_run: bool = False,
    settings: Optional[Settings] = None,
    anilist_token: Optional[str] = None,
    mal_token: Optional[str] = None,
    token_manager: Optional[TokenManager] = None,
) -> tuple[bool, Optional[SyncResult]]:
    """Serialize syncs across processes sharing the configured token directory."""
    if settings is None:
        settings = get_settings()
    lock_file = _acquire_process_sync_lock(settings)
    if lock_file is None:
        result = SyncResult(
            success=False,
            entries_failed=1,
            errors=["Another sync process is already running for this data directory"],
            dry_run=dry_run,
        )
        return False, result
    try:
        return _execute_sync_unlocked(
            mode,
            dry_run=dry_run,
            settings=settings,
            anilist_token=anilist_token,
            mal_token=mal_token,
            token_manager=token_manager,
        )
    finally:
        _release_process_sync_lock(lock_file)


def print_sync_results(result, mode: str):
    """Print sync results to console."""
    if result.dry_run:
        payload = {
            "mode": mode,
            "dry_run": True,
            "success": result.success,
            "counts": {
                "additions": len(result.additions),
                "updates": len(result.updates),
                "skipped_conflicts": len(result.skipped_conflicts),
                "skipped": len(result.skipped),
                "planned_changes": result.planned_changes,
                "failures": result.entries_failed,
            },
            "additions": [change.model_dump(mode="json") for change in result.additions],
            "updates": [change.model_dump(mode="json") for change in result.updates],
            "skipped_conflicts": [
                change.model_dump(mode="json") for change in result.skipped_conflicts
            ],
            "skipped": [change.model_dump(mode="json") for change in result.skipped],
            "errors": result.errors,
        }
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
        return
    
    click.echo("\n=== Sync Results ===")
    click.echo(f"Mode: {mode}")
    click.echo(f"Success: {result.success}")
    click.echo(f"Entries synced: {result.entries_synced}")
    click.echo(f"Entries failed: {result.entries_failed}")
    
    if result.errors:
        click.echo(f"\nErrors ({len(result.errors)}):")
        for error in result.errors[:10]:  # Show first 10
            click.echo(f"  - {error}")
