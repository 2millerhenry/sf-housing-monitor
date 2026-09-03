from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.potrero import (
    OFFICIAL_SITE_AUDIT,
    POTRERO_CONTACTS,
    POTRERO_LISTINGS,
    SOURCE_COVERAGE,
)
from tests.test_dashboard import app_settings


def test_potrero_research_is_unique_affordable_and_household_safe() -> None:
    assert len(POTRERO_LISTINGS) >= 25
    assert len({listing["url"] for listing in POTRERO_LISTINGS}) == len(POTRERO_LISTINGS)
    assert [listing["rank"] for listing in POTRERO_LISTINGS] == list(
        range(1, len(POTRERO_LISTINGS) + 1)
    )

    for listing in POTRERO_LISTINGS:
        assert 800 <= int(listing["personal_cost"]) <= 3000
        assert str(listing["url"]).startswith("https://")
        assert listing["verified"] == "August 4, 2026"
        assert listing["address"]
        assert listing["beds_baths"]
        assert listing["availability"]
        assert listing["furnished"]
        assert listing["household"]
        assert listing["uncertainty"]


def test_potrero_page_has_dedicated_navigation_and_complete_comparison(tmp_path: Path) -> None:
    application = create_app(
        settings=app_settings(tmp_path), sources=[], enable_scheduler=False
    )

    with TestClient(application) as client:
        response = client.get("/potrero")

    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select("[data-potrero-listing]")
    assert len(rows) == len(POTRERO_LISTINGS)
    assert soup.select_one('nav a[href="/potrero"][aria-current="page"]') is not None
    assert "Eight best opportunities" in response.text
    assert "Worth contacting directly" in response.text
    assert "Building and manager site sweep" in response.text
    assert len(soup.select("[data-official-site]")) == len(OFFICIAL_SITE_AUDIT)
    assert "The Grid Potrero Hill" in response.text
    assert "$3,057/person" in response.text
    assert "EVE Community Village" in response.text
    assert "No availability" in response.text
    assert "AMSI furnished condo at The Potrero" in response.text
    assert "Facebook Marketplace and housing groups" in response.text
    assert "pending" in response.text.lower()
    assert "$1,500 bedroom in shared Potrero house" in response.text
    assert "740 Rhode Island St Unit 208" in response.text
    assert "Verified August 4, 2026" in response.text
    assert "Household, furnishing, and checks" in response.text
    assert len(POTRERO_CONTACTS) >= 5
    assert len(OFFICIAL_SITE_AUDIT) >= 12
    assert all(str(item["url"]).startswith("https://") for item in OFFICIAL_SITE_AUDIT)
    assert all(item["verified"] == "August 4, 2026" for item in OFFICIAL_SITE_AUDIT)
    assert len(SOURCE_COVERAGE) >= 6


def test_potrero_page_does_not_count_removed_or_pending_units_as_active(tmp_path: Path) -> None:
    application = create_app(
        settings=app_settings(tmp_path), sources=[], enable_scheduler=False
    )

    with TestClient(application) as client:
        response = client.get("/potrero")

    soup = BeautifulSoup(response.text, "html.parser")
    active_text = " ".join(row.get_text(" ", strip=True) for row in soup.select("[data-potrero-listing]"))
    watchlist = soup.select_one("#contact-list").find_parent("section").get_text(" ", strip=True)
    assert "328B Connecticut" not in active_text
    assert "1029 Rhode Island" not in active_text
    assert "328B Connecticut" in watchlist
    assert "1029 Rhode Island" in watchlist
    assert "Pending" not in active_text
