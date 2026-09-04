"""Small, explicit location checks shared by source parsing and scoring."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .deal_profile import SF_NEIGHBORHOODS


_OUTSIDE_SF_CITIES = (
    "alameda",
    "albany",
    "american canyon",
    "antioch",
    "belmont",
    "benicia",
    "berkeley",
    "brisbane",
    "burlingame",
    "calistoga",
    "campbell",
    "castro valley",
    "concord",
    "corte madera",
    "cupertino",
    "daly city",
    "danville",
    "dublin",
    "el cerrito",
    "emeryville",
    "fairfield",
    "foster city",
    "fremont",
    "half moon bay",
    "hayward",
    "healdsburg",
    "lafayette",
    "larkspur",
    "livermore",
    "los altos",
    "los gatos",
    "martinez",
    "menlo park",
    "mill valley",
    "millbrae",
    "milpitas",
    "moraga",
    "mountain view",
    "napa",
    "newark",
    "novato",
    "oakland",
    "orinda",
    "pacifica",
    "palo alto",
    "petaluma",
    "pleasanton",
    "redwood city",
    "richmond",
    "rohnert park",
    "san anselmo",
    "san bruno",
    "san carlos",
    "san diego",
    "san jose",
    "san leandro",
    "san lorenzo",
    "san mateo",
    "san pablo",
    "san rafael",
    "san ramon",
    "santa clara",
    "santa rosa",
    "sausalito",
    "sonoma",
    "south san francisco",
    "st helena",
    "suisun city",
    "sunnyvale",
    "tiburon",
    "union city",
    "vacaville",
    "vallejo",
    "walnut creek",
    "windsor",
    "yountville",
)

# Cities whose names are also San Francisco streets, parks or districts. A
# listing near Lafayette Park or on Vallejo Street is in San Francisco, so these
# need "<city>, CA" spelled out before they mean anywhere else.
_ALSO_SAN_FRANCISCO_PLACES = frozenset(
    {"richmond", "lafayette", "moraga", "vallejo", "napa", "santa rosa", "corte madera", "sonoma"}
)

_UNAMBIGUOUS_OUTSIDE_SF_CITIES = tuple(
    city for city in _OUTSIDE_SF_CITIES if city not in _ALSO_SAN_FRANCISCO_PLACES
)

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


def outside_sf_location_label(label: str | None) -> str | None:
    """Return the city when a search card's own area label is another one.

    Craigslist pads a thin San Francisco search with the rest of the Bay Area,
    and those cards label themselves plainly ("Napa"). Only an exact match
    counts, and only for a name that is not also a San Francisco place: the
    city's own area labels include "richmond / seacliff", which must never read
    as the city of Richmond.
    """
    normalized = re.sub(r"\s+", " ", (label or "").casefold()).strip(" ()")
    if not normalized:
        return None
    for city in _UNAMBIGUOUS_OUTSIDE_SF_CITIES:
        if normalized == city:
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


# ---------------------------------------------------------------------------
# street address -> neighbourhood
# ---------------------------------------------------------------------------

# The city housing portal states a street address and a ZIP but never a
# neighbourhood, and most SF ZIPs straddle two areas, so the ZIP could only
# answer for a handful of listings. The table below is built from the city's own
# address dataset by scripts/build_street_neighborhoods.py and consulted
# offline: no network call during a scan, and no third party told which homes
# someone is looking at.

_STREET_TABLE_PATH = Path(__file__).resolve().parent / "data" / "sf_streets.json"

# Written form -> the abbreviation the city's dataset uses.
_STREET_TYPES = {
    "ALLEY": "ALY", "ALY": "ALY",
    "AV": "AVE", "AVE": "AVE", "AVENUE": "AVE",
    "BLVD": "BLVD", "BOULEVARD": "BLVD",
    "CIR": "CIR", "CIRCLE": "CIR",
    "COURT": "CT", "CT": "CT",
    "DR": "DR", "DRIVE": "DR",
    "HIGHWAY": "HWY", "HWY": "HWY",
    "LANE": "LN", "LN": "LN",
    "LOOP": "LOOP",
    "PARK": "PARK",
    "PARKWAY": "PKWY", "PKWY": "PKWY",
    "PL": "PL", "PLACE": "PL",
    "PLAZA": "PLZ", "PLZ": "PLZ",
    "RD": "RD", "ROAD": "RD",
    "ST": "ST", "STREET": "ST",
    "STAIRWAY": "STWY", "STWY": "STWY",
    "TER": "TER", "TERR": "TER", "TERRACE": "TER",
    "WALK": "WALK",
    "WAY": "WAY", "WY": "WAY",
}

# The dataset zero-pads numbered streets ("03RD ST", "15TH AVE"), and people
# write the small ones as words.
_SPELLED_ORDINALS = {
    "FIRST": "01ST", "SECOND": "02ND", "THIRD": "03RD", "FOURTH": "04TH",
    "FIFTH": "05TH", "SIXTH": "06TH", "SEVENTH": "07TH", "EIGHTH": "08TH",
    "NINTH": "09TH", "TENTH": "10TH", "ELEVENTH": "11TH", "TWELFTH": "12TH",
}

# A half of a divided street ("Mission Bay Blvd North"). Tried only as a
# fallback: "Avenue E" and "Avenue N" on Treasure Island are streets in
# their own right.
_BLOCK_QUALIFIERS = {"NORTH", "SOUTH", "EAST", "WEST", "N", "S", "E", "W"}

# "1200 Market St #501", "Apt 2", "Unit B", "Ste 300".
_UNIT_SUFFIX = re.compile(
    r"\s*(?:#|\b(?:APT|APARTMENT|UNIT|STE|SUITE|RM|ROOM|FL|FLOOR|NO)\b\.?\s*)\S*.*$"
)
# "451 Kansas St at 17th St", "Powell St & Market St", cross streets and cities.
_TAIL = re.compile(r"\s+(?:AT|AND|NEAR|BETWEEN|BTWN)\s+.*$|\s*[,&/(].*$")

_STREET_TABLE: dict[str, dict[str, int]] | None = None
_STREET_NAMES: list[str] = []
_BARE_STREETS: dict[str, list[str]] = {}


def _load_street_table() -> dict[str, dict[str, int]]:
    """Read the shipped table once. A missing or broken file is not fatal: the
    portal simply goes back to answering "neighbourhood unknown"."""
    global _STREET_TABLE, _STREET_NAMES, _BARE_STREETS
    if _STREET_TABLE is not None:
        return _STREET_TABLE
    try:
        raw = json.loads(_STREET_TABLE_PATH.read_text(encoding="utf-8"))
        names = [str(name) for name in raw["names"]]
        streets = {str(k): {str(b): int(i) for b, i in v.items()}
                   for k, v in raw["streets"].items()}
    except (OSError, ValueError, KeyError, TypeError):
        names, streets = [], {}
    # A name the product no longer knows must not reach a listing, so drop it
    # here rather than letting an unknown area through to the score.
    known = [name if name in SF_NEIGHBORHOODS else "" for name in names]
    bare: dict[str, list[str]] = {}
    for key in streets:
        head = key.rsplit(" ", 1)[0] if " " in key else key
        bare.setdefault(head, []).append(key)
    _STREET_NAMES, _STREET_TABLE, _BARE_STREETS = known, streets, bare
    return _STREET_TABLE


def _normalise_street(text: str) -> str:
    words = []
    for word in text.split():
        word = _SPELLED_ORDINALS.get(word, word)
        ordinal = re.fullmatch(r"(\d{1,2})(ST|ND|RD|TH)", word)
        if ordinal:
            word = f"{int(ordinal.group(1)):02d}{ordinal.group(2)}"
        words.append(word)
    if len(words) > 1 and words[-1] in _STREET_TYPES:
        words[-1] = _STREET_TYPES[words[-1]]
    return " ".join(words)


def _street_variants(street: str) -> list[str]:
    """The spellings worth trying, most literal first.

    Each fallback is only reached when the more literal spelling is not a real
    street, which is what keeps "Mrs. Jackson Way" and "Avenue E" intact.
    """
    variants = [street]
    words = street.split()
    # "1201 Tennessee St." -- an abbreviation point the city does not record.
    # Only the final word is trimmed, because "Mayor Edwin M. Lee Ave" keeps its.
    if words and words[-1].endswith(".") and words[-1].rstrip(".") in _STREET_TYPES:
        variants.append(" ".join([*words[:-1], _STREET_TYPES[words[-1].rstrip(".")]]))
    # "588 Mission Bay Blvd North" -- the city records the north and south halves
    # of a divided street under the one name.
    for variant in list(variants):
        parts = variant.split()
        if len(parts) > 2 and parts[-1] in _BLOCK_QUALIFIERS:
            variants.append(" ".join(parts[:-1]))
    return variants


def _street_key(street: str) -> str | None:
    """Match a written street against the dataset's own spelling."""
    table = _load_street_table()
    for variant in _street_variants(street):
        if variant in table:
            return variant
        # No street type given ("1200 Market"): answer only when one street fits.
        candidates = _BARE_STREETS.get(variant, [])
        if len(candidates) == 1:
            return candidates[0]
    return None


