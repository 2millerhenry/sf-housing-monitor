from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from datetime import UTC, datetime, timedelta

from sf_housing.app import (
    _compact_move_in_date,
    _compact_pacific_date,
    _is_recently_posted,
    _prepared_zillow_searches,
    _was_checked_today,
    _typical_scan_seconds,
    create_app,
)
from sf_housing.scheduling import PACIFIC
from sf_housing.database import Repository
from sf_housing.freshness import FRESHNESS_WINDOW
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.preferences import load_preferences
from sf_housing.scoring import score_listing
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES


class WaitingDashboardSource:
    platform = "Waiting source"
    mode = "automatic"
    search_url = "https://example.test/waiting"
    manual_reason = None
    detail_budget = 0

    def __init__(self, started: threading.Event, release: threading.Event):
        self.started = started
        self.release = release

    def search(self, client, preferences):
        self.started.set()
        assert self.release.wait(timeout=3)
        return []

    def enrich(self, client, listing):
        return listing


class EmptyFacebookSource:
    platform = "Facebook Marketplace"
    mode = "automatic"
    search_url = "https://example.test/facebook"
    manual_reason = None


class StaleDashboardSource:
    platform = "Craigslist"
    mode = "automatic"
    search_url = "https://example.test/craigslist"
    manual_reason = None


def app_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    return Settings(
        data_dir=data_dir,
        preferences_path=preferences_path,
        database_path=data_dir / "housing.sqlite3",
        log_path=data_dir / "test.log",
    )


def add_listing(
    repository: Repository,
    platform: str,
    source_id: str,
    title: str,
    price: int,
    neighborhood: str,
    metadata: dict | None = None,
):
    repository.upsert_listing(
        ListingCandidate(
            platform=platform,
            source_id=source_id,
            title=title,
            original_url=f"https://example.test/{source_id}",
            price=price,
            neighborhood=neighborhood,
            metadata=metadata or {},
        ),
        ScoreResult(80, ["Good fit"], "Unknown: lease.", {}),
    )


def test_dashboard_filters_by_platform_and_neighborhood(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "wanted", "Wanted Mission room", 1500, "Mission")
    add_listing(repository, "SpareRoom", "wrong-platform", "Other Mission room", 1400, "Mission")
    add_listing(repository, "Craigslist", "wrong-area", "Richmond room", 1300, "Inner Richmond")

    with TestClient(application) as client:
        response = client.get("/?platform=Craigslist&neighborhood=Mission&sort=price")

    assert response.status_code == 200
    assert "Wanted Mission room" in response.text
    assert "Other Mission room" not in response.text
    assert "Richmond room" not in response.text
    assert 'href="/?sort=score&amp;view=active&amp;neighborhood=Mission&amp;platform=Craigslist"' in response.text
    assert 'href="/?sort=newest&amp;view=active&amp;neighborhood=Mission&amp;platform=Craigslist"' in response.text


