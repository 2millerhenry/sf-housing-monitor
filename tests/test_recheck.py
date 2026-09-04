"""A home that was taken down must stop looking current.

Every listing is enriched once, when it is first collected, and never looked at
again. A room verified live on Monday and deleted on Wednesday therefore stayed
on the shortlist looking exactly as current as one posted this morning; on a
real board, four of six shortlisted homes that had stopped appearing in searches
were already deleted on Craigslist.

Absence from one search is not proof -- a source returns one page and an older
post falls off it -- so the scanner goes and reads the page. The source already
reports a removed post, and scoring already refuses one; what was missing was
anyone ever asking again.
"""

from __future__ import annotations

import pathlib
from dataclasses import replace

import pytest

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.preferences import parse_preferences
from sf_housing.scanner import Scanner
from tests.conftest import TEST_PREFERENCES


def room(source_id: str, title: str = "Sunny private room in NOPA") -> ListingCandidate:
    return ListingCandidate(
        platform="Craigslist",
        source_id=source_id,
        title=title,
        original_url=f"https://sfbay.craigslist.org/roo/d/x/{source_id}.html",
        price=1500,
        neighborhood="NOPA",
        listing_type="Room/share",
        summary="A private room in a shared home, flexible lease, laundry on site.",
    )


class Source:
    """A source whose search stops returning a listing that is still stored."""

    platform = "Craigslist"
    mode = "automatic"
    search_url = "https://example.test/search"
    manual_reason = None
    detail_budget = 0

    def __init__(self, returns, *, removed=(), unreachable=(), recheck_budget=6):
        self._returns = list(returns)
        self.removed = set(removed)
        self.unreachable = set(unreachable)
        self.recheck_budget = recheck_budget
        self.enriched: list[str] = []

    def search(self, client, preferences):
        return list(self._returns)

    def enrich(self, client, listing):
        self.enriched.append(listing.source_id)
        if listing.source_id in self.unreachable:
            raise RuntimeError("connection reset")
        if listing.source_id in self.removed:
            return replace(
                listing,
                metadata={
                    **listing.metadata,
                    "verified_inactive": True,
                    "verification_concern": "Verified inactive: Craigslist has removed this post.",
                },
            )
        return replace(listing, summary="Still up, with a full description.")


def board(tmp_path: pathlib.Path):
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    preferences = parse_preferences(TEST_PREFERENCES)
    return repository, preferences


def shortlist(repository: Repository, preferences) -> set[str]:
    return {
        row["source_id"]
        for row in repository.query_listings(
            preferences.minimum_score, view="active", housing_kind="room"
        )
    }


def test_a_home_taken_down_stops_appearing_on_the_shortlist(tmp_path: pathlib.Path) -> None:
    """The whole point."""
    repository, preferences = board(tmp_path)
    first = Source([room("stays"), room("taken-down")])
    Scanner(repository, lambda: preferences, [first]).run_scan("manual")
    assert shortlist(repository, preferences) == {"stays", "taken-down"}

    # The next search no longer returns it, and its page says it is gone.
    second = Source([room("stays")], removed={"taken-down"})
    Scanner(repository, lambda: preferences, [second]).run_scan("scheduled")

    assert "taken-down" in second.enriched, "the home has to actually be rechecked"
    assert shortlist(repository, preferences) == {"stays"}


def test_the_home_is_kept_and_explained_rather_than_deleted(tmp_path: pathlib.Path) -> None:
    """You may have starred it. It has to still be there, saying what happened."""
    repository, preferences = board(tmp_path)
    Scanner(repository, lambda: preferences, [Source([room("gone")])]).run_scan("manual")
    listing_id = repository.query_listings(0, view="all", housing_kind="room")[0]["id"]
    repository.set_listing_status(listing_id, "saved")

    Scanner(repository, lambda: preferences, [Source([], removed={"gone"})]).run_scan("scheduled")

    stored = repository.listing(listing_id)
    assert stored is not None, "the home must not be deleted"
    assert stored["status"] == "saved", "and must keep the star you put on it"
    reasons = [entry["reason"] for entry in stored["blockers"]]
    assert any("inactive" in reason for reason in reasons), reasons


def test_absence_alone_never_marks_a_home_gone(tmp_path: pathlib.Path) -> None:
    """A source returns one page; an older post falls off it while still live.
    Only the page itself decides."""
    repository, preferences = board(tmp_path)
    Scanner(repository, lambda: preferences, [Source([room("still-live")])]).run_scan("manual")

    quiet = Source([])  # returns nothing, but the page says the home is fine
    Scanner(repository, lambda: preferences, [quiet]).run_scan("scheduled")

    assert quiet.enriched == ["still-live"], "it is checked"
    assert shortlist(repository, preferences) == {"still-live"}, "and kept, because it is still up"


