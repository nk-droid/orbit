"""Connecting a Google account and keeping its access token fresh."""

import contextlib
import os
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from . import tokens
from .client import load_client
from .scopes import scopes_for

# With include_granted_scopes, Google returns earlier grants alongside the scopes
# just requested. oauthlib treats that difference as an error unless told otherwise.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# Refresh this long before expiry, so no caller is handed a token about to lapse.
REFRESH_MARGIN = timedelta(minutes=2)

CONSENT_TIMEOUT_SECONDS = 300
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Google only returns a refresh token with offline access and an explicit consent
# prompt. include_granted_scopes keeps services connected earlier when another
# service, or write access, is added later.
AUTHORIZATION_PARAMS = {
    "access_type": "offline",
    "prompt": "consent",
    "include_granted_scopes": "true",
}


class ReconnectRequiredError(Exception):
    """The stored grant no longer works; the user has to sign in again."""


def connect(
    services: Iterable[str], *, write: bool = False, open_browser: bool = True
) -> Credentials:
    """Opens Google's consent screen in the browser and stores the resulting tokens.

    Listens on a free loopback port for the redirect, as Google's "Desktop app"
    clients allow. Read-only unless `write` is set.
    """
    flow = InstalledAppFlow.from_client_config(
        load_client().as_config(), scopes=scopes_for(services, write=write)
    )
    credentials = flow.run_local_server(
        host="localhost",
        port=0,
        open_browser=open_browser,
        timeout_seconds=CONSENT_TIMEOUT_SECONDS,
        **AUTHORIZATION_PARAMS,
    )
    tokens.save(credentials, account=_account_email(credentials))
    return credentials


def load_credentials() -> Credentials | None:
    """The stored credentials, refreshed if close to expiry. None if not connected."""
    info = tokens.load()
    if info is None:
        return None

    client = load_client()
    credentials = Credentials.from_authorized_user_info(
        {**info, "client_id": client.client_id, "client_secret": client.client_secret}
    )
    if _expires_soon(credentials):
        try:
            credentials.refresh(Request())
        except RefreshError as e:
            raise ReconnectRequiredError(
                "Google rejected the stored sign-in. Connect the account again."
            ) from e
        tokens.save(credentials, account=info.get("account"))
    return credentials


def disconnect() -> None:
    """Revokes the grant at Google, best effort, and forgets the stored tokens."""
    info = tokens.load()
    token = (info or {}).get("refresh_token") or (info or {}).get("token")
    if token:
        # If Google can't be reached, deleting the local tokens still disconnects
        # Orbit; the grant can be removed by hand at myaccount.google.com/permissions.
        with contextlib.suppress(requests.RequestException):
            requests.post(REVOKE_URL, params={"token": token}, timeout=10)
    tokens.delete()


def _expires_soon(credentials: Credentials) -> bool:
    if credentials.expiry is None:
        return True
    # google-auth stores expiry as a naive UTC datetime.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return credentials.expiry - now < REFRESH_MARGIN


def _account_email(credentials: Credentials) -> str | None:
    try:
        response = requests.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {credentials.token}"},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException:
        return None
    return response.json().get("email")