def test_dashboard_calls_out_a_stale_source_without_hiding_other_results(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    source = StaleDashboardSource()
    application = create_app(settings=settings, sources=[source], enable_scheduler=False)
    repository = application.state.repository
    run_id = repository.begin_scan("manual")
    source_run = repository.begin_source_run(
        run_id, source.platform, source.search_url, source_key="StaleDashboardSource"
    )
    repository.finish_source_run(source_run, "success", seen=1)
    repository.finish_scan(run_id, "completed")
    old = (datetime.now(UTC) - FRESHNESS_WINDOW - timedelta(minutes=1)).isoformat()
    with repository.connection() as connection:
        connection.execute("UPDATE source_runs SET finished_at = ? WHERE id = ?", (old, source_run))
        connection.commit()
    add_listing(repository, "Craigslist", "still-visible", "Still visible room", 1500, "Mission")

    with TestClient(application) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Still visible room" in response.text
    assert "1 source needs attention" in response.text
    assert "Craigslist is stale" in response.text
    assert "Use Check for new homes now" in response.text


def test_dashboard_keeps_each_listing_link_in_the_left_side_of_the_table(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "direct-link", "Direct link room", 1500, "Mission")

    with TestClient(application) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'class="cell-quick-open"' in response.text
    assert 'aria-label="Open Direct link room"' in response.text
    assert 'data-mark-opened="/listings/' in response.text
    listing_id = repository.query_listings(0, view="all")[0]["id"]
    # Two different destinations, not the same one twice: the button leaves for
    # the source, the title opens the home's own page.
    assert response.text.count(f'href="/listings/{listing_id}/open"') == 1
    assert f'href="/listings/{listing_id}?from=' in response.text


def test_dashboard_prioritizes_direct_application_and_direct_lister_routes(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(
        repository,
        "Listings Project",
        "direct-lister",
        "Direct lister home",
        2200,
        "Mission",
        metadata={"direct_lister": True},
    )
    add_listing(
        repository,
        "Abacus (small buildings)",
        "direct-apply",
        "Direct application home",
        2100,
        "Mission",
        metadata={"application_url": "https://abacus.appfolio.com/listings/rental_applications/new?one"},
    )

    with TestClient(application) as client:
        response = client.get("/?sort=contact")

    assert response.status_code == 200
    assert 'value="contact" selected' in response.text
    assert "Apply now for Direct application home" in response.text
    assert "Contact the lister for Direct lister home" in response.text
    assert response.text.index("Direct application home") < response.text.index("Direct lister home")
    assert "Copy contact note" in response.text


def test_dashboard_marks_a_listing_viewed_after_opening_it(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "opened", "Opened room", 1500, "Mission")
    listing_id = repository.query_listings(0, view="all")[0]["id"]

    with TestClient(application) as client:
        marked = client.post(f"/listings/{listing_id}/opened")
        response = client.get("/")

    assert marked.status_code == 204
    assert 'class="listing-row opened-listing"' in response.text


def test_dashboard_can_sort_unopened_listings_first(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "unopened", "Untouched room", 1500, "Mission")
    add_listing(repository, "Craigslist", "opened-first", "Opened room", 1600, "Mission")
    opened_id = next(
        listing["id"]
        for listing in repository.query_listings(0, view="all")
        if listing["title"] == "Opened room"
    )
    assert repository.mark_listing_opened(opened_id)

    with TestClient(application) as client:
        response = client.get("/?sort=unopened")

    assert response.status_code == 200
    assert 'value="unopened" selected' in response.text
    assert "Not opened yet first" in response.text
    assert response.text.index("Untouched room") < response.text.index("Opened room")


def test_dashboard_open_route_marks_listing_then_redirects_to_original_url(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "open-route", "Open route room", 1500, "Mission")
    listing_id = repository.query_listings(0, view="all")[0]["id"]

    with TestClient(application) as client:
        response = client.get(f"/listings/{listing_id}/open", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "https://example.test/open-route"
    assert repository.query_listings(0, view="all")[0]["opened_at"] is not None


def test_dashboard_source_filter_keeps_connected_sources_visible_without_matches(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(
        settings=settings,
        sources=[EmptyFacebookSource()],
        enable_scheduler=False,
    )

    with TestClient(application) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'value="Facebook Marketplace"' in response.text


def test_dashboard_exposes_home_filter_and_move_in_sort(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    application.state.repository.upsert_listing(
        ListingCandidate(
            platform="Furnished Finder",
            source_id="house",
            title="Private room in a house",
            original_url="https://example.test/house",
            listing_type="Private room · House",
        ),
        ScoreResult(
            80,
            ["Good fit"],
            "Unknown: lease.",
            {
                "availability": {"available_on": "2026-08-01"},
                "home_facts": {"primary": "Private room · House", "secondary": ["Private bath"]},
            },
        ),
    )

    with TestClient(application) as client:
        response = client.get("/?platform=Furnished+Finder&home_style=house&sort=available")

    assert response.status_code == 200
    assert "Soonest move-in" in response.text
    assert 'name="home_style"' in response.text
    assert 'value="house" selected' in response.text
    assert "Home &amp; room" in response.text
    assert "Move-in" in response.text


def test_dashboard_uses_the_whole_unit_budget_in_its_labels(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.get("/?housing=whole_unit")

    assert response.status_code == 200
    assert "up to $3,000" in response.text
    assert "Entire places up to $3,000" in response.text


def test_dashboard_highlights_applied_filters_and_normalizes_an_invalid_order(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "filtered", "Filtered room", 1500, "Mission")

    with TestClient(application) as client:
        filtered = client.get("/?platform=Craigslist&sort=newest")
        invalid = client.get("/?sort=not-a-real-order")

    assert filtered.status_code == 200
    assert 'name="platform" class="is-selected"' in filtered.text
    assert 'name="sort" data-auto-submit-order class="is-selected"' in filtered.text
    assert 'th scope="col" class="cell-found" aria-sort="descending"' in filtered.text
    assert "Update filters" in filtered.text
    assert invalid.status_code == 200
    assert 'value="score" selected' in invalid.text


def test_dashboard_explains_empty_connected_source_filter(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(
        settings=settings,
        sources=[EmptyFacebookSource()],
        enable_scheduler=False,
    )

    with TestClient(application) as client:
        response = client.get("/?platform=Facebook+Marketplace")

    assert "No Facebook Marketplace matches yet" in response.text
    assert "View every imported Facebook Marketplace listing" in response.text


def test_compact_listing_dates_are_easy_to_scan() -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=PACIFIC)

    assert _compact_pacific_date("2026-07-20T08:00:00-07:00", now=now) == "Today"
    assert _compact_pacific_date("2026-07-19T08:00:00-07:00", now=now) == "Yesterday"
    assert _compact_pacific_date("2026-06-03T08:00:00-07:00", now=now) == "Jun 3"
    assert _compact_move_in_date("2026-08-01") == "Aug 1, 2026"
    assert _was_checked_today("2026-07-20T08:00:00-07:00", now=now)
    assert not _was_checked_today("2026-07-19T23:59:00-07:00", now=now)


def test_recent_posting_indicator_uses_source_posted_time_not_first_discovery() -> None:
    now = datetime(2026, 7, 20, 19, 0, tzinfo=UTC)

    assert _is_recently_posted(
        {"metadata": {"listing_timestamp": (now - timedelta(hours=47, minutes=59)).isoformat()}}, now=now
    )
    assert _is_recently_posted({"metadata": {"listing_timestamp": (now - timedelta(days=2)).isoformat()}}, now=now)
    assert not _is_recently_posted({"metadata": {"listing_timestamp": (now - timedelta(days=2, seconds=1)).isoformat()}}, now=now)
    assert _is_recently_posted({"metadata": {}, "summary": "Listed yesterday"}, now=now)
    assert not _is_recently_posted({"metadata": {}, "summary": "Listed 3 days ago"}, now=now)
    assert not _is_recently_posted({"metadata": {}, "summary": "No date given"}, now=now)


def test_dashboard_marks_recent_listings_as_new(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    add_listing(
        application.state.repository,
        "Craigslist",
        "fresh",
        "Fresh Mission room",
        1500,
        "Mission",
        metadata={"listing_timestamp": datetime.now(UTC).isoformat()},
    )

    with TestClient(application) as client:
        response = client.get("/")

    assert 'class="listing-row new-listing"' in response.text
    assert 'class="new-badge" title="Posted in the last two days">New</b>' in response.text


def test_typical_scan_time_uses_completed_runs_of_same_kind() -> None:
    scans = [
        {"trigger": "manual", "status": "completed", "started_at": "2026-07-20T12:00:00+00:00", "finished_at": "2026-07-20T12:00:03+00:00"},
        {"trigger": "manual", "status": "completed_with_errors", "started_at": "2026-07-20T12:01:00+00:00", "finished_at": "2026-07-20T12:01:05+00:00"},
        {"trigger": "scheduled", "status": "completed", "started_at": "2026-07-20T12:02:00+00:00", "finished_at": "2026-07-20T12:03:40+00:00"},
    ]

    assert _typical_scan_seconds(scans, "manual") == 4


def test_dashboard_shows_live_scan_progress_and_status_endpoint(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    started, release = threading.Event(), threading.Event()
    source = WaitingDashboardSource(started, release)
    application = create_app(settings=settings, sources=[source], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.post("/scan", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/?scan=starting"
        assert started.wait(timeout=2)

        page = client.get("/")
        active = client.get("/scan/status").json()
        assert "data-scan-progress" in page.text
        assert "scan-progress.js" in page.text
        assert active["running"] is True
        assert active["current_source"] == "Waiting source"
        assert active["sources_total"] == 1

        release.set()
        deadline = time.monotonic() + 3
        while client.get("/scan/status").json()["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        finished = client.get("/scan/status").json()

    assert finished["running"] is False
    assert finished["status"] == "completed"
    assert finished["percent"] == 100


def test_dashboard_keeps_short_completion_feedback_after_an_instant_scan(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.post("/scan", follow_redirects=False)
        page = client.get(response.headers["location"])

    assert response.status_code == 303
    assert response.headers["location"] == "/?scan=starting"
    assert "data-scan-progress" in page.text
    assert "scan-progress.js" in page.text


def test_dashboard_defaults_to_best_match_order(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    repository.upsert_listing(
        ListingCandidate("Test", "possible", "Possible home", "https://example.test/possible", 1500, "Mission"),
        ScoreResult(62, ["Possible"], "Unknown: lease.", {}),
    )
    repository.upsert_listing(
        ListingCandidate("Test", "strong", "Strong home", "https://example.test/strong", 1500, "Mission"),
        ScoreResult(88, ["Strong"], "Unknown: lease.", {}),
    )

    with TestClient(application) as client:
        response = client.get("/")

    assert response.text.index("Strong home") < response.text.index("Possible home")
    assert 'option value="score" selected' in response.text


def test_dashboard_review_actions_persist(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "one", "Room to save", 1500, "Mission")
    listing_id = repository.query_listings(0, view="all")[0]["id"]

    with TestClient(application) as client:
        initial = client.get("/")
        saved = client.post(
            f"/listings/{listing_id}/status",
            data={"status": "saved", "return_to": "/"},
            follow_redirects=False,
        )
        noted = client.post(
            f"/listings/{listing_id}/note",
            data={"note": "Message tonight", "return_to": "/"},
            follow_redirects=False,
        )

    assert saved.status_code == 303
    assert noted.status_code == 303
    assert "☆ Star" in initial.text
    stored = repository.query_listings(0, view="saved")[0]
    assert stored["note"] == "Message tonight"

    with TestClient(application) as client:
        starred = client.get("/?view=saved")

    assert starred.status_code == 200
    assert "Starred listings" in starred.text
    # Saved is a comparison view, not the scanning table: the note you wrote is
    # visible without opening anything, and the star can be undone from here.
    assert "Message tonight" in starred.text
    # The same control the table uses, so the star reads the same either place.
    assert 'aria-label="Remove from starred listings"' in starred.text
    assert "★ Starred" in starred.text
    assert "Why it fits" in starred.text
    assert "Room to save" in starred.text


def test_dashboard_keeps_whole_units_separate_and_labels_the_unit_type(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    preferences = load_preferences(settings.preferences_path)
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="studio-separate",
        title="Studio apartment in Mission",
        original_url="https://example.test/studio-separate",
        price=2400,
        neighborhood="Mission",
        summary="Studio apartment in a 12-unit building.",
    )
    repository.upsert_listing(listing, score_listing(listing, preferences))

    with TestClient(application) as client:
        room_page = client.get("/")
        unit_page = client.get("/?housing=whole_unit")

    assert "Studio apartment in Mission" not in room_page.text
    assert "Studios worth a look" in unit_page.text
    assert "Studio apartment in Mission" in unit_page.text
    assert ">Studio<" in unit_page.text
    assert "12-unit building" in unit_page.text
    # The tab is the size now, so a separate size dropdown would repeat it.
    assert 'name="unit_type"' not in unit_page.text


def test_each_split_size_has_its_own_tab_with_total_and_per_person_price(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    preferences = load_preferences(settings.preferences_path)
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="two-bedroom-separate",
        title="2 bedroom apartment in NOPA",
        original_url="https://example.test/two-bedroom-separate",
        price=5200,
        neighborhood="NOPA",
        summary="Entire 2 bedroom apartment in a 14-unit building.",
    )
    three_bedroom_listing = ListingCandidate(
        platform="Craigslist",
        source_id="three-bedroom-separate",
        title="3 bedroom apartment in NOPA",
        original_url="https://example.test/three-bedroom-separate",
        price=7350,
        neighborhood="NOPA",
        summary="Entire 3 bedroom apartment in a 9-unit building.",
    )
    repository.upsert_listing(listing, score_listing(listing, preferences))
    repository.upsert_listing(
        three_bedroom_listing, score_listing(three_bedroom_listing, preferences)
    )

    with TestClient(application) as client:
        room_page = client.get("/")
        small_unit_page = client.get("/?housing=whole_unit")
        two_bedroom_page = client.get("/?housing=two_bedroom")
        only_two_bedrooms = client.get("/?housing=two_bedroom")
        only_three_bedrooms = client.get("/?housing=three_bedroom")

    assert "2 bedroom apartment in NOPA" not in room_page.text
    assert "2 bedroom apartment in NOPA" not in small_unit_page.text
    assert "2 bedrooms worth a look" in two_bedroom_page.text
    assert "2 bedroom apartment in NOPA" in two_bedroom_page.text
    assert "$5,200" in two_bedroom_page.text
    assert "$2,600/person" in two_bedroom_page.text
    assert "14-unit building" in two_bedroom_page.text
    # Each size has its own tab instead of hiding inside a combined dropdown.
    assert 'housing=three_bedroom' in two_bedroom_page.text
    # Each size keeps to its own tab, so a three-bedroom never pads the
    # two-bedroom results the way the combined view used to.
    assert "3 bedroom apartment in NOPA" not in two_bedroom_page.text
    assert "$2,450/person" in only_three_bedrooms.text
    assert "3 bedroom apartment in NOPA" not in only_two_bedrooms.text
    assert "2 bedroom apartment in NOPA" in only_two_bedrooms.text
    assert "3 bedroom apartment in NOPA" in only_three_bedrooms.text
    assert "2 bedroom apartment in NOPA" not in only_three_bedrooms.text


def test_two_bedroom_priority_filter_distinguishes_strong_and_secondary_areas(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    preferences = load_preferences(settings.preferences_path)
    strong = ListingCandidate(
        platform="Craigslist",
        source_id="strong-two-bed",
        title="2 bedroom apartment in NOPA",
        original_url="https://example.test/strong-two-bed",
        price=4000,
        neighborhood="NOPA",
        summary="Entire 2-bedroom apartment in a 4-unit building.",
    )
    secondary = ListingCandidate(
        platform="Craigslist",
        source_id="secondary-two-bed",
        title="2 bedroom apartment in Bernal Heights",
        original_url="https://example.test/secondary-two-bed",
        price=4000,
        neighborhood="Bernal Heights",
        summary="Entire 2-bedroom apartment in a 4-unit building.",
    )
    repository.upsert_listing(strong, score_listing(strong, preferences))
    repository.upsert_listing(secondary, score_listing(secondary, preferences))

    with TestClient(application) as client:
        all_targets = client.get("/?housing=two_bedroom")
        priority_only = client.get("/?housing=two_bedroom&area_priority=dream_strong")

    assert all_targets.status_code == 200
    assert "Area priority" in all_targets.text
    assert "Strong area" in all_targets.text
    assert "Secondary area" in all_targets.text
    assert "2 bedroom apartment in NOPA" in priority_only.text
    assert "2 bedroom apartment in Bernal Heights" not in priority_only.text
    assert "Dream and strong target areas only" in priority_only.text


def test_alert_setup_page_explains_local_connection(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.get("/alerts")

    assert response.status_code == 200
    assert "Six sources already work" in response.text
    assert "nothing on this page is required" in response.text
    assert "Before you can connect Gmail" in response.text
    assert "Google OAuth Web client JSON" in response.text
    # The promise, not the sentence: whatever the wording, the page has to say
    # the Facebook login is never involved.
    assert "Facebook login is never used or stored" in response.text
    # The prepared searches themselves, not the words wrapped around them: a
    # link built from this deal's own areas, and none built from areas it does
    # not want.
    assert "zillow.com/nopa-san-francisco-ca/rentals/" in response.text
    assert "Mission" in response.text
    assert "Potrero Hill" not in response.text
    assert "3-bed</a>" in response.text, "the split searches are still offered"
    # Facebook has its own block further down and is deliberately not offered
    # here as a second, email-shaped way to reach the same listings.
    assert "(3BR)" not in response.text


def test_alerts_explain_why_a_new_user_returns_to_the_deal(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir,
        preferences_path=data_dir / "config" / "preferences.yaml",
        database_path=data_dir / "housing.sqlite3",
        log_path=data_dir / "test.log",
    )
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.get("/alerts", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/preferences?welcome=1&message=Finish+your+deal+first")


def test_alerts_uses_user_saved_zillow_urls(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    settings.preferences_path.write_text(
        TEST_PREFERENCES
        + """
zillow_searches:
  - name: Exact Noe Valley room search
    url: https://www.zillow.com/noe-valley-san-francisco-ca/rentals/?saved=yes
""",
        encoding="utf-8",
    )
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.get("/alerts")

    assert "Exact Noe Valley room search" in response.text
    assert "saved=yes" in response.text


def test_prepared_zillow_room_searches_follow_the_current_room_budget(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    settings.preferences_path.write_text(
        TEST_PREFERENCES
        + """
zillow_searches:
  - name: Budget-aware room search
    url: https://www.zillow.com/noe-valley-san-francisco-ca/rentals/?searchQueryState=%7B%22filterState%22%3A%7B%22mp%22%3A%7B%22min%22%3A800%2C%22max%22%3A2400%7D%7D%7D
""",
        encoding="utf-8",
    )
    preferences = load_preferences(settings.preferences_path)

    prepared = _prepared_zillow_searches(preferences)

    assert len(prepared) == 1
    assert "%22max%22%3A2000" in prepared[0]["url"]


def test_apify_token_can_be_connected_from_alerts(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        saved = client.post(
            "/alerts/apify/token",
            data={"apify_token": "apify_api_abcdefghijklmnopqrstuvwxyz123456"},
            follow_redirects=False,
        )
        page = client.get("/alerts")

    assert saved.status_code == 303
    assert "called Working only after a real bounded check succeeds" in page.text, (
        "a saved token is not a working connection, and the page has to say so"
    )
    assert 'name="apify_token"' in page.text
    assert "Replace the token" in page.text, "and offers a way to change it"


def test_plain_language_deal_form_updates_profile_and_preserves_advanced_settings(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.post(
            "/preferences/profile",
            data={
                "minimum_score": 65,
                "min_monthly": 800,
                "sweet_spot_min": 1200,
                "ideal_monthly": 1500,
                "sweet_spot_max": 1800,
                "max_monthly": 2300,
                "ideal_neighborhoods": "Duboce Triangle, Potrero Hill",
                "preferred_neighborhoods": "Mission Dolores, Bernal Heights",
                "acceptable_neighborhoods": "NOPA, Lower Haight",
                "ideal_min_months": 3,
                "ideal_max_months": 8,
                "max_months": 12,
                "ideal_people": 3,
                "max_people": 5,
            },
            follow_redirects=False,
        )

    saved = load_preferences(settings.preferences_path)
    assert response.status_code == 303
    assert saved.minimum_score == 65
    assert saved.list_value("ideal_neighborhoods") == ["Duboce Triangle", "Potrero Hill"]
    assert saved.section("sources")["max_results_per_source"] == 120
    assert saved.section("two_bedroom") == {
        "enabled": True,
        "occupants": 2,
        "max_per_person": 2700,
        "max_building_units": 50,
    }
    assert saved.section("three_bedroom") == {
        "enabled": True,
        "occupants": 3,
        "max_per_person": 2500,
        "max_building_units": 50,
    }


# --------------------------------------------------------------------------
# an empty shortlist has to say why it is empty
# --------------------------------------------------------------------------


def narrow_profile() -> str:
    """One area, a high bar: a deal that collects plenty and shortlists none."""
    import yaml

    return yaml.safe_dump(
        {
            "profile_version": 1,
            "profile": {
                "state": "active",
                "enabled_paths": ["private_room"],
                "budgets": {"private_room": {"maximum_monthly": 1200}},
                "geography": {"anywhere_in_sf": False, "dream": ["Sea Cliff"]},
                "room_household": {"private_room_required": True},
            },
            "technical": {"minimum_score": 85},
        }
    )


def seed_rooms(repository: Repository, count: int = 6) -> None:
    for index in range(count):
        repository.upsert_listing(
            ListingCandidate(
                platform="Craigslist",
                source_id=f"x{index}",
                title=f"Private room {index}",
                original_url=f"https://sfbay.craigslist.org/roo/d/x/{index}.html",
                price=2600 + index,
                neighborhood="Tenderloin",
                listing_type="Room/share",
                summary="A private room in a shared home.",
            ),
            ScoreResult(70, ["fits"], "check", {}),
        )


def test_an_empty_shortlist_names_what_held_the_homes_back(tmp_path: Path) -> None:
    """An empty list with no explanation reads as a broken search. It almost
    never is: the deal is narrow, and the page should say which limit cost what.
    """
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(narrow_profile(), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    seed_rooms(application.state.repository)

    with TestClient(application) as client:
        page = client.get("/?housing=room").text

    from sf_housing.models import CHECK_EXCLUSION_PHRASES

    assert "No homes made the shortlist yet" in page
    assert "Of 6 homes collected for this tab" in page
    known = set(CHECK_EXCLUSION_PHRASES.values()) | {"below your 85 match cut-off"}
    assert any(phrase in page for phrase in known), "the reason has to be one it can name"
    assert 'href="/preferences"' in page, "and offers the way out"
    assert "view=near_matches" in page, "and the homes themselves are still reachable"


def test_the_reasons_are_counted_not_guessed(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(narrow_profile(), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    seed_rooms(repository, count=7)

    with TestClient(application) as client:
        client.get("/?housing=room")
        summary = repository.exclusion_summary(85, "room", ())

    assert summary, "seven collected homes have to be accounted for"
    assert sum(item["count"] for item in summary) == 7, "every held-back home is counted once"


def test_a_healthy_shortlist_pays_nothing_for_the_explanation(tmp_path: Path) -> None:
    """The breakdown is only computed when the list is thin, and never shown
    when there is a real shortlist to read."""
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    for index in range(6):
        add_listing(repository, "Craigslist", f"good{index}", f"Sunny room {index}", 1500, "NOPA")

    with TestClient(application) as client:
        page = client.get("/").text

    assert "collected for this tab" not in page


def test_nothing_collected_at_all_does_not_pretend_to_explain(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(narrow_profile(), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/?housing=room")

    assert page.status_code == 200
    assert "collected for this tab" not in page.text


# --------------------------------------------------------------------------
# "there should be more listings than this"
# --------------------------------------------------------------------------


def test_a_short_shortlist_says_what_the_deal_held_back(tmp_path: Path) -> None:
    """A list far shorter than what was collected reads as a broken scraper.
    It almost never is, and the page had no way of saying which limit cost what.
    """
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(narrow_profile(), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    # One home that fits, and plenty that the deal holds back.
    repository.upsert_listing(
        ListingCandidate(
            platform="Craigslist", source_id="fits", title="Private room in Sea Cliff",
            original_url="https://sfbay.craigslist.org/roo/d/x/fits.html", price=1100,
            neighborhood="Sea Cliff", listing_type="Room/share",
            summary="A private room in a shared home, available now.",
        ),
        ScoreResult(92, ["fits"], "", {}),
    )
    seed_rooms(repository, count=9)

    with TestClient(application) as client:
        page = client.get("/?housing=room").text

    assert "listing-row" in page, "this shortlist is not empty"
    assert "held back by your deal" in page, "and still explains what is missing"
    assert 'href="/preferences"' in page


def test_a_shortlist_holding_everything_collected_says_nothing(tmp_path: Path) -> None:
    """No exclusions, no explanation. The note is context, not decoration."""
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    for index in range(4):
        add_listing(application.state.repository, "Craigslist", f"g{index}", f"Room {index}", 1500, "NOPA")

    with TestClient(application) as client:
        page = client.get("/").text

    assert "held back by your deal" not in page


# --------------------------------------------------------------------------
# the Posted column
# --------------------------------------------------------------------------


def test_the_posted_column_shows_the_date_the_source_published(tmp_path: Path) -> None:
    """The column people scan for. It has to be the source's own date, not the
    day this app happened to notice the home."""
    import re

    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    repository.upsert_listing(
        ListingCandidate(
            platform="Craigslist",
            source_id="dated",
            title="Sunny room in NOPA",
            original_url="https://sfbay.craigslist.org/roo/d/x/dated.html",
            price=1500,
            neighborhood="NOPA",
            listing_type="Room/share",
            summary="A private room in a shared home.",
            metadata={"listing_timestamp": "2026-08-28T14:11:54-07:00"},
        ),
        ScoreResult(88, ["fits"], "", {}),
    )

    with TestClient(application) as client:
        page = client.get("/?view=all&housing=room").text

    assert '<th scope="col" class="cell-found"' in page, "the column exists"
    cell = re.search(r'<td class="cell-found">(.*?)</td>', page, re.S).group(1)
    assert "Aug 28" in cell, cell
    assert "Found" not in cell, "a real posted date is not a found date"


def test_a_home_whose_source_never_states_a_date_says_so(tmp_path: Path) -> None:
    """SpareRoom publishes no dates at all. Showing the day we noticed it as if
    it were the posting date would be inventing a fact."""
    import re

    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    add_listing(application.state.repository, "SpareRoom", "undated", "Room in NOPA", 1500, "NOPA")

    with TestClient(application) as client:
        page = client.get("/?view=all&housing=room").text

    cell = re.search(r'<td class="cell-found">(.*?)</td>', page, re.S).group(1)
    assert "Found" in cell, "say it is a found date, not a posted one"
    assert "does not publish a date" in cell, "and why"


def test_the_posted_column_survives_a_narrow_window(tmp_path: Path) -> None:
    """It used to be one of the first two columns hidden below 1560px, which is
    most laptops, so the date people were looking for was never on screen."""
    import re

    css = (Path("sf_housing/static/style.css")).read_text(encoding="utf-8")
    start = css.index("@media (max-width: 1560px)")
    narrow = css[start : css.index("\n}", start)]

    assert "cell-found" not in narrow, "the Posted column must not hide at laptop width"
    assert "cell-source" in narrow, "the source is already named on the listing line"


def test_the_score_cell_no_longer_repeats_the_check_column(tmp_path: Path) -> None:
    import re

    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(narrow_profile(), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    seed_rooms(application.state.repository, count=3)

    with TestClient(application) as client:
        page = client.get("/?view=all&housing=room").text

    score_cell = re.search(r'<td class="cell-score">(.*?)</td>', page, re.S).group(1)
    assert "verification-label" not in score_cell
    assert "Check " not in score_cell
    assert "% evidence" in score_cell, "what the cell does keep"


# --------------------------------------------------------------------------
# a configuration value nobody can parse must not take the page down
# --------------------------------------------------------------------------


def test_a_null_budget_setting_does_not_500_the_dashboard(tmp_path: Path) -> None:
    """Readers wrote int(section.get(key, default)), where the default only
    applies when the key is absent. A key present and null reached int() and
    raised, which on the dashboard is a 500 on the page the app opens to."""
    import yaml

    from tests.conftest import TEST_PREFERENCES

    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    document = yaml.safe_load(TEST_PREFERENCES)
    document.setdefault("budget", {})["min_monthly"] = None
    document["budget"]["max_monthly"] = None
    settings.preferences_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        for path in ("/", "/preferences", "/?housing=room", "/?view=near_matches"):
            assert client.get(path).status_code == 200, path


def test_an_unreadable_setting_falls_back_instead_of_raising() -> None:
    from sf_housing.preferences import setting_int

    assert setting_int(None, 800) == 800
    assert setting_int("", 800) == 800
    assert setting_int("nonsense", 800) == 800
    assert setting_int([], 800) == 800
    assert setting_int("1200", 800) == 1200
    assert setting_int(1200, 800) == 1200
    assert setting_int(1200.0, 800) == 1200


def test_the_nav_asks_for_an_action_not_a_noun(tmp_path: Path) -> None:
    """"Sources" reads as a status page, and people treated it as one. The tab
    exists so someone adds a source, so it says that."""
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/").text

    assert '>Add sources</a>' in page
    assert '>Sources</a>' not in page, "the bare noun is gone from the nav"


def test_the_page_it_opens_says_the_same_thing(tmp_path: Path) -> None:
    """A tab called one thing that opens a page called another is how people
    decide they are in the wrong place and leave."""
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/alerts").text

    assert "<title>Add sources" in page
    assert '<p class="eyebrow">Add sources</p>' in page
