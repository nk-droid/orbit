"""The Google OAuth client Orbit signs in with.

Takes the same inputs as orbit2: the client JSON downloaded from Google Cloud Console
("Desktop app" type, nested under `installed`; a `web` client is accepted in case that
type was downloaded instead), or a client ID and secret from the environment, which
win when both are set.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

ENV_CLIENT_ID = "GOOGLE_OAUTH_CLIENT_ID"
ENV_CLIENT_SECRET = "GOOGLE_OAUTH_CLIENT_SECRET"
ENV_CLIENT_FILE = "GOOGLE_OAUTH_CLIENT_FILE"
ENV_SECRETS_DIR = "ORBIT_SECRETS_DIR"

CLIENT_FILE_NAME = "google-oauth-client.json"

_BACKEND_ROOT = Path(__file__).resolve().parents[3]


class ClientNotConfiguredError(Exception):
    """No usable Google OAuth client was found."""


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str

    def as_config(self) -> dict:
        """The shape google-auth-oauthlib expects for an installed-app flow."""
        return {
            "installed": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }


def secrets_dir() -> Path:
    """Where credentials and tokens live: backend/.secrets, which git and Docker ignore."""
    return Path(os.getenv(ENV_SECRETS_DIR) or _BACKEND_ROOT / ".secrets")


def client_file() -> Path:
    return Path(os.getenv(ENV_CLIENT_FILE) or secrets_dir() / CLIENT_FILE_NAME)


def parse_client_json(raw: object) -> OAuthClient:
    """Reads the client ID and secret out of Google's downloaded client JSON."""
    section = None
    if isinstance(raw, dict):
        section = raw.get("installed") or raw.get("web")
    client_id = section.get("client_id") if isinstance(section, dict) else None
    client_secret = section.get("client_secret") if isinstance(section, dict) else None
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise ClientNotConfiguredError(
            'Not a Google OAuth client file: expected an "installed" or "web" section '
            "with client_id and client_secret."
        )
    return OAuthClient(client_id, client_secret)


def _misnamed_client_file(directory: Path) -> Path | None:
    """A client file saved under another name, the usual reason the expected one is missing."""
    for candidate in sorted(directory.glob("*.json")):
        try:
            parse_client_json(json.loads(candidate.read_text()))
        except (ClientNotConfiguredError, json.JSONDecodeError, OSError):
            continue
        return candidate
    return None


def load_client() -> OAuthClient:
    client_id = os.getenv(ENV_CLIENT_ID)
    client_secret = os.getenv(ENV_CLIENT_SECRET)
    if client_id and client_secret:
        return OAuthClient(client_id, client_secret)

    path = client_file()
    if not path.exists():
        message = (
            f"No Google OAuth client found. Save the client JSON from Google Cloud "
            f"Console as {path}, or set {ENV_CLIENT_ID} and {ENV_CLIENT_SECRET}."
        )
        misnamed = _misnamed_client_file(path.parent)
        if misnamed:
            message += (
                f" {misnamed.name} in that folder is a client file: "
                f"rename it to {path.name}."
            )
        raise ClientNotConfiguredError(message)
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ClientNotConfiguredError(f"{path} is not valid JSON.") from e
    return parse_client_json(raw)
