"""Google OAuth scopes per service.

Same policy as orbit2's connectors: a service is first connected with its read-only
scopes, and its write scopes are requested later through incremental authorization,
only once a write action is actually needed.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

_AUTH = "https://www.googleapis.com/auth/"

AccessLevel = Literal["none", "read", "write"]


@dataclass(frozen=True)
class Service:
    label: str
    read: tuple[str, ...]
    write: tuple[str, ...] = ()


SERVICES: dict[str, Service] = {
    "gmail": Service(
        label="Gmail",
        read=(_AUTH + "gmail.readonly",),
        # compose covers drafts and sending; modify covers labels, archive and trash.
        # Together they avoid the much broader mail.google.com scope.
        write=(_AUTH + "gmail.compose", _AUTH + "gmail.modify"),
    ),
    "gcal": Service(
        label="Google Calendar",
        read=(_AUTH + "calendar.readonly",),
        # Create, update and respond to events, without calendar-list management.
        write=(_AUTH + "calendar.events",),
    ),
    "gchat": Service(
        label="Google Chat",
        read=(_AUTH + "chat.spaces.readonly", _AUTH + "chat.messages.readonly"),
        # Create-only: Orbit posts messages, it never edits or deletes them.
        write=(_AUTH + "chat.messages.create",),
    ),
    # Not in orbit2. Writing is limited to the files Orbit itself creates: drive.file
    # gives no access to change the user's other files.
    "gdrive": Service(
        label="Google Drive",
        read=(_AUTH + "drive.readonly",),
        write=(_AUTH + "drive.file",),
    ),
    # Not in orbit2. Sheets are read through the Drive read grant, which the Sheets
    # API accepts. Changing one needs a scope of its own, and unlike Drive's it
    # reaches every spreadsheet the user has: changing the sheets they keep is the
    # point. Each change is still drafted and agreed first (actions/gate.py).
    "gsheets": Service(
        label="Google Sheets",
        read=(_AUTH + "drive.readonly",),
        write=(_AUTH + "spreadsheets",),
    ),
}

# Always requested, so Orbit knows which account it is connected to.
IDENTITY_SCOPES = (_AUTH + "userinfo.email", _AUTH + "userinfo.profile")


def scopes_for(services: Iterable[str], *, write: bool = False) -> list[str]:
    """The scopes to request for these services, read-only unless `write` is set.

    Each scope is asked for once, though Drive and Sheets read with the same one.
    """
    scopes = list(IDENTITY_SCOPES)
    for name in services:
        service = SERVICES[name]
        scopes.extend(service.read)
        if write:
            scopes.extend(service.write)
    return list(dict.fromkeys(scopes))


def access_level(service_name: str, granted: Iterable[str]) -> AccessLevel:
    """What the granted scopes allow for one service."""
    service = SERVICES[service_name]
    have = set(granted)
    if not have.issuperset(service.read):
        return "none"
    if service.write and have.issuperset(service.write):
        return "write"
    return "read"
