"""Command line for the Google connection. Run from backend/:

uv run python -m integrations.google connect            # all five services, read-only
uv run python -m integrations.google connect gmail gcal  # only these
uv run python -m integrations.google connect --write     # also request write access
uv run python -m integrations.google status
uv run python -m integrations.google disconnect
"""

import argparse
import sys

from dotenv import load_dotenv
from google_auth_oauthlib.flow import WSGITimeoutError
from oauthlib.oauth2 import OAuth2Error

from .auth import connect, disconnect
from .client import ClientNotConfiguredError, load_client
from .scopes import SERVICES
from .status import get_status


def _print_status() -> int:
    status = get_status(probe=True)
    if not status.client_configured:
        print(status.problem)
        return 1
    if status.account is None and all(
        s.access == "none" for s in status.services.values()
    ):
        print("Not connected. Run: uv run python -m integrations.google connect")
        return 1

    print(f"Account: {status.account or 'unknown'}")
    if status.problem:
        print(status.problem)
    for name, service in status.services.items():
        label = SERVICES[name].label
        if service.access == "none":
            print(f"  {label:<16} not connected")
        elif service.working:
            print(f"  {label:<16} {service.access:<6} working")
        else:
            print(f"  {label:<16} {service.access:<6} NOT WORKING  {service.problem}")
    return 0 if all(s.working is not False for s in status.services.values()) else 1


def main() -> int:
    load_dotenv(".env.local")
    parser = argparse.ArgumentParser(prog="python -m integrations.google")
    commands = parser.add_subparsers(dest="command", required=True)

    connect_parser = commands.add_parser("connect", help="sign in with Google")
    connect_parser.add_argument(
        "services",
        nargs="*",
        help=f"services to connect: {', '.join(SERVICES)} (default: all)",
    )
    connect_parser.add_argument(
        "--write",
        action="store_true",
        help="also request write access (send mail, create events, change sheets)",
    )
    commands.add_parser("status", help="show what is connected and check each API")
    commands.add_parser("disconnect", help="revoke access and forget the tokens")

    args = parser.parse_args()
    try:
        if args.command == "connect":
            services = args.services or list(SERVICES)
            unknown = [name for name in services if name not in SERVICES]
            if unknown:
                parser.error(f"unknown service: {', '.join(unknown)}")
            # Fail on a missing client before claiming to open the browser.
            load_client()
            print("Opening Google sign-in in your browser…")
            connect(services, write=args.write)
            return _print_status()
        if args.command == "status":
            return _print_status()
        disconnect()
        print("Disconnected from Google.")
        return 0
    except ClientNotConfiguredError as e:
        print(e)
        return 1
    except WSGITimeoutError:
        print("The sign-in was not finished in time. Run connect again.")
        return 1
    except OAuth2Error as e:
        # Raised when consent is declined or Google rejects the client.
        print(f"Google sign-in failed: {e.description or e.error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
