"""One building as several sources describe it.

A reader on the listing page is deciding one thing: is this worth going to
look at. A single source cannot answer that -- Redfin publishes buildings it
has no rent for, Rent.com publishes rents for buildings it describes thinly --
but two sources at one address can.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.preferences import parse_preferences
from sf_housing.scoring import score_listing
from tests.conftest import TEST_PREFERENCES


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    instance = Repository(tmp_path / "housing.sqlite3")
    instance.initialize()
    return instance


PREFERENCES = parse_preferences(TEST_PREFERENCES)


def store(repository: Repository, *, platform: str, address: str | None, price: int | None,
          slug: str, title: str = "A building", status: str = "active") -> int:
    listing = ListingCandidate(
        platform=platform,
        source_id=slug,
        title=title,
        original_url=f"https://example.test/{platform.lower().replace('.', '')}/{slug}",
        price=price,
        neighborhood="Potrero Hill",
        listing_type="Apartment building",
        summary=f"{title} on {platform}.",
        metadata={"address": address} if address else {},
        housing_kind="whole_unit",
    )
    listing_id, _ = repository.upsert_listing(listing, score_listing(listing, PREFERENCES))
    if status != "active":
        repository.set_listing_status(listing_id, status)
    return listing_id


def test_two_sources_at_one_address_corroborate_each_other(repository: Repository) -> None:
    redfin = store(repository, platform="Redfin", address="800 Indiana St", price=4290, slug="a")
    store(repository, platform="Rent.com", address="800 Indiana Street", price=4350, slug="b",
          title="Avalon Dogpatch")

    found = repository.corroborations(redfin)

    assert [item["platform"] for item in found] == ["Rent.com"]
    assert found[0]["price"] == 4350
    assert found[0]["title"] == "Avalon Dogpatch"


def test_the_match_survives_how_each_site_writes_an_address(repository: Repository) -> None:
    """"800 Indiana St" and "800 Indiana Street Unit 4" are one building. Left
    to exact strings this feature would almost never fire."""
    subject = store(repository, platform="Redfin", address="800 Indiana St", price=None, slug="a")
    store(repository, platform="Rent.com", address="800 Indiana Street Unit 4", price=4350, slug="b")

    assert len(repository.corroborations(subject)) == 1


def test_a_different_building_is_not_corroboration(repository: Repository) -> None:
    subject = store(repository, platform="Redfin", address="800 Indiana St", price=None, slug="a")
    store(repository, platform="Rent.com", address="802 Indiana St", price=4350, slug="b")
    store(repository, platform="Zumper", address="800 Tennessee St", price=4100, slug="c")

    assert repository.corroborations(subject) == []


def test_a_source_repeating_itself_is_not_a_second_opinion(repository: Repository) -> None:
    """Two cards from one site are that site listing two homes in a building,
    not two sources agreeing that the building exists."""
    subject = store(repository, platform="Redfin", address="800 Indiana St", price=None, slug="a")
    store(repository, platform="Redfin", address="800 Indiana St", price=4290, slug="b")

    assert repository.corroborations(subject) == []


def test_a_listing_never_corroborates_itself(repository: Repository) -> None:
    subject = store(repository, platform="Redfin", address="800 Indiana St", price=4290, slug="a")
    assert repository.corroborations(subject) == []


def test_a_home_already_passed_on_is_not_offered_as_evidence(repository: Repository) -> None:
    subject = store(repository, platform="Redfin", address="800 Indiana St", price=None, slug="a")
    store(repository, platform="Rent.com", address="800 Indiana St", price=4350, slug="b",
          status="dismissed")

    assert repository.corroborations(subject) == []


def test_a_published_rent_is_offered_before_a_missing_one(repository: Repository) -> None:
    """It is the reason the reader opened the page.

    The priced source is named last alphabetically on purpose: ordered by name
    the answer would come out the same, and the test would pass whether or not
    anything looked at the rent at all."""
    subject = store(repository, platform="Redfin", address="800 Indiana St", price=None, slug="a")
    store(repository, platform="Apartment List", address="800 Indiana St", price=None, slug="b")
    store(repository, platform="Rent.com", address="800 Indiana St", price=None, slug="c")
    store(repository, platform="Zumper", address="800 Indiana St", price=4350, slug="d")

    ordered = [item["platform"] for item in repository.corroborations(subject)]

    assert ordered == ["Zumper", "Apartment List", "Rent.com"]
    assert ordered[0] != sorted(ordered)[0], "name order and rent order must disagree here"


def test_a_listing_with_no_address_asks_nothing(repository: Repository) -> None:
    subject = store(repository, platform="Craigslist", address=None, price=1800, slug="a")
    store(repository, platform="Rent.com", address="800 Indiana St", price=4350, slug="b")

    assert repository.corroborations(subject) == []


def test_a_missing_listing_is_not_an_error(repository: Repository) -> None:
    assert repository.corroborations(99999) == []


# --------------------------------------------------------------------------
# what the reader sees
# --------------------------------------------------------------------------


def test_the_page_names_the_other_sources(repository: Repository, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from tests.test_dashboard import app_settings

    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    live = Repository(settings.database_path)
    live.initialize()
    subject = store(live, platform="Redfin", address="777 Tennessee St", price=None, slug="a",
                    title="777 Tennessee St")
    store(live, platform="Rent.com", address="777 Tennessee Street", price=4290, slug="b",
          title="Potrero 1010")

    with TestClient(application) as client:
        page = client.get(f"/listings/{subject}")

    assert page.status_code == 200
    assert "Also listed elsewhere" in page.text
    assert "Potrero 1010" in page.text
    assert "$4,290" in page.text
    # The point of the section: one source answered what the other left open.
    assert "publishes a rent for this building and Redfin does not" in page.text


def test_the_page_says_nothing_when_no_other_source_has_it(repository: Repository, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from tests.test_dashboard import app_settings

    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    live = Repository(settings.database_path)
    live.initialize()
    only = store(live, platform="Redfin", address="777 Tennessee St", price=4290, slug="a")

    with TestClient(application) as client:
        page = client.get(f"/listings/{only}")

    assert page.status_code == 200
    assert "Also listed elsewhere" not in page.text
