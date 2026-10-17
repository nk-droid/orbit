import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

# The agent registers under this name in agent.py; tokens may only dispatch to it.
AGENT_NAME = "orbit"

DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


@dataclass(frozen=True)
class Settings:
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    allowed_origins: tuple[str, ...]


@lru_cache
def get_settings() -> Settings:
    load_dotenv(".env.local")
    origins = os.getenv("ORBIT_ALLOWED_ORIGINS", DEFAULT_ORIGINS)
    return Settings(
        livekit_url=os.environ["LIVEKIT_URL"],
        livekit_api_key=os.environ["LIVEKIT_API_KEY"],
        livekit_api_secret=os.environ["LIVEKIT_API_SECRET"],
        allowed_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
    )
