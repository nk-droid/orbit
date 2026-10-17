"""What Orbit is connected to, for the app's status displays."""

from dataclasses import asdict

from fastapi import APIRouter
from pydantic import BaseModel

from integrations.google.scopes import SERVICES, AccessLevel
from integrations.google.status import get_status

router = APIRouter()


class ServiceState(BaseModel):
    label: str
    access: AccessLevel
    # None when not probed; otherwise whether the API answered, and why not.
    working: bool | None = None
    problem: str | None = None


class GoogleState(BaseModel):
    client_configured: bool
    account: str | None = None
    problem: str | None = None
    services: dict[str, ServiceState]


# A plain `def`, so FastAPI runs it in a worker thread: probing makes blocking
# calls to Google.
@router.get("/integrations/google", response_model=GoogleState)
def google_state(probe: bool = False) -> GoogleState:
    """The Google connection. With `probe`, also calls each connected API once."""
    status = get_status(probe=probe)
    return GoogleState(
        client_configured=status.client_configured,
        account=status.account,
        problem=status.problem,
        services={
            name: ServiceState(label=SERVICES[name].label, **asdict(state))
            for name, state in status.services.items()
        },
    )
