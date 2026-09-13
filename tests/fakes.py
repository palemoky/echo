from __future__ import annotations

from datetime import datetime, timedelta, timezone

from echo_voc.models import AccountInfo, Comment, WorkInfo
from echo_voc.sources.base import Source

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def comment(
    i: int,
    *,
    hours_ago: float = 0,
    kind: str = "comment",
    text: str | None = None,
    likes: int = 0,
    work: str = "v1",
    now: datetime = T0,
) -> Comment:
    return Comment(
        id=f"{work}-{kind}-{i}",
        platform="fake",
        kind=kind,
        content=text or f"comment {i}",
        likes=likes,
        author=f"user{i % 7}",
        created_at=now - timedelta(hours=hours_ago),
    )


class FakeSource(Source):
    """In-memory platform: works newest first, comments newest first."""

    name = "fake"
    description = "fake"
    example = "fake://work/v1"
    supports_danmaku = True

    def __init__(self, client=None):
        super().__init__(client)
        self.works: list[WorkInfo] = []
        self.comments: dict[str, list[Comment]] = {}
        self.danmaku: dict[str, list[Comment]] = {}
        self.pulled: dict[str, int] = {}
        self.listed = 0

    @classmethod
    def matches(cls, target):
        return target.startswith("fake://work/")

    @classmethod
    def matches_account(cls, target):
        return target.startswith("fake://account/")

    async def fetch(self, target, limit=200):
        raise NotImplementedError

    async def resolve_account(self, target):
        return AccountInfo(platform="fake", external_id="acc", name="Fake Studio")

    async def resolve_work(self, target):
        wid = target.rsplit("/", 1)[1]
        return (
            AccountInfo(platform="fake", external_id="acc", name="Fake Studio"),
            next(w for w in self.works if w.external_id == wid),
        )

    async def iter_works(self, account):
        for w in self.works:
            self.listed += 1
            yield w

    async def iter_comments(self, work, *, replies=False):
        for c in sorted(
            self.comments.get(work.external_id, []), key=lambda c: c.created_at, reverse=True
        ):
            self.pulled[work.external_id] = self.pulled.get(work.external_id, 0) + 1
            yield c

    async def fetch_danmaku(self, work):
        return list(self.danmaku.get(work.external_id, []))


def make_work(wid: str, days_ago: int = 0) -> WorkInfo:
    return WorkInfo(
        platform="fake",
        external_id=wid,
        title=f"Work {wid}",
        published_at=T0 - timedelta(days=days_ago),
    )


class FakeEmbedder:
    """Deterministic character-bigram hashing embedder: similar texts → similar vectors."""

    name = "fake"
    model = "fake-embed"
    dim = 1024

    async def embed(self, texts):
        import hashlib
        import math

        out = []
        for text in texts:
            v = [0.0] * self.dim
            chars = [c for c in text.lower() if not c.isspace()]
            for a, b in zip(chars, chars[1:] + [" "], strict=True):
                v[int(hashlib.md5((a + b).encode()).hexdigest(), 16) % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out

    async def aclose(self):
        return None
