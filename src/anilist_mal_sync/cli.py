"""Command-line interface for AniList-MAL sync."""

import logging
import os
import sys
import time
from pathlib import Path

import click
import yaml

from .config import get_settings, reload_settings, validate_credentials
from .constants import CONFIG_RETRY_INTERVAL_SECONDS, DEFAULT_SYNC_INTERVAL_MINUTES, DEFAULT_WEB_UI_PORT
from .sync_service import execute_sync, print_sync_results, sync_readiness

logger = logging.getLogger(__name__)


def _resolve_sync_interval(cli_interval: int | None, config_interval: int) -> int:
    """Prefer an explicit CLI interval; otherwise use the configured value."""
    return config_interval if cli_interval is None else cli_interval


def _wait_for_valid_config(config_path: str = "data/config.yaml"):
    """
    Wait for configuration to be valid.
    All commands use this to ensure consistent behavior.
    Retries every CONFIG_RETRY_INTERVAL_SECONDS until config is valid.
    Can be interrupted with Ctrl+C.
    """
    retry_count = 0
    retry_interval = CONFIG_RETRY_INTERVAL_SECONDS
    
    while True:
        retry_count += 1
        logger.info(f"[Attempt #{retry_count}] Validating configuration...")
        
        try:
            reload_settings()
            is_valid, invalid_vars = validate_credentials()
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            is_valid, invalid_vars = False, ["config.yaml"]
        
        if is_valid:
            if retry_count > 1:
                logger.info("[OK] Configuration validated successfully!")
            return
        
        logger.error("")
        _show_config_error(invalid_vars, config_path, exit_code=None)
        logger.error("")
        logger.error(f"[INFO] Checking again in {retry_interval} seconds...")
        logger.error("[INFO] Press Ctrl+C to exit")
        logger.error("="*60)
        
        try:
            time.sleep(retry_interval)
        except KeyboardInterrupt:
            logger.info("")
            logger.info("Exiting due to user interrupt")
            sys.exit(1)




def _show_config_error(invalid_vars: list[str], config_path: str = "data/config.yaml", exit_code: int = 1):
    """Display configuration error message and optionally exit."""
    logger.error("="*60)
    logger.error("[ERROR] CONFIGURATION ERROR: Missing or invalid credentials")
    logger.error("="*60)
    logger.error("Missing/invalid variables:")
    for var in invalid_vars:
        logger.error(f"  - {var}")
    logger.error("")
    logger.error("[INFO] Required steps:")
    logger.error("  1. Get AniList credentials: https://anilist.co/settings/developer")
    logger.error("  2. Get MAL credentials: https://myanimelist.net/apiconfig")
    logger.error(f"  3. Edit {config_path} with your credentials")
    logger.error("")
    logger.error("[INFO] Make sure to replace ALL placeholder values")
    logger.error("="*60)
    if exit_code is not None:
        sys.exit(exit_code)


def setup_logging(level: str):
    """Configure logging for the application."""
    # Include module name only in DEBUG mode for cleaner logs
    log_level = getattr(logging, level)
    if log_level == logging.DEBUG:
        format_str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    else:
        format_str = "%(asctime)s - %(levelname)s - %(message)s"
    
    logging.basicConfig(
        level=log_level,
        format=format_str,
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )


@click.group()
@click.version_option(version="0.1.0")
def main():
    """AniList to MyAnimeList sync service."""
    pass





@main.command()
@click.option(
    "--service",
    type=click.Choice(["anilist", "mal", "both"]),
    default="both",
    help="Which service to authenticate with",
)
def auth(service: str):
    """Interactive authentication setup for AniList and MyAnimeList."""
    from .oauth import TokenManager, run_oauth_flow

    settings = get_settings()
    setup_logging("INFO")
    
    # Wait for valid configuration (consistent behavior for all commands)
    _wait_for_valid_config()
    
    click.echo("=== OAuth Authentication Setup ===\n")
    
    # Initialize token manager
    token_manager = TokenManager(settings.token_file)

    # Authenticate with selected service(s)
    success = True
    
    if service in ["anilist", "both"]:
        if not run_oauth_flow("anilist", settings, token_manager):
            success = False
            click.echo("[ERROR] AniList authentication failed", err=True)
    
    if service in ["mal", "both"]:
        if not run_oauth_flow("mal", settings, token_manager):
            success = False
            click.echo("[ERROR] MyAnimeList authentication failed", err=True)
    
    if success:
        click.echo(f"\n[OK] Authentication complete! Tokens saved to {settings.token_file}")
        click.echo("\nYou can now run: anilist-mal-sync run")
    else:
        sys.exit(1)


