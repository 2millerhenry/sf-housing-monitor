"""A single, derived truth about whether a listing source is still current.

This module intentionally stores nothing.  A source's durable scan history and
connector state already exist in SQLite; the watchdog only turns those facts
into a clear user-facing state and a safe retry decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .connectors import ConnectorStatus

if TYPE_CHECKING:
    from .database import Repository
    from .sources import ListingSource


# The scanner normally runs at 10:00 and 18:00 Pacific.  Twenty-six hours
# leaves room for sleep, a missed slot, and normal network variance, while
# still surfacing a truly stale source within one day.
FRESHNESS_WINDOW = timedelta(hours=26)
BACKOFF_AFTER_FAILURES = 2
BACKOFF_BASE = timedelta(hours=6)
BACKOFF_MAX = timedelta(hours=24)


def source_key(source: "ListingSource") -> str:
    base = str(getattr(source, "source_key", source.__class__.__name__))
    # Display labels are not identities.  Gmail alerts and an optional Apify
    # fallback can both call themselves Facebook Marketplace, but must never
    # be allowed to overwrite each other's source history.
    return f"{base}::{source_provider(source)}"


def source_provider(source: "ListingSource") -> str:
    return str(getattr(source, "last_provider", getattr(source, "provider", source.__class__.__name__)))


def source_connector_state_key(source: "ListingSource") -> str | None:
    value = getattr(source, "connector_state_key", None)
    if value:
        return str(value)
    value = getattr(source, "connector_key", None)
    return str(value) if value else None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _format_time(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%b %-d at %-I:%M %p UTC") if value else "an unknown time"


@dataclass(frozen=True, slots=True)
class SourceFreshness:
    """Read-only source health, derived from the app's existing evidence."""

    key: str
    platform: str
    provider: str
    status: str
    label: str
    explanation: str
    action: str
    latest_run: dict[str, Any] | None
    last_success_at: str | None
    failure_streak: int = 0
    next_retry_at: str | None = None

    @property
    def needs_attention(self) -> bool:
        return self.status in {"attention", "stale", "backoff"}

    @property
    def is_current(self) -> bool:
        return self.status in {"working", "working_zero", "waiting_first_alert"}


def _backoff_until(last_error: datetime | None, failure_streak: int) -> datetime | None:
    if last_error is None or failure_streak < BACKOFF_AFTER_FAILURES:
        return None
    multiplier = 2 ** min(failure_streak - BACKOFF_AFTER_FAILURES, 2)
    return last_error + min(BACKOFF_BASE * multiplier, BACKOFF_MAX)


def _optional_result(
    source: "ListingSource", key: str, provider: str, connector: ConnectorStatus | None
) -> SourceFreshness | None:
    connector_key = getattr(source, "connector_key", None)
    mode = str(getattr(source, "mode", "setup"))
    platform = str(getattr(source, "platform", "Source"))
    if not connector_key:
        if mode == "automatic":
            return None
        return SourceFreshness(
            key,
            platform,
            provider,
            "manual",
            f"{platform} needs setup",
            getattr(source, "manual_reason", None)
            or f"{platform} is a manual source and is not part of automatic checks.",
            "Open this source directly when you want its additional coverage.",
            None,
            None,
        )
    if mode == "automatic":
        return None
    # Connector setup itself has one aggregate Doctor check.  Individual
    # providers become visible once the connector is actually able to scan;
    # otherwise a fresh install would show six copies of the same Gmail action.
    return SourceFreshness(
        key,
        platform,
        provider,
        "optional",
        f"{platform} is optional",
        getattr(source, "manual_reason", None)
        or f"{platform} is not configured and does not block your public-source shortlist.",
        "Open Sources only if you want this additional coverage.",
        None,
        None,
    )


