from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from echo_voc.models import FetchResult

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
}


class Source(ABC):
    """A platform that comments can be fetched from.

    To add a platform: subclass, set ``name``/``description``, implement
    ``matches`` and ``fetch``, then register it in ``echo_voc.sources``.
    """

    name: str
    description: str
    example: str

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    @classmethod
    @abstractmethod
    def matches(cls, target: str) -> bool:
        """Whether this source can handle the given URL / identifier."""

    @abstractmethod
    async def fetch(self, target: str, limit: int = 200) -> FetchResult:
        """Fetch up to ``limit`` comments for the target."""

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=DEFAULT_HEADERS, timeout=20, follow_redirects=True
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
