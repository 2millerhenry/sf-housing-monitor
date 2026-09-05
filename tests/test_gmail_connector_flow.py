from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.gmail_alerts import AlertEmail, GmailAlertMailbox
from sf_housing.settings import Settings
from sf_housing.sources import ZillowAlertSource
from tests.conftest import TEST_PREFERENCES


class FixtureMailbox:
    is_connected = True

    def messages(self, query: str, max_results: int = 50):
        return [
            AlertEmail(
                "zillow-flow-1",
                "New listing for your NOPA search",
                (
                    '<a href="https://www.zillow.com/homedetails/123-NOPA-SF/123_zpid/">'
                    "Sunny studio in NOPA</a><p>$1,900 per month · studio apartment</p>"
                ),
                "",
            )
        ]


def settings_for(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    data.mkdir()
    preferences = data / "config" / "preferences.yaml"
    preferences.parent.mkdir()
    preferences.write_text(TEST_PREFERENCES, encoding="utf-8")
    return Settings(
        data_dir=data,
        preferences_path=preferences,
        database_path=data / "housing.sqlite3",
        log_path=data / "housing.log",
    )


def connect_mailbox_properties(monkeypatch) -> None:
    monkeypatch.setattr(GmailAlertMailbox, "is_connected", property(lambda self: True))
    monkeypatch.setattr(GmailAlertMailbox, "has_client_secret", property(lambda self: True))
    monkeypatch.setattr(GmailAlertMailbox, "client_configuration_error", property(lambda self: None))
    monkeypatch.setattr(GmailAlertMailbox, "client_kind", property(lambda self: "installed"))


def test_gmail_setup_page_has_one_primary_flow_and_provider_truth(tmp_path: Path) -> None:
    application = create_app(settings=settings_for(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/alerts")

    assert page.status_code == 200
    assert "Connect Gmail read-only" not in page.text
    assert "Owner setup required once" in page.text
    assert "Action needed" not in page.text
    assert "Read-only" in page.text
    assert "Narrow searches" in page.text
    assert "Stays local" in page.text
    for provider in ("Zillow", "HotPads", "Apartments.com", "Zumper", "Roomies", "Facebook Marketplace"):
        assert provider in page.text


def test_bounded_gmail_test_enters_normal_ranking_storage_and_provider_state(
    tmp_path: Path, monkeypatch
) -> None:
    connect_mailbox_properties(monkeypatch)
    settings = settings_for(tmp_path)
    application = create_app(
        settings=settings,
        sources=[ZillowAlertSource(FixtureMailbox())],
        enable_scheduler=False,
    )

    with TestClient(application) as client:
        started = client.post("/alerts/gmail/test", follow_redirects=False)
        deadline = time.monotonic() + 3
        while application.state.scanner.is_running and time.monotonic() < deadline:
            time.sleep(0.01)
        provider = application.state.repository.connector_state("gmail:zillow")
        aggregate = application.state.repository.connector_state("gmail")
        page = client.get("/alerts")

    with application.state.repository.connection() as connection:
        listing = connection.execute(
            "SELECT platform, score FROM listings LIMIT 1",
        ).fetchone()
    assert started.status_code == 303
    assert "Gmail+alert+test+started" in started.headers["location"]
    assert provider is not None and provider.state == "working"
    assert aggregate is not None and aggregate.state == "working"
    assert listing is not None
    assert listing["platform"] == "Zillow"
    assert listing["score"] >= 0
    assert "Imported 1 Zillow listing" in page.text


def test_gmail_provider_state_survives_restart(tmp_path: Path, monkeypatch) -> None:
    connect_mailbox_properties(monkeypatch)
    settings = settings_for(tmp_path)
    first = create_app(settings=settings, sources=[], enable_scheduler=False)
    first.state.repository.set_connector_state(
        "gmail:zillow",
        "working",
        message="Imported one Zillow alert.",
        observed_items=1,
        configured=True,
        attempted=True,
        succeeded=True,
    )
    first.state.scanner.refresh_gmail_connector_state()

    restarted = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(restarted) as client:
        page = client.get("/alerts")

    assert restarted.state.repository.connector_state("gmail:zillow").state == "working"
    assert "Imported one Zillow alert" in page.text


def test_disconnect_clears_active_provider_states_without_blocking_public_sources(
    tmp_path: Path, monkeypatch
) -> None:
    connect_mailbox_properties(monkeypatch)
    monkeypatch.setattr(GmailAlertMailbox, "disconnect", lambda self, revoke=True: True)
    application = create_app(settings=settings_for(tmp_path), sources=[], enable_scheduler=False)
    application.state.repository.set_connector_state(
        "gmail:zillow", "working", message="Working", configured=True, succeeded=True
    )

    with TestClient(application) as client:
        response = client.post("/alerts/gmail/disconnect", follow_redirects=False)

    gmail = application.state.repository.connector_state("gmail")
    provider = application.state.repository.connector_state("gmail:zillow")
    assert response.status_code == 303
    assert gmail is not None and gmail.state == "configured_unverified"
    assert provider is not None and provider.state == "disabled"
    assert "public+sources+continue+working" in response.headers["location"]


def test_gmail_test_without_authorization_is_honestly_blocked(tmp_path: Path) -> None:
    application = create_app(settings=settings_for(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.post("/alerts/gmail/test", follow_redirects=False)

    assert response.status_code == 303
    assert "Connect+an+email+account" in response.headers["location"]
    assert application.state.repository.connector_state("gmail").state == "configured_unverified"


def test_consent_denial_is_recorded_without_claiming_connection(tmp_path: Path) -> None:
    application = create_app(settings=settings_for(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.get(
            "/alerts/gmail/callback?error=access_denied", follow_redirects=False
        )

    state = application.state.repository.connector_state("gmail")
    assert response.status_code == 303
    assert state is not None and state.state == "configured_unverified"
    assert "did not grant" in state.message


def test_successful_callback_is_authorized_but_not_falsely_working(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        GmailAlertMailbox,
        "complete_authorization",
        lambda self, callback_url, state, code: None,
    )
    application = create_app(settings=settings_for(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.get(
            "/alerts/gmail/callback?state=expected&code=approved", follow_redirects=False
        )

    state = application.state.repository.connector_state("gmail")
    assert response.status_code == 303
    assert state is not None and state.state == "configured_unverified"
    assert "authorized" in state.message.casefold()


def test_reconnect_starts_a_fresh_authorization_without_touching_public_state(
    tmp_path: Path, monkeypatch
) -> None:
    connect_mailbox_properties(monkeypatch)
    monkeypatch.setattr(
        GmailAlertMailbox,
        "begin_authorization",
        lambda self, callback_url: "https://accounts.google.test/authorize?state=fresh",
    )
    application = create_app(settings=settings_for(tmp_path), sources=[], enable_scheduler=False)
    application.state.repository.set_connector_state(
        "public-fixture", "working", message="Public source stays healthy", succeeded=True
    )

    with TestClient(application) as client:
        response = client.get("/alerts/gmail/connect", follow_redirects=False)

    gmail = application.state.repository.connector_state("gmail")
    public = application.state.repository.connector_state("public-fixture")
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://accounts.google.test/")
    assert gmail is not None and gmail.state == "checking"
    assert public is not None and public.state == "working"


def test_a_provider_checked_directly_leaves_the_email_list(tmp_path: Path, monkeypatch) -> None:
    """Zumper moved to a direct source and kept its row here, so a connected
    mailbox showed it waiting for an alert nothing would ever send."""
    import re

    from sf_housing.sources import ZumperSource

    connect_mailbox_properties(monkeypatch)
    application = create_app(
        settings=settings_for(tmp_path),
        sources=[ZillowAlertSource(FixtureMailbox()), ZumperSource()],
        enable_scheduler=False,
    )
    with TestClient(application) as client:
        page = client.get("/alerts").text

    listed = re.findall(r'<strong>([A-Za-z. ]+)</strong>\s*\n?\s*<span class="connector-state', page)
    assert "Zumper" not in listed, "it is read directly, so email adds nothing"
    assert "Zillow" in listed, "and the ones email really does add have to stay"


def test_connecting_a_mailbox_does_not_empty_the_provider_list(
    tmp_path: Path, monkeypatch
) -> None:
    """An alert source reports mode "automatic" the moment a mailbox connects.
    Filtering the list on mode alone therefore removed every provider exactly
    when it started working."""
    import re

    connect_mailbox_properties(monkeypatch)
    application = create_app(
        settings=settings_for(tmp_path),
        sources=[ZillowAlertSource(FixtureMailbox())],
        enable_scheduler=False,
    )
    with TestClient(application) as client:
        page = client.get("/alerts").text

    listed = re.findall(r'<strong>([A-Za-z. ]+)</strong>\s*\n?\s*<span class="connector-state', page)
    assert "Zillow" in listed, listed
    assert len(listed) >= 5, f"a connected mailbox must not empty the list: {listed}"


def test_a_connected_mailbox_says_it_is_only_half_the_job(tmp_path: Path, monkeypatch) -> None:
    """Connecting reads mail; it does not ask any site to send any. The page
    implied connecting was the whole job, so a reader watched five providers sit
    at "waiting for first alert" with nothing telling them why."""
    connect_mailbox_properties(monkeypatch)
    application = create_app(
        settings=settings_for(tmp_path),
        sources=[ZillowAlertSource(FixtureMailbox())],
        enable_scheduler=False,
    )
    with TestClient(application) as client:
        page = client.get("/alerts").text

    assert "step 1 of 2" in page
    assert "Step 2: turn the alert emails on" in page
    assert "it is not a fault" in page


def test_every_provider_gets_a_link_to_the_page_it_is_set_up_on(
    tmp_path: Path, monkeypatch
) -> None:
    """"Save separate studio/1-bedroom, exact 2-bedroom and exact 3-bedroom SF
    searches" was the whole instruction for two of these, with no link at all."""
    from sf_housing.app import ALERT_SETUP_SEARCHES

    connect_mailbox_properties(monkeypatch)
    application = create_app(
        settings=settings_for(tmp_path),
        sources=[ZillowAlertSource(FixtureMailbox())],
        enable_scheduler=False,
    )
    with TestClient(application) as client:
        page = client.get("/alerts").text

    # Named rather than looped, so deleting an entry deletes a passing test
    # rather than the assertion that would have caught it.
    for platform in ("HotPads", "Apartments.com", "Roomies"):
        assert platform in ALERT_SETUP_SEARCHES, f"{platform} lost its setup link"
        assert ALERT_SETUP_SEARCHES[platform] in page, f"{platform}'s link is not on the page"
    assert "zillow.com" in page and "facebook.com" in page


def test_the_steps_name_the_buttons_each_site_actually_shows() -> None:
    """Checked in a browser: HotPads says "Save search", Apartments.com says
    "Save Search", Zillow offers Instant, Roomies calls them Listing Alerts."""
    import pathlib

    page = pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")

    for phrase in ("Save search", "Save Search", "Instant", "Listing Alerts", "Notify me", "All Filters"):
        assert phrase in page, f"the steps no longer name {phrase!r}"


def test_the_roomies_link_is_the_one_that_actually_resolves() -> None:
    """rooms-for-rent/san-francisco--ca was the obvious guess and returns
    "We couldn't find what you were looking for"."""
    from sf_housing.app import ALERT_SETUP_SEARCHES

    assert ALERT_SETUP_SEARCHES["Roomies"] == "https://www.roomies.com/san-francisco-ca"
    assert "rooms-for-rent" not in ALERT_SETUP_SEARCHES["Roomies"]
    assert "/apartments/" in ALERT_SETUP_SEARCHES["Apartments.com"], "the bare city path 404s"