@main.command()
@click.option(
    "--mode",
    type=click.Choice(["anilist-to-mal", "mal-to-anilist", "bidirectional"]),
    help="Override the configured sync mode",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Simulate sync without making changes",
)
@click.option(
    "--interval",
    type=click.IntRange(min=1),
    default=None,
    help=f"Sync interval in minutes (default: from config or {DEFAULT_SYNC_INTERVAL_MINUTES} = 6 hours)",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
    default="INFO",
    help="Logging level",
)
@click.option(
    "--once",
    is_flag=True,
    help="Run sync once and exit (no web UI, no continuous loop)",
)
@click.option(
    "--mal-id", "mal_ids", type=click.IntRange(min=1), multiple=True,
    help="Limit a one-time sync or preview to the specified MAL ID. Repeat for more titles.",
)
@click.option(
    "--no-web-ui",
    is_flag=True,
    help="Disable web UI (continuous sync only)",
)
@click.option(
    "--port",
    type=int,
    default=DEFAULT_WEB_UI_PORT,
    help=f"Web UI port (default: {DEFAULT_WEB_UI_PORT})",
)
@click.option(
    "--host",
    type=str,
    default="0.0.0.0",
    help="Web UI host",
)
def run(mode: str | None, dry_run: bool, interval: int | None, log_level: str, once: bool, no_web_ui: bool, port: int, host: str, mal_ids: tuple[int, ...]):
    """
    Run sync service (default: continuous sync with web UI).
    
    Default: Runs continuous sync at specified interval with web UI.
    Use --once for single sync, --no-web-ui for headless mode.
    """
    if mal_ids and not once:
        raise click.UsageError("--mal-id requires --once")
    config_path = Path("/app/data/config.yaml") if os.path.exists("/.dockerenv") else Path("data/config.yaml")
    if once or no_web_ui:
        setup_logging(log_level or "INFO")
        _wait_for_valid_config(str(config_path))
    try:
        settings = get_settings()
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        if once or no_web_ui:
            raise
        settings = None
    setup_logging(log_level or (settings.log_level if settings else "INFO"))
    
    # --once: Run sync once and exit (like old sync command)
    if once:
        try:
            run_mode = mode or settings.sync_mode
            success, result = execute_sync(
                run_mode, dry_run=dry_run or settings.dry_run, settings=settings,
                mal_ids=frozenset(mal_ids),
            )
            
            if not success or result is None:
                if result is not None:
                    print_sync_results(result, run_mode)
                logger.error("Sync failed")
                sys.exit(1)
            
            print_sync_results(result, run_mode)
            sys.exit(0 if result.success else 1)
            
        except Exception as e:
            logger.exception("Sync failed with error")
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
    
    # Default or --no-web-ui: Continuous sync loop
    interval_override = interval
    interval = _resolve_sync_interval(
        interval_override, settings.sync_interval if settings else DEFAULT_SYNC_INTERVAL_MINUTES
    )
    interval_seconds = interval * 60
    
    logger.info("="*60)
    logger.info("Starting AniList-MAL Sync Service")
    logger.info("Mode: %s", mode or (settings.sync_mode if settings else "waiting for configuration"))
    logger.info(f"Interval: {interval} minutes ({interval//60}h {interval%60}m)")
    if not no_web_ui:
        # Determine Web UI URL
        web_ui_url = f"http://localhost:{port}"
        try:
            if os.path.exists("/.dockerenv"):
                # In Docker: Use IP from redirect_uri in config
                config_path = Path("/app/data/config.yaml")
                if config_path.exists():
                    with open(config_path, 'r') as f:
                        config = yaml.safe_load(f)
                        redirect_uri = config.get('oauth', {}).get('redirect_uri', '')
                        if redirect_uri and '://' in redirect_uri:
                            from urllib.parse import urlparse
                            parsed = urlparse(redirect_uri)
                            if parsed.hostname and parsed.hostname not in ('localhost', '127.0.0.1'):
                                web_ui_url = f"http://{parsed.hostname}:{port}"
            else:
                # Local: Detect actual local IP address
                import socket
                # Connect to a remote address to determine local IP
                # This doesn't actually send data, just determines which interface would be used
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    # Connect to a non-routable address (doesn't actually connect)
                    s.connect(('8.8.8.8', 80))
                    local_ip = s.getsockname()[0]
                    s.close()
                    if local_ip and local_ip != '127.0.0.1':
                        web_ui_url = f"http://{local_ip}:{port}"
                except Exception:
                    pass
        except Exception:
            pass  # Fall back to localhost if anything fails
        logger.info(f"Web UI: {web_ui_url}")
    logger.info("="*60)
    logger.info("")
    
    # Start web UI if enabled (default)
    if not no_web_ui:
        import threading
        import uvicorn
        from .web import app, update_sync_status, set_cli_sync_params, acquire_sync_lock
        
        # Store CLI sync parameters for manual sync button
        set_cli_sync_params(mode, dry_run)
        
        # Start sync service in background thread
        def sync_service():
            nonlocal interval, interval_seconds
            last_mtime = object()
            config_valid = False
            run_count = 0
            
            # Initialize status
            update_sync_status(running=True)
            # Calculate and set initial next_sync time (when first sync will run)
            next_sync_time = time.localtime(time.time() + interval_seconds)
            next_sync_str = time.strftime('%Y-%m-%d %H:%M:%S %Z', next_sync_time)
            update_sync_status(next_sync=next_sync_str)
            
            while True:
                # Check config file modification time
                try:
                    mtime = config_path.stat().st_mtime
                except Exception:
                    mtime = None
                
                # If config changed or first run, reload and validate
                if last_mtime != mtime:
                    last_mtime = mtime
                    try:
                        current_settings = reload_settings()
                        is_valid, invalid_vars = validate_credentials()
                        if is_valid:
                            if not config_valid:
                                logger.info("[OK] Configuration validated successfully! Resuming sync service...")
                            config_valid = True
                            settings = current_settings
                            interval = _resolve_sync_interval(interval_override, settings.sync_interval)
                            interval_seconds = interval * 60
                        else:
                            logger.error("")
                            _show_config_error(invalid_vars, str(config_path), exit_code=None)
                            logger.error("")
                            logger.error("[ERROR] Configuration invalid. Pausing sync. Waiting for fix...")
                            config_valid = False
                    except Exception as e:
                        logger.error(f"[ERROR] Failed to load config: {e}")
                        logger.error("[ERROR] Configuration invalid. Pausing sync. Waiting for fix...")
                        config_valid = False
                
                # If config is invalid, wait and retry
                if not config_valid:
                    update_sync_status(sync_readiness="Configuration required")
                    time.sleep(10)
                    continue
                
                # Skip scheduled sync if any sync is already running (using lock)
                if not acquire_sync_lock():
                    delay = min(interval_seconds, 30)
                    logger.info("Scheduled sync deferred because another sync is active. Retrying in %s seconds.", delay)
                    update_sync_status(
                        next_sync=time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(time.time() + delay))
                    )
                    try:
                        time.sleep(delay)
                    except KeyboardInterrupt:
                        update_sync_status(running=False)
                        logger.info("Sync service stopped")
                        break
                    continue
                
                run_count += 1
                logger.info(f"Starting sync run #{run_count}...")
                
                try:
                    run_mode = mode or settings.sync_mode
                    success, result = execute_sync(run_mode, dry_run=dry_run or settings.dry_run, settings=settings)
                    update_sync_status(sync_readiness=sync_readiness(result, success))
                    if result is not None and result.accounts:
                        update_sync_status(accounts=result.accounts)
                    if success and result:
                        if result.dry_run:
                            print_sync_results(result, run_mode)
                            last_result = (
                                f"Dry run: {result.planned_changes} planned, "
                                f"{len(result.skipped_conflicts)} conflicts"
                            )
                            update_sync_status(
                                running=True,
                                last_sync=time.strftime('%Y-%m-%d %H:%M:%S %Z'),
                                last_result=last_result,
                            )
                        else:
                            total = result.entries_synced + result.entries_failed
                            skips = f"{len(result.skipped_conflicts)} conflicts, {result.entries_skipped} other skips"
                            if result.success:
                                logger.info(f"Sync run #{run_count} completed: {result.entries_synced}/{total} synced, 0 failed")
                                update_sync_status(
                                    running=True,
                                    last_sync=time.strftime('%Y-%m-%d %H:%M:%S %Z'),
                                    last_result=f"{result.entries_synced}/{total} synced, 0 failed, {skips}",
                                    successful=True,
                                )
                            elif result.entries_synced > 0:
                                logger.warning(f"Sync run #{run_count} completed: {result.entries_synced}/{total} synced, {result.entries_failed} failed")
                                update_sync_status(
                                    running=True,
                                    last_sync=time.strftime('%Y-%m-%d %H:%M:%S %Z'),
                                    last_result=f"{result.entries_synced}/{total} synced, {result.entries_failed} failed, {skips}"
                                )
                            else:
                                logger.error(f"Sync run #{run_count} failed: 0/{total} synced, {result.entries_failed} failed")
                                update_sync_status(
                                    running=True,
                                    last_sync=time.strftime('%Y-%m-%d %H:%M:%S %Z'),
                                    last_result=f"0/{total} synced, {result.entries_failed} failed, {skips}"
                                )
                    else:
                        if result is not None:
                            print_sync_results(result, run_mode)
                            update_sync_status(
                                running=True,
                                last_sync=time.strftime('%Y-%m-%d %H:%M:%S %Z'),
                                last_result=(
                                    f"Failed: {result.entries_synced} writes, "
                                    f"{result.entries_failed} errors"
                                ),
                            )
                        else:
                            logger.error(f"Sync run #{run_count} failed: Could not execute sync")
                            update_sync_status(
                                running=True,
                                last_sync=time.strftime('%Y-%m-%d %H:%M:%S %Z'),
                                last_result="Failed"
                            )
                except Exception as e:
                    logger.error(f"Sync run #{run_count} failed with exception: {e}")
                    update_sync_status(sync_readiness="Error")
                    update_sync_status(
                        running=True,
                        last_sync=time.strftime('%Y-%m-%d %H:%M:%S %Z'),
                        last_result=f"Error: {str(e)}"
                    )
                finally:
                    # Always release the lock when sync completes
                    from .web import _sync_lock
                    _sync_lock.release()
                
                logger.info("")
                logger.info(f"Waiting {interval} minutes until next sync...")
                next_sync_time = time.localtime(time.time() + interval_seconds)
                next_sync_str = time.strftime('%Y-%m-%d %H:%M:%S %Z', next_sync_time)
                logger.info(f"Next sync at: {next_sync_str}")
                update_sync_status(next_sync=next_sync_str)
                logger.info("")
                
                try:
                    time.sleep(interval_seconds)
                except KeyboardInterrupt:
                    update_sync_status(running=False)
                    logger.info("Sync service stopped")
                    break
        
        # Start sync thread
        sync_thread = threading.Thread(target=sync_service, daemon=True)
        sync_thread.start()
        
        # Run FastAPI server (this blocks)
        # Suppress asyncio CancelledError tracebacks during shutdown
        import asyncio
        import signal
        
        def shutdown_handler(sig, frame):
            """Handle shutdown signals gracefully."""
            update_sync_status(running=False)
            print()  # New line
            logger.info("="*60)
            logger.info("Service stopped by user")
            logger.info("="*60)
            # Use os._exit to bypass Python's exception handling and prevent tracebacks
            os._exit(0)
        
        # Register signal handler to catch Ctrl+C before uvicorn handles it
        signal.signal(signal.SIGINT, shutdown_handler)
        
        # Suppress CancelledError and SystemExit tracebacks
        original_excepthook = sys.excepthook
        def custom_excepthook(exc_type, exc_value, exc_traceback):
            # Suppress CancelledError and SystemExit (from our shutdown handler)
            if exc_type is asyncio.CancelledError:
                return
            if exc_type is SystemExit and exc_value.code == 0:
                return
            original_excepthook(exc_type, exc_value, exc_traceback)
        sys.excepthook = custom_excepthook
        
        try:
            uvicorn.run(app, host=host, port=port, log_level="warning")
        except KeyboardInterrupt:
            shutdown_handler(None, None)
        finally:
            sys.excepthook = original_excepthook
    else:
        # --no-web-ui: Continuous sync without web UI (like old run command)
        last_mtime = None
        config_valid = True
        run_count = 0
        
        while True:
            # Check config file modification time
            try:
                mtime = config_path.stat().st_mtime
            except Exception:
                mtime = None
            
            # If config changed or first run, reload and validate
            if last_mtime != mtime:
                last_mtime = mtime
                try:
                    settings = reload_settings()
                    is_valid, invalid_vars = validate_credentials()
                    if is_valid:
                        if not config_valid:
                            logger.info("[OK] Configuration validated successfully! Resuming sync service...")
                        config_valid = True
                        interval = _resolve_sync_interval(interval_override, settings.sync_interval)
                        interval_seconds = interval * 60
                    else:
                        logger.error("")
                        _show_config_error(invalid_vars, str(config_path), exit_code=None)
                        logger.error("")
                        logger.error("[ERROR] Configuration invalid. Pausing sync. Waiting for fix...")
                        config_valid = False
                except Exception as e:
                    logger.error(f"[ERROR] Failed to load config: {e}")
                    logger.error("[ERROR] Configuration invalid. Pausing sync. Waiting for fix...")
                    config_valid = False
            
            # If config is invalid, wait and retry
            if not config_valid:
                time.sleep(10)
                continue
            
            run_count += 1
            logger.info(f"Starting sync run #{run_count}...")
            
            try:
                run_mode = mode or settings.sync_mode
                success, result = execute_sync(run_mode, dry_run=dry_run or settings.dry_run, settings=settings)
                if success and result:
                    if result.dry_run:
                        print_sync_results(result, run_mode)
                        logger.info(
                            "Dry-run plan: %s changes, %s conflicts",
                            result.planned_changes,
                            len(result.skipped_conflicts),
                        )
                    else:
                        total = result.entries_synced + result.entries_failed
                        if result.success:
                            logger.info(f"Sync run #{run_count} completed: {result.entries_synced}/{total} synced, 0 failed")
                        elif result.entries_synced > 0:
                            logger.warning(f"Sync run #{run_count} completed: {result.entries_synced}/{total} synced, {result.entries_failed} failed")
                        else:
                            logger.error(f"Sync run #{run_count} failed: 0/{total} synced, {result.entries_failed} failed")
                else:
                    if result is not None:
                        print_sync_results(result, run_mode)
                        logger.error(
                            "Sync run #%s failed after %s writes and %s errors",
                            run_count,
                            result.entries_synced,
                            result.entries_failed,
                        )
                    else:
                        logger.error(f"Sync run #{run_count} failed: Could not execute sync")
            except Exception as e:
                logger.error(f"Sync run #{run_count} failed with exception: {e}")
            
            logger.info("")
            logger.info(f"Waiting {interval} minutes until next sync...")
            next_sync_time = time.localtime(time.time() + interval_seconds)
            logger.info(f"Next sync at: {time.strftime('%Y-%m-%d %H:%M:%S %Z', next_sync_time)}")
            logger.info("")
            
            try:
                time.sleep(interval_seconds)
            except KeyboardInterrupt:
                logger.info("")
                logger.info("="*60)
                logger.info("Service stopped by user")
                logger.info(f"Total sync runs: {run_count}")
                logger.info("="*60)
                sys.exit(0)


if __name__ == "__main__":
    main()
