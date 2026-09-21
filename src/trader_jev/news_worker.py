"""Compatibility imports for the broker-independent News Worker."""

from trader_jev.news import (
    InMemoryNewsAdapter,
    NewsFeatureEngine,
    NewsIntegrationMode,
    NewsRunResult,
    NewsState,
    NewsStateCache,
    NewsWorkerConfig,
    NewsWorkerService,
    classify_headline,
)

__all__ = [
    "InMemoryNewsAdapter",
    "NewsFeatureEngine",
    "NewsIntegrationMode",
    "NewsRunResult",
    "NewsState",
    "NewsStateCache",
    "NewsWorkerConfig",
    "NewsWorkerService",
    "classify_headline",
]
