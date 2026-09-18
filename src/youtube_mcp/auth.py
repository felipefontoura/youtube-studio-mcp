"""OAuth 2.0 authentication for YouTube APIs.

Users must provide their own client_secret.json from their Google Cloud project.
On first use, a browser-based OAuth consent flow runs and stores the token locally.
"""

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# All scopes we need across all phases.
#
# `youtube.force-ssl` is MANDATORY for comments and captions. Per Google's
# discovery document (youtube/v3/rest), `commentThreads.list`, `comments.list`,
# `commentThreads.insert` and `comments.insert` accept NO alternative scope, and
# `captions.list`/`captions.download` accept only this or `youtubepartner`. A
# token without it fails those calls with
#   HTTP 403 "Request had insufficient authentication scopes"
# even though `youtube.readonly` is granted and every other tool works.
FORCE_SSL = "https://www.googleapis.com/auth/youtube.force-ssl"
YOUTUBE_PARTNER = "https://www.googleapis.com/auth/youtubepartner"

SCOPES = [
    FORCE_SSL,
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
]

# Scopes needed by specific tools. Kept separate from SCOPES on purpose: a token
# that lacks them must still serve every other tool, so the failure has to be
# raised by the affected tool and not by authenticate().
#
# Comments accept NO alternative — force-ssl or nothing. Captions accept either
# force-ssl or youtubepartner (a different product), so for captions having ONE
# of them is enough; requiring both would block a token that works fine.
COMMENT_SCOPES = [FORCE_SSL]
CAPTION_SCOPE_ALTERNATIVES = [FORCE_SSL, YOUTUBE_PARTNER]

DEFAULT_CONFIG_DIR = Path.home() / ".youtube-mcp"
TOKEN_FILE = "token.json"

# Provision the token without filesystem access to config_dir — the token is
# self-contained (it carries its own client_id/secret/refresh_token), so it can
# be injected as inline JSON by whoever manages the deployment. Used as the
# highest-priority source; the file is the fallback.
TOKEN_JSON_ENV = "YOUTUBE_MCP_TOKEN_JSON"


class AuthError(Exception):
    pass


