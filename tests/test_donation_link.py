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
