import json
import stat
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from integrations.google import tokens
from integrations.google.auth import AUTHORIZATION_PARAMS, _expires_soon
from integrations.google.client import (
    ClientNotConfiguredError,
    OAuthClient,
    load_client,
    parse_client_json,
)
from integrations.google.scopes import (
    IDENTITY_SCOPES,
    SERVICES,
    access_level,
    scopes_for,
)
from integrations.google.status import get_status

AUTH = "https://www.googleapis.com/auth/"


@pytest.fixture(autouse=True)
def isolated_secrets(tmp_path, monkeypatch):
    """Every test gets an empty secrets directory and no credentials in the environment."""
    monkeypatch.setenv("ORBIT_SECRETS_DIR", str(tmp_path))
    for name in (
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_CLIENT_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def write_client_file(directory, section="installed"):
    path = directory / "google-oauth-client.json"
    path.write_text(
        json.dumps({section: {"client_id": "file-id", "client_secret": "file-secret"}})
    )


def test_services_connect_read_only_by_default():
    scopes = scopes_for(SERVICES)

    assert set(IDENTITY_SCOPES) <= set(scopes)
    assert AUTH + "gmail.readonly" in scopes
    assert AUTH + "drive.readonly" in scopes
    for service in SERVICES.values():
        assert not set(service.write) & set(scopes)


def test_each_scope_is_asked_for_once():
    for write in (False, True):
        scopes = scopes_for(SERVICES, write=write)

        assert len(scopes) == len(set(scopes))
    # Sheets are read with Drive's own read scope, and changed with their own.
    assert AUTH + "drive.readonly" in scopes
    assert AUTH + "spreadsheets" in scopes


def test_write_access_adds_only_the_requested_services_write_scopes():
    scopes = scopes_for(["gmail"], write=True)

    assert {AUTH + "gmail.compose", AUTH + "gmail.modify"} <= set(scopes)
    assert AUTH + "calendar.events" not in scopes


@pytest.mark.parametrize(
    ("service", "granted", "expected"),
    [
        ("gmail", [], "none"),
        ("gmail", [AUTH + "gmail.readonly"], "read"),
        # Both write scopes are needed: compose alone cannot archive or label.
        ("gmail", [AUTH + "gmail.readonly", AUTH + "gmail.compose"], "read"),
        (
            "gmail",
            [AUTH + "gmail.readonly", AUTH + "gmail.compose", AUTH + "gmail.modify"],
            "write",
        ),
        ("gchat", [AUTH + "chat.spaces.readonly"], "none"),
        ("gdrive", [AUTH + "drive.readonly"], "read"),
        ("gdrive", [AUTH + "drive.readonly", AUTH + "drive.file"], "write"),
    ],
)
def test_access_level(service, granted, expected):
    assert access_level(service, granted) == expected


@pytest.mark.parametrize("section", ["installed", "web"])
def test_client_json_accepts_desktop_and_web_clients(section):
    raw = {section: {"client_id": "id", "client_secret": "secret"}}

    assert parse_client_json(raw) == OAuthClient("id", "secret")


@pytest.mark.parametrize("raw", [None, {}, {"installed": {"client_id": "id"}}, []])
def test_client_json_rejects_anything_else(raw):
    with pytest.raises(ClientNotConfiguredError):
        parse_client_json(raw)


def test_client_comes_from_the_file(isolated_secrets):
    write_client_file(isolated_secrets)

    assert load_client() == OAuthClient("file-id", "file-secret")


def test_environment_wins_over_the_file(isolated_secrets, monkeypatch):
    write_client_file(isolated_secrets)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "env-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-secret")

    assert load_client() == OAuthClient("env-id", "env-secret")


def test_missing_client_says_where_to_put_it(isolated_secrets):
    with pytest.raises(ClientNotConfiguredError, match=r"google-oauth-client\.json"):
        load_client()


def test_missing_client_points_at_a_misnamed_file(isolated_secrets):
    misnamed = isolated_secrets / "google-auth-client.json"
    misnamed.write_text(
        json.dumps({"installed": {"client_id": "i", "client_secret": "s"}})
    )
    (isolated_secrets / "google-token.json").write_text(json.dumps({"token": "t"}))

    with pytest.raises(ClientNotConfiguredError, match=r"google-auth-client\.json"):
        load_client()


def test_sign_in_asks_for_offline_access_and_keeps_earlier_grants():
    flow = InstalledAppFlow.from_client_config(
        OAuthClient("id", "secret").as_config(), scopes=scopes_for(["gmail"])
    )
    flow.redirect_uri = "http://localhost:53000/"

    url, _ = flow.authorization_url(**AUTHORIZATION_PARAMS)
    query = parse_qs(urlparse(url).query)

    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["include_granted_scopes"] == ["true"]
    assert query["code_challenge_method"] == ["S256"]
    assert "client_secret" not in query


def test_tokens_are_owner_only_and_omit_the_client_secret(isolated_secrets):
    credentials = Credentials(
        token="access",
        refresh_token="refresh",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="id",
        client_secret="secret",
        scopes=[AUTH + "gmail.readonly"],
    )

    tokens.save(credentials, account="me@example.com")

    path = tokens.token_file()
    stored = tokens.load()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "secret" not in path.read_text()
    assert stored["refresh_token"] == "refresh"
    assert stored["account"] == "me@example.com"
    assert stored["scopes"] == [AUTH + "gmail.readonly"]


def test_token_is_refreshed_two_minutes_before_it_expires():
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def expiring_in(delta):
        return Credentials(token="t", expiry=now + delta)

    assert _expires_soon(expiring_in(timedelta(seconds=90)))
    assert not _expires_soon(expiring_in(timedelta(minutes=10)))
    assert _expires_soon(Credentials(token="t"))


def test_status_without_a_client_explains_what_is_missing():
    status = get_status()

    assert not status.client_configured
    assert "google-oauth-client.json" in status.problem


def test_status_with_a_client_but_no_sign_in(isolated_secrets):
    write_client_file(isolated_secrets)

    status = get_status()

    assert status.client_configured
    assert status.account is None
    assert {s.access for s in status.services.values()} == {"none"}


def test_status_reports_access_per_service_from_stored_scopes(isolated_secrets):
    write_client_file(isolated_secrets)
    tokens.token_file().write_text(
        json.dumps(
            {
                "refresh_token": "refresh",
                "account": "me@example.com",
                "scopes": [
                    AUTH + "gmail.readonly",
                    AUTH + "calendar.readonly",
                    AUTH + "calendar.events",
                ],
            }
        )
    )

    status = get_status()

    assert status.account == "me@example.com"
    assert status.services["gmail"].access == "read"
    assert status.services["gcal"].access == "write"
    assert status.services["gdrive"].access == "none"
