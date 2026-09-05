"""How many homes a disconnected source is holding that this app never sees.

The Alerts page used to say "Waiting for first alert" beside four providers,
which is true and completely unmotivating: it never says what the missing setup
costs, so a chore with no stated payoff does not get done.

Counting them turns out to be mostly impossible, and finding that out is the
point of this module rather than a disappointment. Of the four, exactly one can
be counted from a script:

    HotPads         200, and the count is in the page title
    Apartments.com  403
    Roomies         403
    Zillow          200 -- and this is the dangerous one

Zillow answers a blocked client with a normal-looking page whose title reads
"0 Rentals" and whose payload carries "totalResultCount": 0, while the body
contains a PerimeterX captcha. A parser that trusted it would have put
"Zillow: 0 homes you are not seeing" in front of the reader: not a missing
number, but the precise opposite of the truth, stated confidently. Everything
below exists to make that outcome impossible.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .connectors import GMAIL_PROVIDERS

LOGGER = logging.getLogger(__name__)


# A page that mentions any of these is a wall, whatever status code it carried.
# Checked case-insensitively against the raw body.
BLOCK_SIGNALS = (
    "captcha",
    "perimeterx",
    "px-captcha",
    "are you a human",
    "unusual traffic",
    "press & hold",
    "access denied",
    "request unsuccessful",
    "cf-browser-verification",
    "just a moment",
)

# Above this, a "count" is a page number, a phone number or a price that
# happened to sit next to the word "rentals". Below one, see the docstring.
MAXIMUM_CREDIBLE_COUNT = 200_000

# How long a count stays good enough to show. This is the real limit on how
# often these sites are asked -- twice a day at most, however many times
# somebody presses Check for new homes -- and it is expressed as staleness
# rather than as which trigger is running, because the constraint that matters
# is requests per day, not which button caused them.
COVERAGE_REFRESH_AFTER = timedelta(hours=11)

# Past this a stored count is not shown at all. A figure from last month is not
# a fact about today, and dating it is not enough to make it one.
COVERAGE_STALE_AFTER = timedelta(days=8)

# Only sources whose count has actually been verified against a real response.
# A provider absent from here shows its invitation with no number, which is the
# honest thing to do and is not a bug.
_TITLE_COUNT = re.compile(
    r"[-–]\s*([\d,]{1,9})\s*(?:rentals?|results?|listings?|apartments?|homes?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CoverageCount:
    """A number that was really read from a real page."""

    platform: str
    count: int
    taken_at: str


def _title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def looks_blocked(html: str) -> bool:
    """Is this a wall rather than a page of results?

    Status codes are not enough: a 200 carrying a captcha is the case that
    produced a confident zero.
    """
    body = html.casefold()
    return any(signal in body for signal in BLOCK_SIGNALS)


def parse_count(platform: str, html: str) -> int | None:
    """Read a result count, or return None. Never guesses, never returns zero.

    Zero is refused on purpose. It is what a blocked page reports, it is falsy
    so it disappears into every truthiness check downstream, and "0 homes you
    are missing" is a sentence this app must never say when it simply could not
    look.
    """
    if not html or looks_blocked(html):
        return None
    if platform != "HotPads":
        # Nothing else has been verified against a real response. See module
        # docstring; adding one means proving it first.
        return None
    match = _TITLE_COUNT.search(_title(html))
    if not match:
        return None
    try:
        count = int(match.group(1).replace(",", ""))
    except ValueError:
        return None
    if count <= 0 or count > MAXIMUM_CREDIBLE_COUNT:
        return None
    return count


def fetch_count(client: httpx.Client, platform: str, url: str) -> int | None:
    """Ask one source how much it is holding. Any failure answers None.

    Deliberately total: this rides along on a scan whose real job is collecting
    homes, and must never be able to fail it, slow it noticeably, or change what
    it reports.
    """
    if platform != "HotPads" or not url:
        LOGGER.debug("%s has no verified way to be counted", platform)
        return None
    try:
        response = client.get(url, timeout=20.0)
    except Exception as exc:
        # Named rather than silent: "could not be counted" covers a refusal, a
        # timeout and a redesign, and telling them apart is the difference
        # between a known limit and a bug nobody notices.
        LOGGER.info("Counting %s failed: %s: %s", platform, type(exc).__name__, exc)
        return None
    if response.status_code != 200:
        LOGGER.info("Counting %s refused with HTTP %s", platform, response.status_code)
        return None
    count = parse_count(platform, response.text)
    if count is None:
        LOGGER.info(
            "Counting %s parsed nothing from %s bytes (blocked=%s)",
            platform,
            len(response.text),
            looks_blocked(response.text),
        )
    return count


# Where each site's San Francisco search actually lives. Every one of these was
# opened in a browser and checked: hotpads.com/san-francisco-ca/apartments-for-rent
# and apartments.com/apartments/san-francisco-ca/ both load real results, and
# roomies.com/rooms-for-rent/san-francisco--ca -- the obvious guess -- returns
# "We couldn't find what you were looking for", so it is /san-francisco-ca.
ALERT_SETUP_SEARCHES = {
    "HotPads": "https://hotpads.com/san-francisco-ca/apartments-for-rent",
    "Apartments.com": "https://www.apartments.com/apartments/san-francisco-ca/",
    "Roomies": "https://www.roomies.com/san-francisco-ca",
}


def missing_coverage_targets(repository, preferences) -> list[tuple[str, str]]:
    """Which disconnected sources are worth asking, and where to ask them.

    A provider that is already working needs no invitation and no number, so it
    is not asked -- both to save the request and because the question is
    meaningless once its alerts are arriving.
    """
    try:
        states = repository.connector_states()
    except Exception:
        states = {}
    targets: list[tuple[str, str]] = []
    for key, platform in GMAIL_PROVIDERS:
        url = ALERT_SETUP_SEARCHES.get(platform)
        if not url:
            continue
        state = states.get(key)
        if state is not None and getattr(state, "state", None) == "working":
            continue
        targets.append((platform, url))
    return targets


def coverage_is_fresh(taken_at: object, *, now: datetime | None = None) -> bool:
    """Was this count taken recently enough to be worth asking again around?"""
    moment = _parse_moment(taken_at)
    if moment is None:
        return False
    return (now or datetime.now(UTC)) - moment < COVERAGE_REFRESH_AFTER


def coverage_is_showable(taken_at: object, *, now: datetime | None = None) -> bool:
    """Is this count recent enough to put in front of somebody at all?"""
    moment = _parse_moment(taken_at)
    if moment is None:
        return False
    return (now or datetime.now(UTC)) - moment < COVERAGE_STALE_AFTER


def _parse_moment(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