def parse_street_address(address: str | None) -> tuple[int, str] | None:
    """Split a written address into its number and the dataset's street key."""
    text = re.sub(r"\s+", " ", str(address or "")).strip().upper()
    if not text:
        return None
    text = _TAIL.sub("", text)
    text = _UNIT_SUFFIX.sub("", text).strip()
    # A range ("1200-1250 Market St") is answered by its first number; a
    # trailing letter ("1200A") is a unit, not part of the number.
    leading = re.match(r"^(\d{1,5})(?:\s*-\s*\d{1,5})?[A-Z]?\s+(.+)$", text)
    if not leading:
        return None
    number, street = int(leading.group(1)), _normalise_street(leading.group(2))
    key = _street_key(street)
    return (number, key) if key else None


def sf_area_from_address(address: str | None) -> str | None:
    """Return the San Francisco neighbourhood a street address sits in.

    Answers from the hundred block, because a street-wide range is destroyed by
    one mis-geocoded record: a single stray address put Russian Hill across all
    of Larkin St, swallowing Nob Hill and the Tenderloin. A block the city's own
    data splits between neighbourhoods returns nothing, so the answer is stable
    from one scan to the next and never guesses a boundary.
    """
    if declared_outside_sf_area_hint(address):
        return None
    parsed = parse_street_address(address)
    if parsed is None:
        return None
    number, key = parsed
    blocks = _load_street_table()[key]
    position = blocks.get(str(number // 100))
    if position is None:
        # An address number the city has no record of. A street that is wholly
        # inside one neighbourhood can still answer; a street that crosses one
        # cannot.
        distinct = set(blocks.values())
        if len(distinct) != 1 or -1 in distinct:
            return None
        position = distinct.pop()
    if position < 0 or position >= len(_STREET_NAMES):
        return None
    return _STREET_NAMES[position] or None
