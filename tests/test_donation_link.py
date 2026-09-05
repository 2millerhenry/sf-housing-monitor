from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES


DONATE_URL = "https://ko-fi.com/example"


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


def test_an_unconfigured_donation_url_renders_no_link_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty has to mean silent.

    This repository now ships a real donation page, so the guarantee is no
    longer about the default being blank; it is about what happens to anyone who
    clears it, which is what a fork does first. A placeholder or an empty string
    must never put a dead link on every page of an app whose whole promise is
    that it does not send you anywhere.
    """
    monkeypatch.setattr("sf_housing.app.DONATE_URL", "")
    application = create_app(
        settings=app_settings(tmp_path), sources=[], enable_scheduler=False
    )
    with TestClient(application) as client:
        for path in ("/preferences", "/alerts", "/support"):
            page = client.get(path)
            assert page.status_code == 200, path
            assert "Say thanks" not in page.text, path
            assert "ko-fi" not in page.text.lower(), path
        # The footer itself stays, because it also carries version and licence.
        assert "site-footer" in client.get("/preferences").text


def test_a_configured_donation_url_appears_once_in_the_footer_of_every_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sf_housing.app.DONATE_URL", DONATE_URL)
    application = create_app(
        settings=app_settings(tmp_path), sources=[], enable_scheduler=False
    )
    with TestClient(application) as client:
        for path in ("/preferences", "/alerts"):
            page = client.get(path)
            assert page.status_code == 200, path
            assert page.text.count(f'href="{DONATE_URL}"') == 1, path
            assert "Say thanks" in page.text, path


def test_the_donation_link_cannot_reach_back_into_the_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is the one outbound link the app adds on its own initiative.

    A new tab opened without ``noopener`` keeps a handle on the page that
    opened it, so the destination could navigate the local dashboard.
    """
    monkeypatch.setattr("sf_housing.app.DONATE_URL", DONATE_URL)
    application = create_app(
        settings=app_settings(tmp_path), sources=[], enable_scheduler=False
    )
    with TestClient(application) as client:
        markup = client.get("/preferences").text

    anchor = markup[markup.index(f'href="{DONATE_URL}"') :]
    anchor = anchor[: anchor.index(">")]
    assert "noopener" in anchor
    assert "noreferrer" in anchor


def test_the_support_page_asks_only_once_everything_is_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking for money while the page says something is broken reads as a toll.

    The note is tied to the same report the heading is, so the ask can only
    appear underneath "Everything is working".
    """
    monkeypatch.setattr("sf_housing.app.DONATE_URL", DONATE_URL)
    application = create_app(
        settings=app_settings(tmp_path), sources=[], enable_scheduler=False
    )
    with TestClient(application) as client:
        page = client.get("/support")

    assert page.status_code == 200
    ready = "Everything is working" in page.text
    assert ("Keeping this working" in page.text) == ready


def test_the_shipped_donation_url_is_a_real_page_not_a_placeholder() -> None:
    """The opposite failure: a default nobody replaced. ko-fi.com/millerhenry
    was opened in a browser, logged out, and serves "Buy Henry Miller a Coffee"."""
    from sf_housing import DONATE_URL as SHIPPED

    if not SHIPPED:
        return  # a fork that cleared it is covered by the test above
    assert SHIPPED.startswith("https://"), SHIPPED
    for placeholder in ("yourname", "example.com", "yourhandle", "username"):
        assert placeholder not in SHIPPED, f"{SHIPPED} still looks like a placeholder"


# --------------------------------------------------------------------------
# the panel
# --------------------------------------------------------------------------


def test_the_panel_fetches_nothing_from_kofi_until_it_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The footer promises the app runs entirely on this computer. A widget that
    loads on every page view would tell Ko-fi each time the dashboard was
    opened, which is the one thing that promise rules out."""
    monkeypatch.setattr("sf_housing.app.DONATE_URL", DONATE_URL)
    application = create_app(settings=app_settings(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/preferences").text

    assert 'src="about:blank"' in page, "the frame starts empty"
    assert f'src="{DONATE_URL}' not in page, "and the embed url is never a src on load"
    assert "data-donate-embed" in page, "it is held in an attribute until a click"
    assert "storage.ko-fi.com" not in page, "Ko-fi's own script never runs on this page"


def test_the_link_still_works_without_javascript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel is an enhancement. With scripts off, or if it fails to load,
    clicking has to go to Ko-fi exactly as it did before."""
    monkeypatch.setattr("sf_housing.app.DONATE_URL", DONATE_URL)
    application = create_app(settings=app_settings(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/").text

    assert f'href="{DONATE_URL}" target="_blank"' in page
    assert 'rel="noopener noreferrer external"' in page


def test_no_panel_and_no_script_when_no_url_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sf_housing.app.DONATE_URL", "")
    application = create_app(settings=app_settings(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/").text

    assert "donate-dialog" not in page
    assert "donate-panel.js" not in page


def test_the_panel_script_is_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sf_housing.app.DONATE_URL", DONATE_URL)
    application = create_app(settings=app_settings(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        assert client.get("/static/donate-panel.js").status_code == 200