class YouTubeAuth:
    """Manages OAuth 2.0 credentials and builds API service clients."""

    def __init__(
        self,
        client_secret_path: str | Path | None = None,
        config_dir: str | Path | None = None,
        api_key: str | None = None,
    ):
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        self.token_path = self.config_dir / TOKEN_FILE
        self._credentials: Credentials | None = None

        # Resolve client_secret.json path
        if client_secret_path:
            self.client_secret_path = Path(client_secret_path)
        else:
            env_path = os.environ.get("YOUTUBE_MCP_CLIENT_SECRET")
            if env_path:
                self.client_secret_path = Path(env_path)
            else:
                self.client_secret_path = self.config_dir / "client_secret.json"

        # API key fallback for public-only operations
        self.api_key = api_key or os.environ.get("YOUTUBE_API_KEY")

    def _token_info(self) -> dict | None:
        """Raw token JSON: the env var wins, the file is the fallback.

        Reading the raw JSON (instead of only constructing Credentials) is what
        makes it possible to see the scopes that were ACTUALLY granted.
        """
        env = os.environ.get(TOKEN_JSON_ENV)
        if env:
            try:
                info = json.loads(env)
                if isinstance(info, dict) and info:
                    return info
            except Exception:
                pass
        if not self.token_path.exists():
            return None
        try:
            info = json.loads(self.token_path.read_text())
            return info if isinstance(info, dict) else None
        except Exception:
            return None

    def _load_token(self) -> Credentials | None:
        """Load saved credentials from the env var or the token file."""
        info = self._token_info()
        if not info:
            return None
        try:
            return Credentials.from_authorized_user_info(info, SCOPES)
        except Exception:
            return None

    def granted_scopes(self) -> list[str]:
        """The scopes actually granted on the stored token.

        Read from the raw token JSON on purpose:
        Credentials.from_authorized_user_info() overwrites `.scopes` with the
        list we pass in, so `creds.scopes` reports what we ASKED FOR, not what
        was GRANTED. Trusting it silently hides a missing scope — which is how a
        token without `youtube.force-ssl` still looks fully authorised.
        """
        raw = (self._token_info() or {}).get("scopes")
        return list(raw) if isinstance(raw, list) else []

    def missing_scopes(self, required: list[str]) -> list[str]:
        """Required scopes absent from the token. Unknown grant -> [] (no claim)."""
        granted = self.granted_scopes()
        if not granted:
            return []
        return [s for s in required if s not in granted]

    def require_scopes(self, required: list[str], purpose: str) -> None:
        """Raise a clear AuthError when a tool's required scope is missing.

        ALL of `required` must be granted. Used where the API accepts no
        alternative (comments). Called by the affected tool — NOT by
        authenticate() — so a token that lacks these scopes still serves every
        other tool instead of failing the whole server.
        """
        missing = self.missing_scopes(required)
        if not missing:
            return
        raise AuthError(
            f"{purpose} needs OAuth scope(s) missing from the current token: "
            f"{', '.join(missing)}. Nothing else is affected. The token was "
            f"authorised without them, so the call fails with HTTP 403 "
            f"'insufficient authentication scopes'. Fix: re-run the consent flow "
            f"with the full SCOPES list, then replace {self.token_path} or set "
            f"{TOKEN_JSON_ENV}."
        )

    def require_any_scope(self, alternatives: list[str], purpose: str) -> None:
        """Raise unless at least ONE of `alternatives` is granted.

        For endpoints that accept a choice of scopes — `captions.list` works with
        `youtube.force-ssl` OR `youtubepartner`. Demanding both would block a
        token that works perfectly well.
        """
        missing = self.missing_scopes(alternatives)
        if len(missing) < len(alternatives):
            return  # at least one alternative is granted (or grant is unknown)
        raise AuthError(
            f"{purpose} needs one of these OAuth scopes, and the current token has "
            f"none: {', '.join(alternatives)}. Nothing else is affected. The call "
            f"fails with HTTP 403 'insufficient authentication scopes'. Fix: re-run "
            f"the consent flow with the full SCOPES list, then replace "
            f"{self.token_path} or set {TOKEN_JSON_ENV}."
        )

    def _save_token(self, creds: Credentials):
        """Save credentials to token file."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json())

    def authenticate(self) -> Credentials:
        """Get valid credentials, running OAuth flow if needed.

        Returns valid credentials. Raises AuthError if client_secret.json
        is missing or the flow fails.
        """
        creds = self._load_token()

        if creds and creds.valid:
            self._credentials = creds
            return creds

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_token(creds)
                self._credentials = creds
                return creds
            except Exception as e:
                # Refresh failed, need to re-auth
                pass

        # Need to run the OAuth flow
        if not self.client_secret_path.exists():
            raise AuthError(
                f"client_secret.json not found at {self.client_secret_path}. "
                f"Download it from your Google Cloud Console "
                f"(APIs & Services > Credentials > OAuth 2.0 Client IDs) "
                f"and place it at this path, or set YOUTUBE_MCP_CLIENT_SECRET env var."
            )

        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secret_path), SCOPES
            )
            creds = flow.run_local_server(port=0)
            self._save_token(creds)
            self._credentials = creds
            return creds
        except Exception as e:
            raise AuthError(f"OAuth flow failed: {e}") from e

    @property
    def credentials(self) -> Credentials:
        """Get current credentials, authenticating if needed."""
        if self._credentials and self._credentials.valid:
            return self._credentials
        return self.authenticate()

    def build_youtube_service(self):
        """Build a YouTube Data API v3 service client."""
        return build("youtube", "v3", credentials=self.credentials)

    def build_youtube_analytics_service(self):
        """Build a YouTube Analytics API service client."""
        return build("youtubeAnalytics", "v2", credentials=self.credentials)

    def build_youtube_reporting_service(self):
        """Build a YouTube Reporting API service client."""
        return build("youtubereporting", "v1", credentials=self.credentials)

    def build_public_youtube_service(self):
        """Build a YouTube Data API client using API key only (public data)."""
        if not self.api_key:
            raise AuthError(
                "No API key available. Set YOUTUBE_API_KEY env var for public-only access."
            )
        return build("youtube", "v3", developerKey=self.api_key)

    @property
    def token_available(self) -> bool:
        """True when a token can be read from the env var or the file."""
        return bool(self._token_info())

    def status(self) -> dict:
        """Current auth status, with granted-vs-required scopes spelled out.

        Reports `scopes` as GRANTED (from the token) and `missing_scopes` as the
        ones SCOPES asks for but the token lacks — so an insufficient grant is
        visible here instead of only surfacing as a 403 from one tool.
        """
        info = self._token_info()
        if info:
            creds = self._load_token()
            granted = self.granted_scopes()
            return {
                "authenticated": bool(creds and creds.valid),
                "scopes": granted or (list(creds.scopes) if creds and creds.scopes else []),
                "missing_scopes": self.missing_scopes(SCOPES),
                "token_source": "env" if os.environ.get(TOKEN_JSON_ENV) else "file",
                "token_path": str(self.token_path),
                "expired": bool(creds and creds.expired),
                "has_refresh_token": bool(info.get("refresh_token")),
            }
        return {
            "authenticated": False,
            "token_exists": self.token_path.exists(),
            "client_secret_exists": self.client_secret_path.exists(),
            "client_secret_path": str(self.client_secret_path),
        }
