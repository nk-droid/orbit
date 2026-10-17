"""Authenticated calls to Google's REST APIs."""

import asyncio
from typing import Any

import aiohttp

from .auth import load_credentials

_TIMEOUT = aiohttp.ClientTimeout(total=15)
# A file's contents take longer to arrive than an answer about it.
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)

Params = dict[str, Any] | list[tuple[str, Any]] | None


class NotConnectedError(Exception):
    """No Google account has been connected."""


class GoogleApiError(Exception):
    """Google answered with an error status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message

    @property
    def insufficient_scope(self) -> bool:
        """The stored grant does not cover this call, typically read-only access."""
        return self.status == 403 and "insufficient" in self.message.lower()

    @property
    def export_too_large(self) -> bool:
        """Google will not convert the file because the result would be too big."""
        return self.status == 403 and "too large to be exported" in self.message

    @property
    def api_not_enabled(self) -> bool:
        """The API was never switched on in the Google Cloud project."""
        return self.status == 403 and (
            "has not been used" in self.message or "is disabled" in self.message
        )


class TooLargeError(Exception):
    """A download turned out bigger than the caller is prepared to hold."""


async def _headers() -> dict[str, str]:
    # Loading may refresh the token over the network, which blocks.
    credentials = await asyncio.to_thread(load_credentials)
    if credentials is None:
        raise NotConnectedError
    return {"Authorization": f"Bearer {credentials.token}"}


async def _error(response: aiohttp.ClientResponse) -> GoogleApiError:
    try:
        message = (await response.json())["error"]["message"]
    except (aiohttp.ContentTypeError, ValueError, KeyError, TypeError):
        message = (await response.text())[:200]
    return GoogleApiError(response.status, message)


async def get_json(
    session: aiohttp.ClientSession,
    url: str,
    params: Params = None,
    *,
    timeout: aiohttp.ClientTimeout = _TIMEOUT,
) -> dict[str, Any]:
    headers = await _headers()
    async with session.get(
        url, params=params, headers=headers, timeout=timeout
    ) as response:
        if response.status >= 400:
            raise await _error(response)
        return await response.json()


async def get_text(
    session: aiohttp.ClientSession, url: str, params: Params = None
) -> str:
    headers = await _headers()
    async with session.get(
        url, params=params, headers=headers, timeout=_TIMEOUT
    ) as response:
        if response.status >= 400:
            raise await _error(response)
        return await response.text()


async def get_bytes(
    session: aiohttp.ClientSession, url: str, params: Params = None, *, limit: int
) -> bytes:
    """A download, such as a file's contents. Gives up once it passes `limit` bytes."""
    headers = await _headers()
    async with session.get(
        url, params=params, headers=headers, timeout=DOWNLOAD_TIMEOUT
    ) as response:
        if response.status >= 400:
            raise await _error(response)
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > limit:
                raise TooLargeError
            chunks.append(chunk)
        return b"".join(chunks)


async def send_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    body: dict[str, Any],
    params: Params = None,
) -> dict[str, Any]:
    """POST, PATCH or PUT a JSON body. Used for the calls that change something."""
    headers = await _headers()
    async with session.request(
        method, url, json=body, params=params, headers=headers, timeout=_TIMEOUT
    ) as response:
        if response.status >= 400:
            raise await _error(response)
        return await response.json()


async def send(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    data: Any,
    params: Params = None,
    *,
    timeout: aiohttp.ClientTimeout = _TIMEOUT,
) -> dict[str, Any]:
    """POST or PATCH a body that is not plain JSON, such as a file upload."""
    headers = await _headers()
    async with session.request(
        method, url, data=data, params=params, headers=headers, timeout=timeout
    ) as response:
        if response.status >= 400:
            raise await _error(response)
        return await response.json()
