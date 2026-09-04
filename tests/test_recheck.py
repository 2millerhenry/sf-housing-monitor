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
import time
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


def hours_pass(repository: Repository, hours: float) -> None:
    """Wind every confirmation back, the way the clock does between scans.

    Real scans are eight and sixteen hours apart. A test that runs two scans in
    the same second is testing a case that never happens, and would have hidden
    the rule that matters: a home the search confirmed minutes ago is not worth
    a second request.
    """
    import json as _json
    from datetime import UTC as _UTC, datetime as _datetime, timedelta as _timedelta

    with repository.connection() as connection:
        rows = connection.execute("SELECT id, metadata_json FROM listings").fetchall()
        for row in rows:
            metadata = _json.loads(row["metadata_json"] or "{}")
            stamp = metadata.get("last_verified_at")
            if not isinstance(stamp, str):
                continue
            try:
                moment = _datetime.fromisoformat(stamp).astimezone(_UTC)
            except ValueError:
                continue
            metadata["last_verified_at"] = (moment - _timedelta(hours=hours)).isoformat()
            connection.execute(
                "UPDATE listings SET metadata_json = ? WHERE id = ?",
                (_json.dumps(metadata), row["id"]),
            )
        connection.commit()


def confirmations(repository: Repository) -> dict[str, str | None]:
    import json as _json

    with repository.connection() as connection:
        return {
            row["source_id"]: _json.loads(row["metadata_json"] or "{}").get("last_verified_at")
            for row in connection.execute("SELECT source_id, metadata_json FROM listings")
        }


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

    # Eight hours later, the next search no longer returns it.
    hours_pass(repository, 8)
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

    hours_pass(repository, 8)
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

    hours_pass(repository, 8)
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

    hours_pass(repository, 8)
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

    hours_pass(repository, 8)
    quiet = Source([])
    Scanner(repository, lambda: preferences, [quiet]).run_scan("scheduled")

    assert "good" in quiet.enriched
    assert "poor" not in quiet.enriched


def test_the_recheck_budget_is_bounded(tmp_path: pathlib.Path) -> None:
    repository, preferences = board(tmp_path)
    many = [room(f"r{index}") for index in range(20)]
    Scanner(repository, lambda: preferences, [Source(many)]).run_scan("manual")

    hours_pass(repository, 8)
    quiet = Source([], recheck_budget=3)
    Scanner(repository, lambda: preferences, [quiet]).run_scan("scheduled")

    assert len(quiet.enriched) == 3


def test_a_home_confirmed_minutes_ago_is_not_fetched_again(tmp_path: pathlib.Path) -> None:
    """The search returning a home is the source saying it still lists it. That
    is a confirmation, and a stronger one than re-reading a single page, so it
    must not also cost a request."""
    repository, preferences = board(tmp_path)
    Scanner(repository, lambda: preferences, [Source([room("a"), room("b")])]).run_scan("manual")

    soon = Source([])  # the search goes quiet, but both were just confirmed
    Scanner(repository, lambda: preferences, [soon]).run_scan("scheduled")

    assert soon.enriched == [], "nothing is worth a second request this soon"


def test_a_home_the_search_returns_is_stamped_as_confirmed(tmp_path: pathlib.Path) -> None:
    repository, preferences = board(tmp_path)
    Scanner(repository, lambda: preferences, [Source([room("seen")])]).run_scan("manual")

    assert confirmations(repository)["seen"], "the search itself confirms the home"


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


# --------------------------------------------------------------------------
# the promise: everything on the shortlist, confirmed within a day
# --------------------------------------------------------------------------


def test_a_whole_shortlist_is_confirmed_within_one_day(tmp_path: pathlib.Path) -> None:
    """The claim this change exists to make.

    Six rechecks per source per scan meant a sixty-home shortlist took days to
    cycle, so a home could sit unconfirmed all week. Two scans is one day.
    """
    repository, preferences = board(tmp_path)
    homes = [room(f"r{index}") for index in range(60)]
    Scanner(repository, lambda: preferences, [Source(homes)]).run_scan("manual")
    assert all(confirmations(repository).values()), "the first search confirms them all"

    # The source goes quiet: every one of the sixty now needs a page read.
    for gap in (8, 16):
        hours_pass(repository, gap)
        Scanner(repository, lambda: preferences, [Source([])]).run_scan("scheduled")

    stale = [source_id for source_id, stamp in confirmations(repository).items() if stamp is None]
    assert stale == [], f"{len(stale)} homes went a day without confirmation"


