"""OAuth authentication helpers for AniList and MyAnimeList."""

import json
import logging
import secrets
import tempfile
import webbrowser
from time import monotonic
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .config import Settings

logger = logging.getLogger(__name__)

# Constants
TOKEN_EXPIRY_BUFFER_SECONDS = 300  # Refresh tokens 5 minutes before expiry
HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403


class TokenManager:
    """Manages OAuth tokens with persistence and auto-refresh."""

    def __init__(self, token_file: Path):
        """Initialize token manager with file path."""
        self.token_file = token_file
        self.data = self._load_tokens()

    def _load_tokens(self) -> dict:
        """Load tokens from file."""
        if self.token_file.exists():
            try:
                with open(self.token_file, "r") as f:
                    data = json.load(f)
                    # Support both old and new format
                    if "tokens" in data:
                        return data
                    else:
                        # Migrate old format to new format
                        return {"tokens": data}
            except Exception as e:
                logger.warning(f"Failed to load tokens: {e}")
        return {"tokens": {}}

    def save_tokens(self):
        """Save tokens to file."""
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.token_file.parent,
                prefix=f"{self.token_file.name}.",
                suffix=".tmp",
                delete=False,
            ) as token_file:
                temp_path = Path(token_file.name)
                json.dump(self.data, token_file, indent=4)
                token_file.flush()
            temp_path.replace(self.token_file)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
        logger.info(f"Tokens saved to {self.token_file}")

    def get_token(self, service: str, token_type: str = "access_token") -> Optional[str]:
        """Get a token for a service."""
        return self.data.get("tokens", {}).get(service, {}).get(token_type)

    def set_tokens(
        self,
        service: str,
        access_token: str,
        refresh_token: Optional[str] = None,
        expires_in: Optional[int] = None,
    ):
        """Set tokens for a service with expiry tracking."""
        if not access_token:
            raise ValueError(f"Cannot save an empty access token for {service}")
        if "tokens" not in self.data:
            self.data["tokens"] = {}
        
        if service not in self.data["tokens"]:
            self.data["tokens"][service] = {}

        self.data["tokens"][service]["access_token"] = access_token
        self.data["tokens"][service]["token_type"] = "Bearer"
        
        if refresh_token:
            self.data["tokens"][service]["refresh_token"] = refresh_token
        
        # Calculate expiry time in RFC3339 format
        if expires_in is not None:
            expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            # Format as RFC3339 (compatible with Go's time.Time)
            self.data["tokens"][service]["expiry"] = expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        else:
            self.data["tokens"][service].pop("expiry", None)

        self.save_tokens()

    def is_token_expired(self, service: str, buffer_seconds: int = TOKEN_EXPIRY_BUFFER_SECONDS) -> bool:
        """Check if token is expired or will expire soon (within buffer)."""
        expiry_str = self.data.get("tokens", {}).get(service, {}).get("expiry")
        if not expiry_str:
            # No expiry info, assume it might be expired
            return True

        try:
            # Accept RFC3339 timestamps both with and without fractional seconds.
            expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            # Add buffer to refresh before actual expiry
            return now >= (expiry - timedelta(seconds=buffer_seconds))
        except Exception as e:
            logger.warning(f"Failed to parse expiry time: {e}")
            return True

    def get_valid_token(
        self, service: str, settings: Settings, refresh_func=None
    ) -> Optional[str]:
        """Get a valid token, refreshing if necessary."""
        # Check if token or refresh credentials exist
        if service not in self.data.get("tokens", {}):
            logger.warning(f"No token found for {service}")
            return None
        access_token = self.get_token(service, "access_token")
        refresh_token = self.get_token(service, "refresh_token")
        if not access_token and not (refresh_func and refresh_token):
            logger.warning(f"No token found for {service}")
            return None

        # Check if token is expired
        if self.is_token_expired(service):
            logger.info(f"Token for {service} is expired or expiring soon, refreshing...")
            if refresh_func and refresh_token:
                try:
                    refresh_func(service, settings, self)
                except Exception as e:
                    logger.error(f"Failed to refresh token for {service}: {e}")
                    return None
                refreshed_token = self.get_token(service, "access_token")
                if not refreshed_token or self.is_token_expired(service):
                    logger.error(f"Refresh did not produce a valid token for {service}")
                    return None
                return refreshed_token
            else:
                logger.warning(f"Cannot refresh token for {service} - no refresh function")
                # An access token with no expiry metadata may still be valid.
                # Let the API verify it instead of forcing an OAuth prompt.
                if not self.data["tokens"][service].get("expiry"):
                    return access_token
                return None

        return access_token


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for OAuth callback."""

    timeout = 5

    def do_GET(self):
        """Handle GET request from OAuth callback."""
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/callback":
            if params.get("state", [None])[0] != self.server.expected_state:
                self.send_error(400, "Invalid OAuth state")
                return
            self.server.callback_received = True
            self.server.auth_code = params.get("code", [None])[0]
            if not self.server.auth_code:
                self.send_error(400, "Authorization was cancelled or no code was received")
                return

            # Send response to browser
            self.send_response(HTTP_OK)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            
            html = """
            <html>
            <head><title>Authentication Successful</title></head>
            <body>
                <h1>Authentication Successful!</h1>
                <p>You can close this window and return to the terminal.</p>
                <script>window.close();</script>
            </body>
            </html>
            """
            self.wfile.write(html.encode())
        else:
            self.send_error(HTTP_NOT_FOUND)

    def log_message(self, format, *args):
        """Suppress HTTP server logging."""
        pass


class AniListOAuth:
    """OAuth flow for AniList."""

    def __init__(self, settings: Settings):
        """Initialize AniList OAuth."""
        self.settings = settings

    def get_authorization_url(self) -> tuple[str, str]:
        """Get authorization URL and state."""
        state = secrets.token_urlsafe(32)
        
        params = {
            "client_id": self.settings.anilist_client_id,
            "redirect_uri": self.settings.oauth_redirect_uri,
            "response_type": "code",
            "state": state,
        }
        
        url = f"{self.settings.anilist_auth_url}?{urlencode(params)}"
        return url, state

    def exchange_code_for_token(self, code: str) -> dict:
        """Exchange authorization code for access token."""
        data = {
            "grant_type": "authorization_code",
            "client_id": self.settings.anilist_client_id,
            "client_secret": self.settings.anilist_client_secret,
            "redirect_uri": self.settings.oauth_redirect_uri,
            "code": code,
        }

        response = requests.post(self.settings.anilist_token_url, json=data, timeout=(5, 30))
        response.raise_for_status()
        
        return response.json()


class MALOAuth:
    """OAuth flow for MyAnimeList with PKCE."""

    def __init__(self, settings: Settings):
        """Initialize MAL OAuth."""
        self.settings = settings
        self.code_verifier = None

    def _generate_pkce_pair(self) -> tuple[str, str]:
        """Generate PKCE code verifier and challenge."""
        # For plain method, challenge = verifier
        code_verifier = secrets.token_urlsafe(64)  # ~86 characters
        code_challenge = code_verifier  # Plain method
        
        return code_verifier, code_challenge

    def get_authorization_url(self) -> tuple[str, str, str]:
        """Get authorization URL, state, and code verifier."""
        state = secrets.token_urlsafe(32)
        self.code_verifier, code_challenge = self._generate_pkce_pair()
        
        params = {
            "response_type": "code",
            "client_id": self.settings.mal_client_id,
            "redirect_uri": self.settings.oauth_redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "plain",  # Try plain first
        }
        
        url = f"{self.settings.mal_auth_url}?{urlencode(params)}"
        return url, state, self.code_verifier

    def exchange_code_for_token(self, code: str, code_verifier: str = None) -> dict:
        """Exchange authorization code for access token."""
        data = {
            "client_id": self.settings.mal_client_id,
            "client_secret": self.settings.mal_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.oauth_redirect_uri,
        }
        
        # Add code_verifier for PKCE
        if code_verifier:
            data["code_verifier"] = code_verifier

        logger.debug(f"Sending token request to {self.settings.mal_token_url}")
        
        response = requests.post(
            self.settings.mal_token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=(5, 30),
        )
        
        if response.status_code != 200:
            logger.error(f"Token exchange failed: {response.status_code}")
        
        response.raise_for_status()
        
        return response.json()

    def refresh_access_token(self, refresh_token: str) -> dict:
        """Refresh access token using refresh token."""
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        logger.info("Refreshing MAL access token...")
        response = requests.post(
            self.settings.mal_token_url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
            auth=(self.settings.mal_client_id, self.settings.mal_client_secret),
            timeout=(5, 30),
        )
        
        if response.status_code != 200:
            logger.error(f"Token refresh failed: {response.status_code}")
        
        response.raise_for_status()
        logger.info("MAL access token refreshed successfully")
        
        return response.json()


def refresh_mal_token(service: str, settings: Settings, token_manager: TokenManager):
    """Refresh MAL token and update storage."""
    refresh_token = token_manager.get_token(service, "refresh_token")
    if not refresh_token:
        raise Exception("No refresh token available")

    oauth = MALOAuth(settings)
    token_data = oauth.refresh_access_token(refresh_token)
    access_token = token_data.get("access_token")
    if not access_token:
        raise ValueError("MAL token refresh response did not include an access token")

    # Update tokens
    token_manager.set_tokens(
        service,
        access_token,
        token_data.get("refresh_token") or refresh_token,
        token_data.get("expires_in"),
    )


def run_oauth_flow(service: str, settings: Settings, token_manager: TokenManager) -> bool:
    """Run OAuth flow for a service."""
    logger.info(f"Starting OAuth flow for {service}...")

    # Initialize OAuth handler
    if service == "anilist":
        oauth = AniListOAuth(settings)
        auth_url, expected_state = oauth.get_authorization_url()
        code_verifier = None
    else:  # mal
        oauth = MALOAuth(settings)
        auth_url, expected_state, code_verifier = oauth.get_authorization_url()

    with HTTPServer(("", settings.oauth_port), OAuthCallbackHandler) as server:
        server.expected_state = expected_state
        server.callback_received = False
        server.auth_code = None
        deadline = monotonic() + 180
        print(f"\n[INFO] Opening browser for {service.upper()} authorization...")
        print(f"If the browser doesn't open, visit this URL:\n{auth_url}\n")
        webbrowser.open(auth_url)
        while not server.callback_received:
            remaining = deadline - monotonic()
            if remaining <= 0:
                logger.error("%s authorization timed out", service)
                return False
            server.timeout = remaining
            server.handle_request()
        code = server.auth_code

    if not code:
        logger.error("%s authorization did not return a code", service)
        return False

    # Exchange code for token
    try:
        if service == "anilist":
            token_data = oauth.exchange_code_for_token(code)
            access_token = token_data.get("access_token")
            refresh_token = None
            expires_in = token_data.get("expires_in")  # AniList returns this in response
        else:  # mal
            token_data = oauth.exchange_code_for_token(
                code, code_verifier
            )
            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")
            expires_in = token_data.get("expires_in")

        # Save tokens with expiry
        token_manager.set_tokens(service, access_token, refresh_token, expires_in)
        
        print(f"[OK] Successfully authenticated with {service.upper()}!")
        return True

    except Exception as e:
        logger.error(f"Failed to exchange code for token: {e}")
        return False
