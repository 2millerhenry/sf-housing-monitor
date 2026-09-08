#!/usr/bin/env python3
"""Ask every source that needs no setup whether it still answers.

The question this exists for is "would somebody downloading this today get
listings", and the only honest way to answer it is to read the real pages. The
test suite proves each reader handles the shape it was written against; a
fixture cannot notice that a site changed that shape last week.

Deliberately configured the way a fresh download is -- no mailbox, no Apify --
so the sources checked here are exactly the ones a new person gets for free.
The deal is as wide as the app allows, because a narrow one would report a
working source as empty and this is a check of the source, not of the filter.

Paced on purpose. A source that answers is worth more than a source read
quickly, and reading these back to back is how an address gets refused.

    uv run python scripts/check_sources.py            # every free source
    uv run python scripts/check_sources.py Zillow     # just one
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sf_housing.preferences import Preferences, parse_preferences  # noqa: E402
from sf_housing.sources import MONITOR_HEADERS, default_sources  # noqa: E402

PATHS = ("private_room", "studio", "one_bedroom", "two_bedroom", "three_bedroom", "four_bedroom")
# Long enough that a slow source is reported slow rather than reported broken.
TIMEOUT = 30.0
# Between sources, not between pages: each source paces its own pages where it
# needs to. This is here so eighteen of them in a row does not read as a burst.
PAUSE_BETWEEN = 2.0

# How a site says "not you, not now" rather than "that reader is broken".
# Reported apart from a real failure because running this twice inside an hour
# is enough to produce them: ApartmentGuide answered with 488 homes and then,
# twenty minutes later, with one of these. A checker that calls that a broken
# source teaches you to distrust the checker.
THROTTLE_MARKERS = ("HTTP 202", "HTTP 403", "HTTP 429", "turned away", "rate-limit")


def widest_deal() -> Preferences:
    """Every home type, no ceiling worth speaking of, anywhere in the city."""
    return parse_preferences(
        yaml.safe_dump(
            {
                "profile_version": 1,
                "profile": {
                    "state": "active",
                    "enabled_paths": list(PATHS),
                    "budgets": {
                        path: {"maximum_monthly": 50000, "minimum_monthly": 1, "occupants": 4}
                        for path in PATHS
                    },
                    "geography": {"anywhere_in_sf": True},
                },
                "sources": {"max_results_per_source": 5000},
            }
        )
    )


def check(source, preferences: Preferences) -> tuple[str, int, float, str]:
    """Return (verdict, listings, seconds, detail) for one source."""
    started = time.monotonic()
    try:
        # The headers the scan itself uses. Reading these with a browser
        # string instead reports Zumper as broken, because Zumper answers this
        # app honestly and serves a browser a bot challenge -- a checker that
        # asks a different question than the app is worse than no checker.
        with httpx.Client(
            headers=dict(MONITOR_HEADERS), timeout=TIMEOUT, follow_redirects=True
        ) as client:
            found = source.search(client, preferences)
    except Exception as exc:  # noqa: BLE001 - every failure is a result here
        raw = str(exc).replace("\n", " ")
        elapsed = time.monotonic() - started
        if any(marker in raw for marker in THROTTLE_MARKERS):
            return "THROTTLED", 0, elapsed, raw[:88]
        return "FAILING", 0, elapsed, f"{type(exc).__name__}: {raw[:76]}"
    elapsed = time.monotonic() - started
    if found:
        return "WORKING", len(found), elapsed, ""
    # Not a failure by itself: a small landlord with nothing free answers this
    # way, and so does a reader whose page changed shape. Only reading the page
    # says which, so this is reported as its own verdict rather than as either.
    return "EMPTY", 0, elapsed, "answered, but returned no homes"


def main() -> int:
    wanted = {name.casefold() for name in sys.argv[1:]}
    preferences = widest_deal()
    # No mailbox and no Apify: the set a fresh download actually gets.
    free = [s for s in default_sources() if s.mode == "automatic"]
    if wanted:
        free = [s for s in free if s.platform.casefold() in wanted]

    print(f"Checking {len(free)} sources that need no setup, against the live pages.\n")
    results = []
    for index, source in enumerate(free, start=1):
        verdict, count, seconds, detail = check(source, preferences)
        results.append((source.platform, verdict, count, seconds, detail))
        shown = f"{count:>5}" if verdict == "WORKING" else "    -"
        print(f"  {verdict:<8} {source.platform:<26} {shown}  {seconds:5.1f}s  {detail}")
        if index < len(free):
            time.sleep(PAUSE_BETWEEN)

    working = [r for r in results if r[1] == "WORKING"]
    empty = [r for r in results if r[1] == "EMPTY"]
    throttled = [r for r in results if r[1] == "THROTTLED"]
    failing = [r for r in results if r[1] == "FAILING"]
    print(
        f"\n{len(working)} working, {len(throttled)} throttled, {len(empty)} answered but empty,"
        f" {len(failing)} failing  --  {sum(r[2] for r in results)} homes seen"
    )
    for platform, _, _, _, detail in failing:
        print(f"  FAILING    {platform}: {detail}")
    for platform, _, _, _, detail in throttled:
        print(f"  THROTTLED  {platform}: {detail}")
    for platform, _, _, _, _ in empty:
        print(f"  EMPTY      {platform}: read the page yourself before trusting this")
    if throttled:
        print(
            "\nThrottled is the site declining for now, not a broken reader. Running this\n"
            "again within the hour is usually what caused it; the app itself backs off and\n"
            "retries on its own."
        )
    # Only a reader that broke is worth failing on. A site declining today is
    # the thing this app is built to survive.
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
