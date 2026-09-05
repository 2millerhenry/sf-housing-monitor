"""A home worth acting on had nowhere to be read.

The results table truncates by design, and the only link out of a row went
straight to the source, so deciding about a listing meant leaving for the site
that published it. The text the source actually wrote was never shown at all:
the scanner collects it, the score is partly built from it, and no page
displayed a word of it.

A listing now has its own page. It is the starred card rather than a second
layout, because a starred home and a home you are reading about are the same
job, and two layouts drift apart.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from sf_housing.app import _safe_return, create_app
from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

DESCRIPTION = (
    "Family home available for sublet in sunny Bernal Heights while we are "
    "travelling. Cats welcome. Street parking is easy."
)


def build(tmp_path: pathlib.Path, **overrides):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(
        data_dir=data_dir,
        preferences_path=preferences_path,
        database_path=data_dir / "housing.sqlite3",
        log_path=data_dir / "test.log",
    )
    repository = Repository(settings.database_path)
    repository.initialize()
    fields = dict(
        platform="Craigslist",
        source_id="one",
        title="Sunny private room in NOPA",
        original_url="https://sfbay.craigslist.org/roo/d/x/1.html",
        price=1500,
        neighborhood="NOPA",
        listing_type="Room/share",
        summary=DESCRIPTION,
    )
    fields.update(overrides)
    listing_id, _ = repository.upsert_listing(
        ListingCandidate(**fields),
        ScoreResult(82, ["A good fit", "Second reason", "Third reason", "Fourth reason"], "Check the lease.", {}),
    )
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    return application, repository, listing_id


def page_of(application, listing_id: int, query: str = "") -> str:
    with TestClient(application) as client:
        response = client.get(f"/listings/{listing_id}{query}")
    assert response.status_code == 200
    return response.text


# --------------------------------------------------------------------------
# the page exists
# --------------------------------------------------------------------------


def test_a_listing_has_a_page_of_its_own(tmp_path: pathlib.Path) -> None:
    application, _, listing_id = build(tmp_path)
    page = page_of(application, listing_id)

    assert "Sunny private room in NOPA" in page
    assert "<title>Sunny private room in NOPA · SF Housing Monitor</title>" in page


def test_a_listing_that_does_not_exist_is_a_clean_404(tmp_path: pathlib.Path) -> None:
    application, _, _ = build(tmp_path)
    with TestClient(application) as client:
        assert client.get("/listings/999999").status_code == 404
        assert client.get("/listings/0").status_code == 404
        assert client.get("/listings/-3").status_code == 404
        assert client.get("/listings/abc").status_code == 422


def test_the_detail_route_does_not_swallow_the_outbound_link(tmp_path: pathlib.Path) -> None:
    """"/listings/{id}" and "/listings/{id}/open" are different routes and the
    new one is registered first."""
    application, _, listing_id = build(tmp_path)
    with TestClient(application) as client:
        redirect = client.get(f"/listings/{listing_id}/open", follow_redirects=False)

    assert redirect.status_code == 307
    assert redirect.headers["location"] == "https://sfbay.craigslist.org/roo/d/x/1.html"


# --------------------------------------------------------------------------
# what the page adds
# --------------------------------------------------------------------------


def test_the_page_shows_what_the_source_actually_wrote(tmp_path: pathlib.Path) -> None:
    """The point of the page. This text was collected and scored against but
    never displayed anywhere in the app."""
    application, _, listing_id = build(tmp_path)
    page = page_of(application, listing_id)

    assert DESCRIPTION in page
    assert "What the listing says" in page


def test_a_listing_with_no_description_says_so_rather_than_showing_a_blank(
    tmp_path: pathlib.Path,
) -> None:
    application, _, listing_id = build(tmp_path, summary="")
    page = page_of(application, listing_id)

    assert "published no description" in page


def test_the_page_does_not_present_a_stale_copy_as_the_authority(
    tmp_path: pathlib.Path,
) -> None:
    """The text is a snapshot from the last scan, and a renter deciding on it
    needs to know that."""
    application, _, listing_id = build(tmp_path)
    page = page_of(application, listing_id)

    assert "source page is the only authority" in page


def test_the_page_shows_every_reason_the_score_was_built_from(
    tmp_path: pathlib.Path,
) -> None:
    """The list caps at three because it holds several homes; a page devoted to
    one does not. The scorer happens to publish three today, so this asserts
    that all of them arrive rather than a count that would go stale."""
    application, repository, listing_id = build(tmp_path)
    page = page_of(application, listing_id)
    reasons = repository.listing(listing_id)["match_reasons"]

    assert reasons, "a scored listing has reasons"
    for reason in reasons:
        assert reason in page


def test_the_page_shows_every_open_question_the_score_recorded(
    tmp_path: pathlib.Path,
) -> None:
    application, repository, listing_id = build(tmp_path)
    page = page_of(application, listing_id)
    row = repository.listing(listing_id)

    for entry in row["blockers"] or row["checks"]:
        assert entry["reason"] in page


def test_a_fact_the_source_never_stated_is_named_as_missing(
    tmp_path: pathlib.Path,
) -> None:
    """An empty cell reads as an oversight; "Not stated" is the truth."""
    application, _, listing_id = build(tmp_path, price=None, neighborhood=None)
    page = page_of(application, listing_id)

    facts = re.search(r'<dl class="detail-facts">(.*?)</dl>', page, re.S)
    assert facts, "the fact table must render"
    assert facts.group(1).count("Not stated") >= 2


def test_the_page_says_where_the_open_button_will_take_you(tmp_path: pathlib.Path) -> None:
    application, _, listing_id = build(tmp_path)
    page = page_of(application, listing_id)

    assert "sfbay.craigslist.org/roo/d/x/1.html" in page


def test_reading_about_a_home_is_not_the_same_as_opening_it(
    tmp_path: pathlib.Path,
) -> None:
    """The unopened sort tracks whether the reader went to the source. Viewing
    the page must not quietly count as having done so."""
    application, repository, listing_id = build(tmp_path)
    page_of(application, listing_id)

    assert repository.listing(listing_id)["opened_at"] is None
    assert "You have not opened this listing yet" in page_of(application, listing_id)


# --------------------------------------------------------------------------
# one card, two places
# --------------------------------------------------------------------------


def test_the_starred_list_and_the_page_render_the_same_card(
    tmp_path: pathlib.Path,
) -> None:
    """Two layouts for one job drift apart; this is the guard against a second
    one growing back."""
    application, repository, listing_id = build(tmp_path)
    repository.set_listing_status(listing_id, "saved")
    with TestClient(application) as client:
        starred = client.get("/?view=saved").text
    page = page_of(application, listing_id)

    for marker in ('class="listing-card', "Why it fits", "Worth checking", "Copy contact note", "★ Starred"):
        assert marker in starred, marker
        assert marker in page, marker


def test_the_row_title_opens_the_page_and_the_button_still_leaves_for_the_source(
    tmp_path: pathlib.Path,
) -> None:
    """They used to be the same link twice."""
    application, _, listing_id = build(tmp_path)
    with TestClient(application) as client:
        dashboard = client.get("/?view=all").text

    assert f'href="/listings/{listing_id}?from=' in dashboard
    assert dashboard.count(f'href="/listings/{listing_id}/open"') == 1


def test_both_surfaces_load_the_shared_listing_behaviour(tmp_path: pathlib.Path) -> None:
    """Marking a listing opened and copying a contact note used to be an inline
    script in the results table, so a page showing one listing had the markup
    and none of the behaviour."""
    application, repository, listing_id = build(tmp_path)
    repository.set_listing_status(listing_id, "saved")
    with TestClient(application) as client:
        starred = client.get("/?view=saved").text
    page = page_of(application, listing_id)

    for markup in (starred, page):
        assert "listing-actions.js" in markup
    index_source = (REPO_ROOT / "sf_housing" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "data-copy-outreach" in index_source or "listing_card" in index_source
    assert "navigator.clipboard" not in index_source, "the behaviour belongs in the shared file"


# --------------------------------------------------------------------------
# acting on the home from its own page
# --------------------------------------------------------------------------


def test_saving_a_note_from_the_page_keeps_the_reader_on_the_page(
    tmp_path: pathlib.Path,
) -> None:
    application, repository, listing_id = build(tmp_path)
    own = f"/listings/{listing_id}"
    with TestClient(application) as client:
        response = client.post(
            f"/listings/{listing_id}/note",
            data={"note": "Called Tuesday, asked about the rate", "return_to": own},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == own
    assert repository.listing(listing_id)["note"] == "Called Tuesday, asked about the rate"
    assert "Called Tuesday, asked about the rate" in page_of(application, listing_id)


@pytest.mark.parametrize(
    "status,expected_label",
    [("saved", "★ Starred"), ("dismissed", "Restore"), ("active", "☆ Star")],
)
def test_the_page_offers_the_same_controls_the_table_does(
    tmp_path: pathlib.Path, status: str, expected_label: str
) -> None:
    application, repository, listing_id = build(tmp_path)
    repository.set_listing_status(listing_id, status)

    assert expected_label in page_of(application, listing_id)


def test_the_page_says_which_pile_the_home_is_in(tmp_path: pathlib.Path) -> None:
    application, repository, listing_id = build(tmp_path)
    repository.set_listing_status(listing_id, "dismissed")

    assert "· Passed" in page_of(application, listing_id)


# --------------------------------------------------------------------------
# getting back
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin,label",
    [
        ("/?view=saved", "Back to starred listings"),
        ("/?view=near_matches", "Back to near matches"),
        ("/?view=dismissed", "Back to passed listings"),
        ("/?view=all", "Back to the archive"),
        ("/?housing=studio", "Back to the shortlist"),
        ("", "Back to the shortlist"),
    ],
)
def test_the_back_link_names_where_the_reader_came_from(
    tmp_path: pathlib.Path, origin: str, label: str
) -> None:
    from urllib.parse import quote

    application, _, listing_id = build(tmp_path)
    query = f"?from={quote(origin, safe='')}" if origin else ""
    page = page_of(application, listing_id, query)

    assert label in page


def test_the_filters_the_reader_had_survive_the_round_trip(
    tmp_path: pathlib.Path,
) -> None:
    from urllib.parse import quote

    application, _, listing_id = build(tmp_path)
    origin = "/?sort=price&view=near_matches&housing=studio"
    page = page_of(application, listing_id, f"?from={quote(origin, safe='')}")

    assert f'href="{origin}"'.replace("&", "&amp;") in page


# --------------------------------------------------------------------------
# nothing on this page can send a reader off the machine
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "//evil.test",
        "/\\evil.test",
        "/\\\\evil.test",
        "https://evil.test",
        "http://evil.test",
        "javascript:alert(1)",
        "/\tevil.test",
        "/\revil.test",
        "/\nevil.test",
        "/\x00evil.test",
        "\\\\evil.test",
    ],
)
def test_a_hostile_origin_cannot_become_the_back_link(
    tmp_path: pathlib.Path, hostile: str
) -> None:
    """A browser normalises a backslash to a forward slash, so "/\\evil.test"
    became "//evil.test" and left the machine. The old guard only rejected a
    literal leading "//"."""
    from urllib.parse import quote

    application, _, listing_id = build(tmp_path)
    page = page_of(application, listing_id, f"?from={quote(hostile, safe='')}")
    back = re.search(r'<nav class="detail-back".*?href="([^"]+)"', page, re.S)

    assert back and back.group(1) == "/"


@pytest.mark.parametrize(
    "hostile",
    ["//evil.test", "/\\evil.test", "https://evil.test", "/\tevil", "/\revil", "/\x00evil", None, ""],
)
def test_no_form_on_any_page_can_redirect_off_the_machine(hostile) -> None:
    """The same guard protects every existing form post, not just this page."""
    assert _safe_return(hostile) == "/"


@pytest.mark.parametrize(
    "legitimate",
    ["/", "/?view=saved", "/listings/3", "/listings/3?from=%2F%3Fview%3Dsaved", "/preferences"],
)
def test_a_real_page_of_this_app_is_still_a_valid_destination(legitimate: str) -> None:
    assert _safe_return(legitimate) == legitimate


def test_a_hostile_return_to_on_a_real_post_lands_on_the_dashboard(
    tmp_path: pathlib.Path,
) -> None:
    application, _, listing_id = build(tmp_path)
    with TestClient(application) as client:
        response = client.post(
            f"/listings/{listing_id}/status",
            data={"status": "saved", "return_to": "/\\evil.test"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/"


# --------------------------------------------------------------------------
# text this app did not write
# --------------------------------------------------------------------------


def test_nothing_a_source_wrote_can_become_markup(tmp_path: pathlib.Path) -> None:
    """Every string on this page -- title, description, the destination URL --
    was scraped from a page nobody here controls."""
    application, _, listing_id = build(
        tmp_path,
        title="Room <script>alert(1)</script>",
        summary="A room. <img src=x onerror=alert(2)> <b>bold</b>",
        original_url='https://sfbay.craigslist.org/x/1.html"onmouseover="alert(3)',
    )
    page = page_of(application, listing_id)
    soup = BeautifulSoup(page, "html.parser")

    assert soup.find_all("img") == [], "no element the source wrote may exist"
    assert soup.find_all("b") == []
    handlers = [tag.name for tag in soup.find_all(True) if any(k.startswith("on") for k in tag.attrs)]
    assert handlers == [], f"an event handler reached the page on {handlers}"
    # Every script has to be one this app shipped, served from this app. A
    # names list rather than a count, so adding one of ours does not quietly
    # widen what the page will run.
    allowed = {"listing-actions.js", "donate-panel.js"}
    for tag in soup.find_all("script"):
        src = tag.get("src")
        assert src, "no inline script belongs on this page"
        assert src.startswith("http://testserver/static/"), src
        assert src.split("/")[-1].split("?")[0] in allowed, src
    assert all("alert" not in (tag.string or "") for tag in soup.find_all("script"))


def test_a_listing_with_almost_nothing_set_still_renders(tmp_path: pathlib.Path) -> None:
    """Sources fail in the middle of a page all the time."""
    application, _, listing_id = build(
        tmp_path, price=None, neighborhood=None, listing_type=None, summary=None, title="x"
    )

    assert "Everything known" in page_of(application, listing_id)
