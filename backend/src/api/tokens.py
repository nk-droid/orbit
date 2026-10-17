"""LiveKit's standard token endpoint, so any LiveKit client SDK can use it.

https://docs.livekit.io/frontends/build/authentication/endpoint/
"""

import secrets

from fastapi import APIRouter, HTTPException
from google.protobuf.json_format import ParseDict, ParseError
from livekit import api
from pydantic import BaseModel

from .settings import AGENT_NAME, get_settings

router = APIRouter()


class TokenRequest(BaseModel):
    room_name: str | None = None
    participant_identity: str | None = None
    participant_name: str | None = None
    participant_metadata: str | None = None
    participant_attributes: dict[str, str] | None = None
    room_config: dict | None = None


class TokenResponse(BaseModel):
    server_url: str
    participant_token: str


def _room_config(raw: dict) -> api.RoomConfiguration:
    # AccessToken.with_room_config needs the protobuf message; the client sends JSON.
    try:
        config = ParseDict(raw, api.RoomConfiguration())
    except ParseError as e:
        raise HTTPException(status_code=400, detail="Invalid room_config") from e
    if any(agent.agent_name != AGENT_NAME for agent in config.agents):
        raise HTTPException(status_code=400, detail="Unknown agent")
    return config


# TODO: no authentication yet. Anyone who can reach this endpoint can start an
# agent session, so keep it bound to localhost until a login is in front of it.
@router.post("/token", status_code=201, response_model=TokenResponse)
async def create_token(request: TokenRequest) -> TokenResponse:
    settings = get_settings()
    room_name = request.room_name or f"orbit-{secrets.token_hex(6)}"
    identity = request.participant_identity or f"user-{secrets.token_hex(4)}"

    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(request.participant_name or "User")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
            )
        )
    )
    if request.participant_metadata:
        token = token.with_metadata(request.participant_metadata)
    if request.participant_attributes:
        token = token.with_attributes(request.participant_attributes)
    if request.room_config:
        token = token.with_room_config(_room_config(request.room_config))

    return TokenResponse(
        server_url=settings.livekit_url,
        participant_token=token.to_jwt(),
    )
