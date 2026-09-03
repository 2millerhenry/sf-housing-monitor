from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ListingCandidate:
    platform: str
    source_id: str
    title: str
    original_url: str
    price: int | None = None
    neighborhood: str | None = None
    listing_type: str | None = None
    summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    housing_kind: str = "unknown"
    unit_type: str | None = None
    building_units: int | None = None


@dataclass(slots=True)
class ScoreResult:
    score: int
    reasons: list[str]
    concern: str
    details: dict[str, Any]
    confidence: int = 0
    eligibility: str = "eligible"
    unknowns: list[str] = field(default_factory=list)
    eligibility_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ScanOutcome:
    run_id: int
    status: str
    listings_seen: int = 0
    listings_added: int = 0
    listings_updated: int = 0
    sources_failed: int = 0
    already_running: bool = False
