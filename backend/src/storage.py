"""Orbit's local database: one SQLite file shared by the agent and the API."""

import os
import sqlite3
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    """backend/.data, which git and Docker ignore."""
    return Path(os.getenv("ORBIT_DATA_DIR") or _BACKEND_ROOT / ".data")


def connect(schema: str) -> sqlite3.Connection:
    """Opens the database, creating the caller's table if it is not there yet."""
    directory = data_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "orbit.db", timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute(schema)
    return connection