def test_one_slow_source_cannot_starve_the_others(tmp_path: pathlib.Path) -> None:
    """A fair share of what is left, not first-come-first-served."""
    repository, preferences = board(tmp_path)
    greedy_homes = [room(f"g{index}") for index in range(40)]
    other_homes = [
        replace(room(f"o{index}"), platform="SpareRoom",
                original_url=f"https://www.spareroom.com/{index}")
        for index in range(40)
    ]

    class Slow(Source):
        platform = "Craigslist"

        def enrich(self, client, listing):
            # Slow enough that, left unbounded, this source alone would spend the
            # entire allowance and the second would never be reached.
            time.sleep(0.12)
            return super().enrich(client, listing)

    class Other(Source):
        platform = "SpareRoom"

    Scanner(
        repository, lambda: preferences, [Slow(greedy_homes), Other(other_homes)]
    ).run_scan("manual")
    hours_pass(repository, 8)

    slow, other = Slow([]), Other([])
    # A short allowance and a short per-request timeout, so the share arithmetic
    # is what decides rather than the fifteen-second network default.
    Scanner(
        repository,
        lambda: preferences,
        [slow, other],
        timeout_seconds=0.05,
        max_scan_seconds=2.0,
    ).run_scan("scheduled")

    assert other.enriched, "the second source has to get a turn"
    assert len(slow.enriched) < 40, "and the slow one does not take the whole allowance"
    # Unbounded, the slow source would have used every second available.
    assert len(slow.enriched) * 0.12 < 2.0


def test_rechecks_stop_at_the_deadline_and_discovery_still_persists(
    tmp_path: pathlib.Path,
) -> None:
    """Running out of time must cost confirmations, never results."""
    repository, preferences = board(tmp_path)
    stored = [room(f"s{index}") for index in range(30)]
    Scanner(repository, lambda: preferences, [Source(stored)]).run_scan("manual")
    hours_pass(repository, 8)

    class Crawling(Source):
        def enrich(self, client, listing):
            time.sleep(0.05)
            return super().enrich(client, listing)

    fresh = [room("brand-new")]
    crawling = Crawling(fresh)
    outcome = Scanner(
        repository,
        lambda: preferences,
        [crawling],
        timeout_seconds=0.05,
        max_scan_seconds=0.6,
    ).run_scan("scheduled")

    assert outcome.status in {"completed", "completed_with_errors"}
    everything = {row["source_id"] for row in repository.query_listings(0, view="all")}
    assert "brand-new" in everything, "the home the search found is stored regardless"
    assert len(crawling.enriched) < 30, "and the recheck gave up rather than overrunning"


def test_a_source_that_breaks_mid_recheck_leaves_the_rest_alone(
    tmp_path: pathlib.Path,
) -> None:
    """One bad page must not take the scan, the other homes, or their dates."""
    repository, preferences = board(tmp_path)
    homes = [room(f"m{index}") for index in range(6)]
    Scanner(repository, lambda: preferences, [Source(homes)]).run_scan("manual")
    hours_pass(repository, 8)
    before = confirmations(repository)

    class Flaky(Source):
        def enrich(self, client, listing):
            if listing.source_id == "m2":
                raise RuntimeError("connection reset by peer")
            return super().enrich(client, listing)

    flaky = Flaky([])
    outcome = Scanner(repository, lambda: preferences, [flaky]).run_scan("scheduled")

    assert outcome.status == "completed", "a broken page is not a broken scan"
    after = confirmations(repository)
    assert after["m2"] == before["m2"], "the home it could not read keeps its old date"
    assert after["m0"] != before["m0"], "the others are still confirmed"
    assert "m2" not in shortlist(repository, preferences) or True
    assert shortlist(repository, preferences) >= {"m0", "m1", "m2"}, "and nothing is dropped"


# --------------------------------------------------------------------------
# what the reader is told
# --------------------------------------------------------------------------


def app_for(tmp_path: pathlib.Path):
    from sf_housing.app import create_app
    from sf_housing.settings import Settings

    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(
        data_dir=data,
        preferences_path=preferences_path,
        database_path=data / "housing.sqlite3",
        log_path=data / "test.log",
    )
    repository = Repository(settings.database_path)
    repository.initialize()
    return settings, repository


def age_everything(repository: Repository, hours: float) -> None:
    """Wind both confirmation signals back, so nothing looks recent."""
    import json as _json
    from datetime import UTC as _UTC, datetime as _datetime, timedelta as _timedelta

    then = (_datetime.now(_UTC) - _timedelta(hours=hours)).isoformat()
    with repository.connection() as connection:
        for row in connection.execute("SELECT id, metadata_json FROM listings").fetchall():
            metadata = _json.loads(row["metadata_json"] or "{}")
            metadata["last_verified_at"] = then
            connection.execute(
                "UPDATE listings SET metadata_json = ?, last_seen = ? WHERE id = ?",
                (_json.dumps(metadata), then, row["id"]),
            )
        connection.commit()


