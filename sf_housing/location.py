"""Small, explicit location checks shared by source parsing and scoring."""

from __future__ import annotations

import re


_OUTSIDE_SF_CITIES = (
    "alameda",
    "belmont",
    "berkeley",
    "burlingame",
    "campbell",
    "concord",
    "cupertino",
    "daly city",
    "danville",
    "dublin",
    "emeryville",
    "foster city",
    "fremont",
    "half moon bay",
    "hayward",
    "los altos",
    "los gatos",
    "menlo park",
    "milpitas",
    "mill valley",
    "mountain view",
    "oakland",
    "pacifica",
    "palo alto",
    "pleasanton",
    "redwood city",
    "richmond",
    "san bruno",
    "san carlos",
    "san diego",
    "san jose",
    "san leandro",
    "san mateo",
    "san ramon",
    "san rafael",
    "sausalito",
    "south san francisco",
    "santa clara",
    "sunnyvale",
    "tiburon",
    "union city",
    "walnut creek",
)

_UNAMBIGUOUS_OUTSIDE_SF_CITIES = tuple(city for city in _OUTSIDE_SF_CITIES if city != "richmond")

# Shared with scoring: "south san francisco" contains the string a naive
# San Francisco check would accept, and is a different city.
OUTSIDE_SF_CITIES = _OUTSIDE_SF_CITIES


def declared_outside_sf_area_hint(text: str | None) -> str | None:
    """Return a clearly declared city outside San Francisco."""
    normalized = re.sub(r"\s+", " ", (text or "").casefold()).strip()
    for city in _OUTSIDE_SF_CITIES:
        if re.search(rf"(?<!\w){re.escape(city)}(?!\w)\s*,?\s+ca(?:\s+\d{{5}})?\b", normalized):
            return f"{city.title()} (outside SF)"
    # Group posts and short public cards often omit the state. Only accept
    # strong location grammar for unambiguous city names; "Richmond" alone can
    # mean San Francisco's Richmond District and therefore still requires CA.
    location_prefix = (
        r"(?:in|at|near|around|location\s*:\s*|located\s+in|available\s+in|"
        r"for\s+rent\s+in|downtown|central|north|south|east|west)\s+"
    )
    for city in _UNAMBIGUOUS_OUTSIDE_SF_CITIES:
        if re.search(rf"{location_prefix}(?:the\s+)?{re.escape(city)}(?!\w)", normalized):
            return f"{city.title()} (outside SF)"
        if re.match(rf"^[^a-z0-9]{{0,8}}{re.escape(city)}(?!\w)", normalized):
            return f"{city.title()} (outside SF)"
    return None


def declared_outside_sf_url_hint(url: str | None) -> str | None:
    """Return an outside city explicitly encoded in a Craigslist detail URL."""
    normalized = (url or "").casefold()
    for city in _OUTSIDE_SF_CITIES:
        slug = city.replace(" ", "-")
        if re.search(rf"/view/d/{re.escape(slug)}(?:-|/)", normalized):
            return f"{city.title()} (outside SF)"
    return None
