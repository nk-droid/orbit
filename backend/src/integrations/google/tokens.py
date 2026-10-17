"""Stored Google tokens: one local file, readable only by its owner, outside git.

The file never holds the OAuth client's own ID and secret; those are re-read from the
client configuration each time, so replacing the client does not leave a stale copy.
"""

import json
import os
from pathlib import Path

from google.oauth2.credentials import Credentials

from .client import secrets_dir

TOKEN_FILE_NAME = "google-token.json"


def token_file() -> Path:
    return secrets_dir() / TOKEN_FILE_NAME


def save(credentials: Credentials, *, account: str | None) -> None:
    info = json.loads(credentials.to_json(strip=["client_id", "client_secret"]))
    # Record what Google actually granted, which can be more than was just requested
    # when earlier grants are carried over.
    info["scopes"] = sorted(credentials.granted_scopes or credentials.scopes or [])
    info["account"] = account or ""

    path = token_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Create with owner-only permissions from the start, so the token is never
    # briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(info, f, indent=2)
    path.chmod(0o600)


def load() -> dict | None:
    path = token_file()
    if not path.exists():
        return None
    return json.loads(path.read_text())


def delete() -> None:
    token_file().unlink(missing_ok=True)
