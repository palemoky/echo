from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import httpx

from echo_voc.errors import SourceError
from echo_voc.models import AccountInfo, Comment, FetchResult, WorkInfo

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
}
RETRY_STATUSES = {412, 429, 500, 502, 503, 504}


class RetryableError(SourceError):
    """A throttling / transient failure worth retrying with backoff."""


class Source(ABC):
    """A platform that comments can be fetched from.

    To add a platform: subclass, set ``name``/``description``, implement ``matches`` and
    ``fetch``, then register it in ``echo_voc.sources``. Implement the optional account-level
    methods (``resolve_account``, ``resolve_work``, ``iter_works``, ``iter_comments``) to
    support ``echo-voc sync``.
    """

    name: str
    description: str
    example: str
    account_example: str | None = None
    supports_danmaku = False

    # Politeness: minimum seconds between requests (+ random jitter) and retry backoff.
    min_interval: float = 0.0
    jitter: float = 0.0
    max_retries: int = 4
    backoff_base: float = 2.0

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    # ---- single-link (realtime) API ---------------------------------------------------------

    @classmethod
    @abstractmethod
    def matches(cls, target: str) -> bool:
        """Whether this source can handle the given work URL / identifier."""

    @abstractmethod
    async def fetch(self, target: str, limit: int = 200) -> FetchResult:
        """Fetch up to ``limit`` comments for the target."""

    # ---- account-level (sync) API -----------------------------------------------------------

    @classmethod
    def matches_account(cls, target: str) -> bool:
        return False

    async def resolve_account(self, target: str) -> AccountInfo:
        raise SourceError(f"{self.name} does not support account tracking")

    async def resolve_work(self, target: str) -> tuple[AccountInfo, WorkInfo]:
        raise SourceError(f"{self.name} does not support work tracking")

    def iter_works(self, account: AccountInfo) -> AsyncIterator[WorkInfo]:
        """Yield the account's works, newest first."""
        raise SourceError(f"{self.name} does not support listing works")

    def iter_comments(self, work: WorkInfo, *, replies: bool = False) -> AsyncIterator[Comment]:
        """Yield a work's comments, newest first (the caller decides when to stop)."""
        raise SourceError(f"{self.name} does not support incremental comments")

    async def fetch_danmaku(self, work: WorkInfo) -> list[Comment]:
        raise SourceError(f"{self.name} has no danmaku")

    # ---- HTTP helpers -----------------------------------------------------------------------

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=DEFAULT_HEADERS, timeout=20, follow_redirects=True
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock:
            delay = self.min_interval + random.uniform(0, self.jitter)
            wait = self._last_request + delay - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Rate-limited request with exponential backoff on throttling / 5xx responses."""
        for attempt in range(self.max_retries + 1):
            await self._throttle()
            try:
                resp = await self.client().request(method, url, **kwargs)
            except httpx.TransportError as e:
                error: Exception = e
            else:
                if resp.status_code not in RETRY_STATUSES:
                    return resp
                error = RetryableError(f"HTTP {resp.status_code} from {url}")
            if attempt == self.max_retries:
                break
            delay = self.backoff_base * (2**attempt)
            log.info("%s: %s, retrying in %.0fs", self.name, error, delay)
            await asyncio.sleep(delay)
        raise SourceError(f"{self.name}: request failed after retries: {error}")