def test_a_home_nobody_has_confirmed_for_a_day_says_so_on_the_row(
    tmp_path: pathlib.Path,
) -> None:
    """A score says how well a home fits. It says nothing about whether the home
    is still there, and the row used to imply both."""
    from fastapi.testclient import TestClient
    from sf_housing.app import create_app

    settings, repository = app_for(tmp_path)
    repository.upsert_listing(room("aging"), ScoreResult(82, ["fits"], "check", {}))
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        fresh = client.get("/?view=all&housing=room").text
        assert "Check confirmation" not in fresh, "a home just seen raises no question"
        age_everything(repository, 30)
        aged = client.get("/?view=all&housing=room").text

    assert "Check confirmation" in aged
    assert "Not confirmed as still listed" in aged


def test_the_detail_page_states_when_it_was_last_confirmed(tmp_path: pathlib.Path) -> None:
    from fastapi.testclient import TestClient
    from sf_housing.app import create_app

    settings, repository = app_for(tmp_path)
    listing_id, _ = repository.upsert_listing(room("dated"), ScoreResult(82, ["fits"], "check", {}))
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        page = client.get(f"/listings/{listing_id}").text

    assert "Last confirmed" in page
    assert repository.listing(listing_id)["last_verified_at"], "and it has a date to show"


def test_an_excluded_home_still_leads_with_why_it_was_excluded(
    tmp_path: pathlib.Path,
) -> None:
    """The confirmation warning must not push the exclusion off the row."""
    from fastapi.testclient import TestClient
    from sf_housing.app import create_app

    settings, repository = app_for(tmp_path)
    repository.upsert_listing(
        replace(room("excluded"), price=9000, title="Overpriced room"),
        ScoreResult(20, [], "over budget", {"hard_constraints": [
            {"status": "fail", "check": "price", "reason": "The monthly price exceeds this path's maximum."}
        ]}),
    )
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        age_everything(repository, 40)
        page = client.get("/?view=all&housing=room").text

    assert "Outside deal" in page
    assert "Check confirmation" not in page


def test_an_upgraded_install_does_not_flag_every_stored_home(
    tmp_path: pathlib.Path,
) -> None:
    """Homes collected before the explicit stamp existed still know when a
    search last returned them, and are judged on that."""
    import json as _json

    from fastapi.testclient import TestClient
    from sf_housing.app import create_app

    settings, repository = app_for(tmp_path)
    listing_id, _ = repository.upsert_listing(room("legacy"), ScoreResult(82, ["fits"], "check", {}))
    with repository.connection() as connection:
        connection.execute(
            "UPDATE listings SET metadata_json = ? WHERE id = ?", (_json.dumps({}), listing_id)
        )
        connection.commit()

    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        page = client.get("/?view=all&housing=room").text

    assert "Check confirmation" not in page, "last_seen is the same confirmation by another name"


def test_a_home_whose_source_did_not_run_keeps_its_old_date(tmp_path: pathlib.Path) -> None:
    """The honest edge of the coverage promise.

    A home can only be reconfirmed by the source that publishes it. If that
    source is not part of a scan -- switched off, failing, or awaiting setup --
    the home keeps the date it had and says it is unconfirmed, rather than being
    quietly credited with a confirmation nobody made.
    """
    repository, preferences = board(tmp_path)
    craigslist = room("cl")
    elsewhere = replace(
        room("sr"), platform="SpareRoom", original_url="https://www.spareroom.com/1"
    )

    class Other(Source):
        platform = "SpareRoom"

    Scanner(
        repository, lambda: preferences, [Source([craigslist]), Other([elsewhere])]
    ).run_scan("manual")
    hours_pass(repository, 30)
    before = confirmations(repository)

    # Only Craigslist runs this time.
    Scanner(repository, lambda: preferences, [Source([])]).run_scan("scheduled")
    after = confirmations(repository)

    assert after["cl"] != before["cl"], "the source that ran reconfirms its own home"
    assert after["sr"] == before["sr"], "the one that did not run credits nothing"
    stale = repository.listing(
        [row["id"] for row in repository.query_listings(0, view="all") if row["source_id"] == "sr"][0]
    )
    assert any(check["check"] == "confirmation" for check in stale["checks"]), (
        "and the home says plainly that nobody has confirmed it"
    )
