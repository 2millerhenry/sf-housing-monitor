#!/usr/bin/env python3
"""Build the shipped street-to-neighbourhood table from San Francisco open data.

The city housing portal publishes a street address but never a neighbourhood,
and most SF ZIP codes straddle two areas, so a ZIP cannot answer the question.
This turns DataSF's address dataset into a small table the app can consult
offline: no network call during a scan, nothing to rate-limit, and no third
party told which homes a user is looking at.

The unit is the hundred block, not the street. A street-wide address range is
destroyed by a single mis-geocoded address -- one stray record put Russian Hill
on 0-3040 Larkin St, which swallows the whole of Nob Hill and the Tenderloin.
Grouping by block instead makes 94% of blocks unanimous, and a block that is
genuinely split is recorded as contested so the resolver refuses it.

Run it to refresh the data; the output is committed so the app never needs it.

    python scripts/build_street_neighborhoods.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "sf_housing" / "data" / "sf_streets.json"
DATASET = "https://data.sfgov.org/resource/ramy-di5m.json"

# A block where the winning neighbourhood holds less than this share of the
# addresses is a real boundary rather than a stray record, and is recorded as
# contested. A wrong neighbourhood costs more than an unknown one, because area
# carries the most weight in the score.
DOMINANCE = 0.8

# DataSF publishes 41 "analysis neighborhoods"; the product has its own
# vocabulary. Only names with one honest counterpart are mapped. An analysis
# neighbourhood covering several of the product's areas -- "Sunset/Parkside" is
# both Inner Sunset and Parkside, "West of Twin Peaks" is four of them -- is
# deliberately left out, and its blocks are recorded as contested so a number
# falling in one resolves to nothing rather than to a neighbour.
ANALYSIS_TO_CANONICAL = {
    "Bayview Hunters Point": "Bayview",
    "Bernal Heights": "Bernal Heights",
    "Castro/Upper Market": "Castro",
    "Chinatown": "Chinatown",
    "Excelsior": "Excelsior",
    "Financial District/South Beach": "Financial District",
    "Glen Park": "Glen Park",
    "Haight Ashbury": "Haight-Ashbury",
    "Hayes Valley": "Hayes Valley",
    "Inner Richmond": "Inner Richmond",
    "Inner Sunset": "Inner Sunset",
    "Marina": "Marina",
    "Mission": "Mission District",
    "Mission Bay": "Mission Bay",
    "Nob Hill": "Nob Hill",
    "Noe Valley": "Noe Valley",
    "North Beach": "North Beach",
    "Outer Mission": "Outer Mission",
    "Outer Richmond": "Outer Richmond",
    "Pacific Heights": "Pacific Heights",
    "Portola": "Portola",
    "Potrero Hill": "Potrero Hill",
    "Presidio Heights": "Presidio Heights",
    "Russian Hill": "Russian Hill",
    "Seacliff": "Sea Cliff",
    "South of Market": "SoMa",
    "Tenderloin": "Tenderloin",
    "Twin Peaks": "Twin Peaks",
    "Visitacion Valley": "Visitacion Valley",
    "Western Addition": "Western Addition",
}


def fetch() -> list[dict]:
    """One grouped query; the whole city comes back in ~11,000 rows."""
    query = urlencode(
        {
            "$select": (
                "street_name,street_type,nhood,"
                "floor(address_number/100) AS blk,count(*) AS n"
            ),
            "$group": "street_name,street_type,nhood,blk",
            "$limit": 100000,
        }
    )
    with urlopen(f"{DATASET}?{query}", timeout=300) as response:
        rows = json.loads(response.read().decode("utf-8"))
    if len(rows) >= 100000:  # pragma: no cover - a silent truncation would ship
        raise SystemExit("the dataset outgrew one page; add paging before trusting this")
    return rows


def build(rows: list[dict]) -> dict:
    counts: dict[tuple[str, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        name = str(row.get("street_name") or "").strip().upper()
        suffix = str(row.get("street_type") or "").strip().upper()
        analysis = str(row.get("nhood") or "").strip()
        if not name or not analysis:
            continue
        try:
            block, count = int(row["blk"]), int(row["n"])
        except (KeyError, TypeError, ValueError):
            continue
        if block < 0 or count <= 0:
            continue
        counts[(f"{name} {suffix}".strip(), block)][analysis] += count

    names: list[str] = sorted(set(ANALYSIS_TO_CANONICAL.values()))
    index = {name: position for position, name in enumerate(names)}
    streets: dict[str, dict[str, int]] = defaultdict(dict)
    contested = 0
    for (street, block), tally in counts.items():
        winner, votes = max(tally.items(), key=lambda item: (item[1], item[0]))
        canonical = ANALYSIS_TO_CANONICAL.get(winner)
        if canonical is None or votes / sum(tally.values()) < DOMINANCE:
            # -1 means "the city's own data disagrees here". Recording it is the
            # point: an omitted block would fall through to the street-wide
            # answer, which is exactly the wrong answer on a boundary block.
            streets[street][str(block)] = -1
            contested += 1
            continue
        streets[street][str(block)] = index[canonical]

    return {
        "source": "DataSF Addresses with Units (ramy-di5m)",
        "note": (
            "Hundred block -> neighbourhood, per street. -1 means the block is "
            "split between neighbourhoods and must resolve to nothing."
        ),
        "dominance": DOMINANCE,
        "names": names,
        "streets": {street: dict(sorted(blocks.items(), key=lambda i: int(i[0])))
                    for street, blocks in sorted(streets.items())},
        "contested_blocks": contested,
    }


def main() -> None:
    table = build(fetch())
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(table, separators=(",", ":")), encoding="utf-8")
    blocks = sum(len(b) for b in table["streets"].values())
    print(f"streets:  {len(table['streets'])}")
    print(f"blocks:   {blocks} ({blocks - table['contested_blocks']} resolve, "
          f"{table['contested_blocks']} contested)")
    print(f"written:  {OUTPUT} ({OUTPUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    sys.exit(main())
