from __future__ import annotations

from echo_voc.errors import SourceError
from echo_voc.sources.appstore import AppStoreSource
from echo_voc.sources.base import Source
from echo_voc.sources.bilibili import BilibiliSource
from echo_voc.sources.youtube import YouTubeSource

SOURCES: dict[str, type[Source]] = {
    s.name: s for s in (AppStoreSource, YouTubeSource, BilibiliSource)
}


def resolve_source(target: str, platform: str | None = None) -> Source:
    """Pick a source by explicit platform name, or auto-detect from the URL."""
    if platform:
        try:
            return SOURCES[platform]()
        except KeyError:
            raise SourceError(
                f"Unknown platform '{platform}'. Available: {', '.join(SOURCES)}"
            ) from None
    for cls in SOURCES.values():
        if cls.matches(target):
            return cls()
    raise SourceError(
        f"Could not detect platform for '{target}'. Supported: "
        + "; ".join(f"{s.name} (e.g. {s.example})" for s in SOURCES.values())
    )


def detect_link(target: str) -> tuple[Source, str]:
    """Return (source, "account" | "work") for an account or work URL."""
    for cls in SOURCES.values():
        if cls.matches_account(target):
            return cls(), "account"
    return resolve_source(target), "work"


__all__ = ["SOURCES", "Source", "detect_link", "resolve_source"]
