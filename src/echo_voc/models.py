from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class Comment(BaseModel):
    """A single user comment / review, normalized across platforms."""

    id: str
    platform: str
    content: str
    author: str | None = None
    title: str | None = None
    rating: float | None = None
    likes: int | None = None
    created_at: datetime | None = None
    version: str | None = None
    url: str | None = None


class FetchResult(BaseModel):
    platform: str
    target: str
    title: str | None = None
    comments: list[Comment]
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Sentiment(str, Enum):
    positive = "positive"
    neutral = "neutral"
    negative = "negative"


class ThemeCategory(str, Enum):
    pain_point = "pain_point"
    feature_request = "feature_request"
    praise = "praise"
    question = "question"


class Evidence(BaseModel):
    comment_id: str
    content: str
    likes: int | None = None
    rating: float | None = None


class Theme(BaseModel):
    category: ThemeCategory
    title: str
    description: str
    mentions: int
    severity: int | None = Field(default=None, ge=1, le=5)
    evidence: list[Evidence] = Field(default_factory=list)


class SentimentStats(BaseModel):
    positive: int = 0
    neutral: int = 0
    negative: int = 0

    @property
    def total(self) -> int:
        return self.positive + self.neutral + self.negative


class Report(BaseModel):
    platform: str
    target: str
    title: str | None = None
    model: str
    total_comments: int
    sentiment: SentimentStats
    average_rating: float | None = None
    summary: str
    themes: list[Theme]
    suggestions: list[str]
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
