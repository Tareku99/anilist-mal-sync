"""Health check script for Docker container."""

import logging
import sys

from anilist_mal_sync.config import get_settings
from anilist_mal_sync.oauth import TokenManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AUTH_INSTRUCTION = "   Run: anilist-mal-sync auth"


def main():
    """Check if the configuration loads and both services have credentials."""
    try:
        settings = get_settings()
    except Exception as e:
        logger.error(f"[ERROR] UNHEALTHY: Failed to load configuration: {e}")
        sys.exit(1)
    
    try:
        token_manager = TokenManager(settings.token_file)
        if not (settings.anilist_access_token or token_manager.get_token("anilist")):
            logger.error("[ERROR] UNHEALTHY: AniList access token missing")
            logger.error(AUTH_INSTRUCTION)
            sys.exit(1)
        
        if not (
            settings.mal_access_token or settings.mal_refresh_token
            or token_manager.get_token("mal") or token_manager.get_token("mal", "refresh_token")
        ):
            logger.error("[ERROR] UNHEALTHY: MAL credentials missing")
            logger.error(AUTH_INSTRUCTION)
            sys.exit(1)
        
        logger.info("[OK] Configuration loaded and credentials present")
        sys.exit(0)
        
    except Exception as e:
        logger.error(f"[ERROR] UNHEALTHY: Token validation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