def test_a_recheck_that_cannot_reach_the_source_changes_nothing(tmp_path: pathlib.Path) -> None:
    """Unreachable is not gone. This is the difference between a careful product
    and one that empties your shortlist during a wifi drop."""
    repository, preferences = board(tmp_path)
    Scanner(repository, lambda: preferences, [Source([room("live")])]).run_scan("manual")
    before = repository.query_listings(0, view="all", housing_kind="room")[0]

    offline = Source([], unreachable={"live"})
    outcome = Scanner(repository, lambda: preferences, [offline]).run_scan("scheduled")

    after = repository.query_listings(0, view="all", housing_kind="room")[0]
    assert outcome.status == "completed", "a failed recheck is not a failed scan"
    assert after["score"] == before["score"]
    assert after["eligibility"] == before["eligibility"]
    assert shortlist(repository, preferences) == {"live"}


def test_a_home_the_search_still_returns_is_not_rechecked(tmp_path: pathlib.Path) -> None:
    """Rechecking costs a request. Only silence earns one."""
    repository, preferences = board(tmp_path)
    Scanner(repository, lambda: preferences, [Source([room("here")])]).run_scan("manual")

    again = Source([room("here")])
    Scanner(repository, lambda: preferences, [again]).run_scan("scheduled")

    assert again.enriched == [], "a home in the results needs no recheck"


def test_only_shortlisted_homes_earn_a_recheck(tmp_path: pathlib.Path) -> None:
    """Rechecking everything ever collected would be a different product."""
    repository, preferences = board(tmp_path)
    good = room("good")
    poor = replace(room("poor"), price=9000, title="Overpriced room")
    Scanner(repository, lambda: preferences, [Source([good, poor])]).run_scan("manual")

    quiet = Source([])
    Scanner(repository, lambda: preferences, [quiet]).run_scan("scheduled")

    assert "good" in quiet.enriched
    assert "poor" not in quiet.enriched


def test_the_recheck_budget_is_bounded(tmp_path: pathlib.Path) -> None:
    repository, preferences = board(tmp_path)
    many = [room(f"r{index}") for index in range(20)]
    Scanner(repository, lambda: preferences, [Source(many)]).run_scan("manual")

    quiet = Source([], recheck_budget=3)
    Scanner(repository, lambda: preferences, [quiet]).run_scan("scheduled")

    assert len(quiet.enriched) == 3


def test_a_home_is_not_rechecked_twice_in_the_same_day(tmp_path: pathlib.Path) -> None:
    """Attention rotates; it does not land on the same home every run."""
    repository, preferences = board(tmp_path)
    Scanner(repository, lambda: preferences, [Source([room("a"), room("b")])]).run_scan("manual")

    first = Source([], recheck_budget=1)
    Scanner(repository, lambda: preferences, [first]).run_scan("scheduled")
    second = Source([], recheck_budget=1)
    Scanner(repository, lambda: preferences, [second]).run_scan("scheduled")

    assert first.enriched and second.enriched
    assert first.enriched != second.enriched, "the second run moves on to the other home"


def test_a_source_whose_search_failed_rechecks_nothing(tmp_path: pathlib.Path) -> None:
    """Silence from a broken source is not evidence about any home."""
    repository, preferences = board(tmp_path)
    Scanner(repository, lambda: preferences, [Source([room("live")])]).run_scan("manual")

    class Broken(Source):
        def search(self, client, preferences):
            raise RuntimeError("Craigslist is unreachable")

    broken = Broken([])
    outcome = Scanner(repository, lambda: preferences, [broken]).run_scan("scheduled")

    assert outcome.sources_failed == 1
    assert broken.enriched == [], "a source that could not search must not judge its homes"
    assert shortlist(repository, preferences) == {"live"}


# --------------------------------------------------------------------------
# how the source says "this is gone"
# --------------------------------------------------------------------------


class Reply:
    def __init__(self, status: int, text: str = "<html><body>ok</body></html>"):
        self.status_code = status
        self.text = text
        self.url = "https://www.craigslist.org/view/d/room/1.html"

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"Client error '{self.status_code}'", request=None, response=None
            )


class Replying:
    def __init__(self, reply):
        self.reply = reply

    def get(self, url, **kwargs):
        return self.reply


@pytest.mark.parametrize("status", [404, 410])
def test_a_status_that_means_gone_is_an_answer_not_a_failure(status: int) -> None:
    """Craigslist answers 410 for a deleted post. raise_for_status turned that
    into an exception that read as a dropped connection, so the home was left
    alone and kept its place on the shortlist -- the removal check below it
    could only ever run on a post that still returned a page."""
    from sf_housing.sources import CraigslistSource

    enriched = CraigslistSource().enrich(Replying(Reply(status)), room("gone"))

    assert enriched.metadata["verified_inactive"] is True
    assert str(status) in enriched.metadata["verification_concern"]


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_a_status_that_means_try_later_still_raises(status: int) -> None:
    """A server error is not the source saying the home is gone."""
    import httpx

    from sf_housing.sources import CraigslistSource

    with pytest.raises(httpx.HTTPStatusError):
        CraigslistSource().enrich(Replying(Reply(status)), room("maybe"))


def test_a_page_that_still_loads_and_says_removed_is_also_gone() -> None:
    from sf_housing.sources import CraigslistSource

    page = '<html><body><div class="removed">This posting has been deleted by its author.</div></body></html>'
    enriched = CraigslistSource().enrich(Replying(Reply(200, page)), room("deleted"))

    assert enriched.metadata["verified_inactive"] is True