def evaluate_source_freshness(
    repository: "Repository", source: "ListingSource", *, now: datetime | None = None
) -> SourceFreshness:
    """Derive a truthful source state without doing network work or mutating data."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    key = source_key(source)
    platform = str(getattr(source, "platform", "Source"))
    provider = source_provider(source)
    connector_key = source_connector_state_key(source)
    connector = repository.connector_state(connector_key) if connector_key else None
    optional = _optional_result(source, key, provider, connector)
    if optional is not None:
        return optional

    history = repository.source_run_history(source_key=key, platform=platform)
    latest = history[0] if history else None
    terminal = [run for run in history if str(run.get("status") or "") in {"success", "error"}]
    last_success = next((run for run in terminal if run.get("status") == "success"), None)
    last_success_at = str(last_success.get("finished_at") or "") or None if last_success else None

    failure_streak = 0
    last_error_at: datetime | None = None
    for run in terminal:
        if run.get("status") != "error":
            break
        failure_streak += 1
        if last_error_at is None:
            last_error_at = _parse_time(run.get("finished_at") or run.get("started_at"))
    retry_at = _backoff_until(last_error_at, failure_streak)
    latest_status = str(latest.get("status") or "") if latest else ""
    success_time = _parse_time(last_success_at)

    # A newly inserted source-run is intentionally visible before network work
    # begins.  It must not erase the persisted repeated-failure evidence used
    # to decide whether this automatic attempt should be deferred.
    if latest_status == "running" and not failure_streak:
        return SourceFreshness(
            key,
            platform,
            provider,
            "checking",
            f"{platform} is checking",
            "This source is part of the active scan. Its last known listing results remain available.",
            "Wait for the current check to finish.",
            latest,
            last_success_at,
            failure_streak,
            retry_at.isoformat() if retry_at else None,
        )

    if failure_streak:
        message = str((latest or {}).get("message") or "The latest source request did not complete.")
        last_good = (
            f" The last good result is preserved from {_format_time(success_time)}."
            if success_time
            else " No successful result has been recorded yet."
        )
        if retry_at and current < retry_at:
            return SourceFreshness(
                key,
                platform,
                provider,
                "backoff",
                f"{platform} is paused briefly",
                f"{failure_streak} consecutive checks failed. {message}{last_good}",
                f"Automatic retry resumes after {_format_time(retry_at)}. You can use Check for new homes once now if you want an earlier retry.",
                latest,
                last_success_at,
                failure_streak,
                retry_at.isoformat(),
            )
        return SourceFreshness(
            key,
            platform,
            provider,
            "stale" if not success_time or current - success_time > FRESHNESS_WINDOW else "attention",
            f"{platform} needs attention",
            f"The latest check failed. {message}{last_good}",
            "Use Check for new homes once. The next scheduled check will also retry this source without blocking the others.",
            latest,
            last_success_at,
            failure_streak,
            retry_at.isoformat() if retry_at else None,
        )

    if success_time:
        age = current - success_time
        seen = int((latest or last_success).get("listings_seen") or 0)
        if age > FRESHNESS_WINDOW:
            return SourceFreshness(
                key,
                platform,
                provider,
                "stale",
                f"{platform} is stale",
                f"Its last successful result was {_format_time(success_time)}, more than {int(FRESHNESS_WINDOW.total_seconds() // 3600)} hours ago. Existing listings remain available but may be old.",
                "Use Check for new homes now. If it stays stale, open Sources for the direct source link and run the Ready Check again.",
                latest,
                last_success_at,
            )
        if seen == 0:
            return SourceFreshness(
                key,
                platform,
                provider,
                "working_zero",
                f"{platform} is working, no matches",
                f"The latest source check completed at {_format_time(success_time)} and found no matching listings. This is a valid result, not a failure.",
                "Nothing to do. The next scheduled check will look again.",
                latest,
                last_success_at,
            )
        return SourceFreshness(
            key,
            platform,
            provider,
            "working",
            f"{platform} is current",
            f"The latest source check completed at {_format_time(success_time)} and checked {seen} listing(s).",
            "Nothing to do.",
            latest,
            last_success_at,
        )

    return SourceFreshness(
        key,
        platform,
        provider,
        "not_run",
        f"{platform} has not run yet",
        "No source result has been recorded yet.",
        "Use Check for new homes to run the first bounded check.",
        latest,
        None,
    )


def source_is_in_backoff(repository: "Repository", source: "ListingSource", *, now: datetime | None = None) -> SourceFreshness | None:
    """Return an active automatic backoff, if any. Manual checks intentionally bypass it."""
    health = evaluate_source_freshness(repository, source, now=now)
    return health if health.status == "backoff" else None
