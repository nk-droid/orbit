"""What is connected, and whether each Google API actually answers."""

from dataclasses import dataclass, field

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from requests import RequestException

from . import tokens
from .auth import ReconnectRequiredError, load_credentials
from .client import ClientNotConfiguredError, load_client
from .scopes import SERVICES, AccessLevel, access_level

# One cheap read per service, enough to prove the API is enabled and the scope works.
_PROBES = {
    "gmail": "https://gmail.googleapis.com/gmail/v1/users/me/profile",
    "gcal": "https://www.googleapis.com/calendar/v3/users/me/calendarList?maxResults=1",
    "gchat": "https://chat.googleapis.com/v1/spaces?pageSize=1",
    "gdrive": "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)",
    # Sheets has nothing to list. Asking for a spreadsheet that does not exist is
    # answered "not found" once the API is enabled and the grant covers it.
    "gsheets": "https://sheets.googleapis.com/v4/spreadsheets/orbit-probe?fields=spreadsheetId",
}
# Answers that prove the API works although the call itself was refused.
_ANSWERED = {"gsheets": (400, 404)}


@dataclass
class ServiceStatus:
    access: AccessLevel
    # None when not probed; otherwise whether the API answered, and why not.
    working: bool | None = None
    problem: str | None = None


@dataclass
class GoogleStatus:
    client_configured: bool
    account: str | None = None
    problem: str | None = None
    services: dict[str, ServiceStatus] = field(default_factory=dict)


def get_status(*, probe: bool = False) -> GoogleStatus:
    """The connection state. With `probe`, also calls each connected API once."""
    try:
        load_client()
    except ClientNotConfiguredError as e:
        return GoogleStatus(client_configured=False, problem=str(e))

    info = tokens.load()
    if info is None:
        return GoogleStatus(client_configured=True, services=_services(()))

    status = GoogleStatus(
        client_configured=True,
        account=info.get("account"),
        services=_services(info.get("scopes", ())),
    )
    if not probe:
        return status

    try:
        credentials = load_credentials()
    except ReconnectRequiredError as e:
        status.problem = str(e)
        return status
    if credentials is not None:
        for name, service in status.services.items():
            if service.access != "none":
                service.working, service.problem = _probe(name, credentials)
    return status


def _services(granted) -> dict[str, ServiceStatus]:
    return {
        name: ServiceStatus(access=access_level(name, granted)) for name in SERVICES
    }


def _probe(name: str, credentials: Credentials) -> tuple[bool, str | None]:
    try:
        response = AuthorizedSession(credentials).get(_PROBES[name], timeout=10)
    except RequestException as e:
        return False, f"Could not reach Google: {e}"
    if response.ok or response.status_code in _ANSWERED.get(name, ()):
        return True, None
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = response.text[:200]
    return False, f"{response.status_code}: {message}"
