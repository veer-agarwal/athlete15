"""WHOOP recovery, sleep and strain. Phase 5.

The hardest integration in the project, and the one worth understanding properly.

OAuth 2.0 authorization code flow:
    1. Register an app at developer-dashboard.whoop.com. Set the redirect URI to
       match config.WHOOP_REDIRECT_URI.
    2. Send yourself to the authorize URL in a browser. You approve, WHOOP redirects
       to your redirect URI with a ?code= parameter.
    3. Exchange that code for an access token and a refresh token.
    4. Access tokens expire. Refresh tokens let you get a new one without repeating
       the browser step. Store both, persist them to disk, and refresh on 401.

Two traps specific to WHOOP:

    - Recovery data does not exist until the preceding sleep cycle closes. If the
      6:30 AM job runs before you wake up, you get nothing for that day. Handle the
      empty case rather than crashing, and consider scheduling around your actual
      wake time.
    - The API is on v2. Endpoints are /v2/activity/sleep, /v2/activity/workout, and
      recovery comes through the v2 cycle endpoints. v1 webhooks were removed.

BACKFILL: on first successful connection, pull your full history with a date range
query rather than only fetching today. Unlike training data, this history exists
already and is free to retrieve.
"""

from src import config

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"

SCOPES = [
    "read:recovery",
    "read:sleep",
    "read:workout",
    "read:cycles",
    "read:profile",
]


def build_authorize_url() -> str:
    """Return the URL to open in a browser to start the OAuth flow."""
    raise NotImplementedError("phase 5")


def exchange_code(code: str) -> dict:
    """Trade an authorization code for access and refresh tokens."""
    raise NotImplementedError("phase 5")


def refresh_access_token() -> dict:
    """Use the stored refresh token to get a fresh access token."""
    raise NotImplementedError("phase 5")


def fetch() -> list[dict]:
    """Return the most recent complete day of recovery, sleep and strain.

    Returns an empty list when the sleep cycle has not closed yet. That is a normal
    condition, not an error.
    """
    raise NotImplementedError("phase 5")


def backfill(start_date: str) -> int:
    """Pull all history from start_date to now into daily_metrics.

    Returns the number of days written.
    """
    raise NotImplementedError("phase 5")
