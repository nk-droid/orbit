"""Google sign-in for Gmail, Calendar, Chat, Drive and Sheets.

The configuration follows orbit2: one OAuth client shared by every Google service,
a browser sign-in on a loopback port with offline access, read-only scopes when a
service is first connected, and write scopes added later only when needed.
"""

from .auth import ReconnectRequiredError, connect, disconnect, load_credentials
from .client import ClientNotConfiguredError
from .scopes import SERVICES, access_level

__all__ = [
    "SERVICES",
    "ClientNotConfiguredError",
    "ReconnectRequiredError",
    "access_level",
    "connect",
    "disconnect",
    "load_credentials",
]
