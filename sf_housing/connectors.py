from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


CONNECTOR_STATES = {
    "not_configured",
    "configured_unverified",
    "checking",
    "working",
    "working_zero",
    "waiting_first_alert",
    "degraded",
    "authorization_expired",
    "quota_blocked",
    "disabled",
}

GMAIL_PROVIDERS = (
    ("gmail:zillow", "Zillow"),
    ("gmail:hotpads", "HotPads"),
    ("gmail:apartments-com", "Apartments.com"),
    ("gmail:zumper", "Zumper"),
    ("gmail:roomies", "Roomies"),
    ("gmail:facebook-marketplace", "Facebook Marketplace"),
)


def gmail_provider_key(platform: str) -> str:
    for key, label in GMAIL_PROVIDERS:
        if platform.casefold() == label.casefold():
            return key
    normalized = platform.casefold().replace(".", "")
    slug = "-".join(part for part in normalized.replace("/", " ").split() if part)
    return f"gmail:{slug}"


@dataclass(frozen=True, slots=True)
class ConnectorStatus:
    key: str
    state: str
    configured_at: str | None = None
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    observed_items: int = 0
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def working(self) -> bool:
        return self.state in {"working", "working_zero", "waiting_first_alert"}

    @property
    def label(self) -> str:
        return {
            "not_configured": "Optional",
            "configured_unverified": "Ready to test",
            "checking": "Testing",
            "working": "Working",
            "working_zero": "Working, no matches",
            "waiting_first_alert": "Waiting for first alert",
            "degraded": "Attention",
            "authorization_expired": "Reconnect",
            "quota_blocked": "Allowance reached",
            "disabled": "Optional",
        }[self.state]


def connector_state_for_error(message: str) -> str:
    normalized = message.casefold()
    if any(term in normalized for term in ("quota", "allowance", "monthly cap", "credit")):
        return "quota_blocked"
    if any(term in normalized for term in ("authorization", "oauth", "expired", "reconnect")):
        return "authorization_expired"
    return "degraded"


def aggregate_gmail_status(
    provider_states: dict[str, ConnectorStatus],
) -> tuple[str, str, int, dict[str, str]]:
    """Summarize provider truth without erasing provider-specific recovery state."""
    states = {key: status.state for key, status in provider_states.items()}
    observed = sum(status.observed_items for status in provider_states.values())
    if not states:
        return "configured_unverified", "Gmail is authorized and ready for one bounded test.", 0, {}
    if any(state == "checking" for state in states.values()):
        return "checking", "Testing saved-search providers through Gmail.", observed, states
    if any(state == "authorization_expired" for state in states.values()):
        return "authorization_expired", "Gmail access expired or was revoked. Reconnect once.", observed, states
    failures = [state for state in states.values() if state in {"degraded", "quota_blocked"}]
    if failures:
        return (
            "degraded",
            "Gmail is connected, but one or more saved-search providers need attention.",
            observed,
            states,
        )
    if any(state == "working" for state in states.values()):
        return "working", "Gmail is importing supported saved-search alerts.", observed, states
    if any(state == "working_zero" for state in states.values()):
        return (
            "working_zero",
            "Gmail is connected; supported alerts were checked with no current matching listings.",
            observed,
            states,
        )
    if any(state == "waiting_first_alert" for state in states.values()):
        return (
            "waiting_first_alert",
            "Gmail is connected and waiting for the first supported provider alert.",
            observed,
            states,
        )
    return "configured_unverified", "Gmail is authorized and ready for one bounded test.", observed, states
