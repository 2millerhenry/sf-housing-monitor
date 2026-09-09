from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from sf_housing.apify import ApifyTokenStore
from sf_housing.app import create_app
from sf_housing.database import SCHEMA_VERSION, Repository
from sf_housing.diagnostics import redact_metadata, run_diagnostics
from sf_housing.freshness import FRESHNESS_WINDOW
from sf_housing.furnished_finder_bridge import BRIDGE_VERSION
from sf_housing.gmail_alerts import GmailAlertMailbox
from sf_housing.preferences import ensure_preferences, load_preferences
from sf_housing.scanner import Scanner
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES


def settings_for(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    return Settings(
        data_dir=data,
        preferences_path=data / "config" / "preferences.yaml",
        database_path=data / "housing.sqlite3",
        log_path=data / "housing.log",
    )


def components(tmp_path: Path):
    settings = settings_for(tmp_path)
    preferences = ensure_preferences(settings.preferences_path)
    repository = Repository(settings.database_path)
    repository.initialize()
    mailbox = GmailAlertMailbox(
        settings.data_dir / "gmail-client-secret.json",
        settings.data_dir / "gmail-token.json",
        settings.data_dir / "gmail-oauth-state.json",
    )
    apify = ApifyTokenStore(settings.data_dir / "apify-token.txt")
    scanner = Scanner(repository, lambda: load_preferences(settings.preferences_path), [])
    return settings, preferences, repository, mailbox, apify, scanner


def test_blank_ready_check_is_healthy_but_requires_the_deal(tmp_path: Path) -> None:
    application = create_app(settings=settings_for(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/support")
        payload = client.get("/support/report.json").json()

    checks = {check["key"]: check for check in payload["checks"]}
    assert page.status_code == 200
    assert "Finish Your deal" in page.text
    assert "Copy the report" in page.text
    assert "no passwords" in page.text, "and says what the report leaves out"
    assert payload["overall"] == "setup_incomplete"
    assert checks["database"]["status"] == "pass"
    assert checks["deal_profile"]["status"] == "attention"
    assert checks["gmail"]["status"] == "not_applicable"


def test_active_profile_with_no_optional_connectors_is_ready(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True)
    settings.preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        payload = client.get("/support/report.json").json()

    assert payload["overall"] == "ready"
    assert payload["headline"] == "Ready to find homes"
    assert all(check["status"] != "blocked" for check in payload["checks"])


def test_ready_check_detects_a_stale_database_scan_without_calling_it_running(tmp_path: Path) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)
    settings.preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    run_id = repository.begin_scan("manual")
    old = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    with repository.connection() as connection:
        connection.execute("UPDATE scan_runs SET started_at = ? WHERE id = ?", (old, run_id))
        connection.commit()

    report = run_diagnostics(
        settings, repository, scanner, mailbox, apify, app_version="test", now=datetime.now(UTC)
    )
    scanner_check = next(check for check in report.checks if check.key == "scanner")

    assert scanner_check.status == "attention"
    assert "interrupted" in scanner_check.label.casefold()


def test_ready_check_reports_database_integrity_and_schema(tmp_path: Path) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)

    report = run_diagnostics(settings, repository, scanner, mailbox, apify, app_version="test")
    database = next(check for check in report.checks if check.key == "database")

    assert database.status == "pass"
    assert database.metadata == {
        "integrity": "ok",
        "schema": SCHEMA_VERSION,
        "expected_schema": SCHEMA_VERSION,
    }


def test_ready_check_rejects_an_invalid_installed_launch_agent(
    tmp_path: Path, monkeypatch
) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)
    runtime = settings.data_dir.parent / "current" / "bin" / "python"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("runtime", encoding="utf-8")
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.sfhousing.monitor.plist").write_text("not a plist", encoding="utf-8")
    monkeypatch.setenv("SF_HOUSING_LAUNCH_AGENTS_DIR", str(agents))

    report = run_diagnostics(settings, repository, scanner, mailbox, apify, app_version="test")
    login = next(check for check in report.checks if check.key == "login_service")

    assert login.status == "blocked"
    assert login.owner == "Repair"


def test_launch_agent_accepts_a_symlink_alias_for_the_same_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    real_root = tmp_path / "real root with spaces"
    alias_root = tmp_path / "alias root"
    alias_root.symlink_to(real_root, target_is_directory=True)
    settings, _, repository, mailbox, apify, scanner = components(real_root)
    runtime = settings.data_dir.parent / "current" / "bin" / "python"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("runtime", encoding="utf-8")
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    alias_runtime = alias_root / "current" / "bin" / "python"
    # settings.data_dir.parent is real_root, while the plist reaches the same
    # inode through alias_root.
    payload = {
        "Label": "com.sfhousing.monitor",
        "ProgramArguments": [
            str(alias_runtime),
            "-m",
            "sf_housing",
            "serve",
            "--host",
            "127.0.0.1",
        ],
    }
    import plistlib

    with (agents / "com.sfhousing.monitor.plist").open("wb") as handle:
        plistlib.dump(payload, handle)
    monkeypatch.setenv("SF_HOUSING_LAUNCH_AGENTS_DIR", str(agents))
    monkeypatch.setenv("SF_HOUSING_NO_LAUNCH_AGENT", "1")

    report = run_diagnostics(settings, repository, scanner, mailbox, apify)
    login = next(check for check in report.checks if check.key == "login_service")

    assert login.status == "unknown"


def test_redacted_report_never_exposes_credentials_or_account_paths() -> None:
    redacted = redact_metadata(
        {
            "access_token": "ya29.real-token",
            "nested": {"apiToken": "apify_api_abcdefghijklmnopqrstuvwxyz"},
            "url": "http://localhost/callback?code=secret-code&state=secret-state",
            "path": "/Users/privateperson/Library/Application Support/App",
        }
    )
    text = json.dumps(redacted)

    assert "real-token" not in text
    assert "abcdefghijklmnopqrstuvwxyz" not in text
    assert "secret-code" not in text
    assert "privateperson" not in text
    assert text.count("[redacted]") >= 4


def test_configured_connector_attention_is_actionable_but_public_app_remains_usable(
    tmp_path: Path,
) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)
    settings.preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    mailbox.client_secret_path.write_text("{}", encoding="utf-8")
    repository.set_connector_state(
        "gmail",
        "configured_unverified",
        message="OAuth client is ready for one bounded test.",
        configured=True,
    )

    report = run_diagnostics(settings, repository, scanner, mailbox, apify, app_version="test")
    gmail = next(check for check in report.checks if check.key == "gmail")

    assert report.overall == "needs_attention"
    assert gmail.status == "attention"
    assert gmail.action.startswith("Open Sources")


def test_database_failure_is_reported_without_hiding_later_checks(
    tmp_path: Path, monkeypatch
) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)
    monkeypatch.setattr(repository, "integrity_check", lambda: (False, "corrupt"))
    monkeypatch.setattr(repository, "schema_version", lambda: -1)

    report = run_diagnostics(settings, repository, scanner, mailbox, apify)
    checks = {check.key: check for check in report.checks}

    assert checks["database"].status == "blocked"
    assert checks["deal_profile"].status == "attention"
    assert checks["gmail"].status == "not_applicable"


def test_live_scan_progress_is_not_reported_as_stale(tmp_path: Path) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)
    scanner._scan_lock.acquire()
    scanner._progress_state.update(
        {
            "running": True,
            "status": "running",
            "sources_total": 4,
            "sources_completed": 2,
            "current_source": "Craigslist",
        }
    )
    try:
        report = run_diagnostics(settings, repository, scanner, mailbox, apify)
    finally:
        scanner._scan_lock.release()
    check = next(item for item in report.checks if item.key == "scanner")

    assert check.status == "pass"
    assert "2 of 4" in check.explanation


@pytest.mark.parametrize(
    ("status", "seen", "expected", "phrase"),
    [
        ("success", 0, "pass", "0 listing"),
        ("success", 3, "pass", "3 listing"),
        ("error", 0, "attention", "problem"),
    ],
)
def test_public_source_zero_success_and_error_remain_distinct(
    tmp_path: Path, status: str, seen: int, expected: str, phrase: str
) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)
    run_id = repository.begin_scan("manual")
    source_id = repository.begin_source_run(
        run_id, "Craigslist", "https://sfbay.craigslist.org/", provider="CraigslistSource"
    )
    repository.finish_source_run(source_id, status, seen=seen, message="fixture")
    repository.finish_scan(run_id, "completed" if status == "success" else "completed_with_errors")

    report = run_diagnostics(settings, repository, scanner, mailbox, apify)
    check = next(item for item in report.checks if item.key == "public_sources")

    assert check.status == expected
    assert phrase in check.explanation


def test_optional_gmail_and_apify_runs_do_not_impersonate_public_source_health(
    tmp_path: Path,
) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)
    run_id = repository.begin_scan("gmail_test")
    for platform, provider in (("Zillow", "gmail"), ("Facebook Marketplace", "apify")):
        source_id = repository.begin_source_run(
            run_id, platform, "https://example.test", provider=provider
        )
        repository.finish_source_run(source_id, "error", message="fixture")
    repository.finish_scan(run_id, "completed_with_errors", failed=2)

    report = run_diagnostics(settings, repository, scanner, mailbox, apify)
    public = next(item for item in report.checks if item.key == "public_sources")

    assert public.status == "not_applicable"


def test_source_watchdog_surfaces_stale_history_with_one_clear_recovery(tmp_path: Path) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)
    source = type(
        "CraigslistSource",
        (),
        {
            "platform": "Craigslist",
            "mode": "automatic",
            "provider": "public",
            "search_url": "https://example.test/craigslist",
            "manual_reason": None,
        },
    )()
    run_id = repository.begin_scan("manual")
    source_id = repository.begin_source_run(
        run_id, source.platform, source.search_url, provider=source.provider, source_key="CraigslistSource"
    )
    repository.finish_source_run(source_id, "success", seen=2)
    repository.finish_scan(run_id, "completed")
    old = (datetime.now(UTC) - FRESHNESS_WINDOW - timedelta(minutes=2)).isoformat()
    with repository.connection() as connection:
        connection.execute("UPDATE source_runs SET finished_at = ? WHERE id = ?", (old, source_id))
        connection.commit()

    report = run_diagnostics(settings, repository, scanner, mailbox, apify, sources=[source])
    check = next(item for item in report.checks if item.key == "source:CraigslistSource::public")

    assert check.status == "attention"
    assert "stale" in check.label.casefold()
    assert check.action.startswith("Use Check for new homes now")


def test_invalid_gmail_client_is_visible_even_before_authorization(tmp_path: Path) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)
    mailbox.client_secret_path.write_text("{}", encoding="utf-8")

    report = run_diagnostics(settings, repository, scanner, mailbox, apify)
    gmail = next(item for item in report.checks if item.key == "gmail")

    assert gmail.status == "attention"
    assert "owner setup" in gmail.label.casefold()


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("configured_unverified", "attention"),
        ("checking", "attention"),
        ("working", "pass"),
        ("working_zero", "pass"),
        ("waiting_first_alert", "pass"),
        ("degraded", "attention"),
        ("authorization_expired", "attention"),
        ("quota_blocked", "attention"),
        ("disabled", "not_applicable"),
    ],
)
def test_connector_state_machine_maps_to_honest_diagnostics(
    tmp_path: Path, state: str, expected: str
) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)
    apify.save("apify_api_abcdefghijklmnopqrstuvwxyz")
    repository.set_connector_state(
        "apify", state, message=f"Fixture {state}", configured=True, attempted=True
    )

    report = run_diagnostics(settings, repository, scanner, mailbox, apify)
    check = next(item for item in report.checks if item.key == "apify")

    assert check.status == expected


def test_apify_local_allowance_cap_is_actionable_and_redacted(tmp_path: Path) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)
    apify.save("apify_api_abcdefghijklmnopqrstuvwxyz")
    apify.usage_path.write_text(
        json.dumps(
            {
                "month": datetime.now(UTC).strftime("%Y-%m"),
                "runs": 60,
                "group_posts": 0,
                "furnished_finder_listings": 0,
            }
        ),
        encoding="utf-8",
    )

    report = run_diagnostics(settings, repository, scanner, mailbox, apify)
    check = next(item for item in report.checks if item.key == "apify")
    serialized = json.dumps(report.to_dict())

    assert check.status == "attention"
    assert "allowance reached" in check.label.casefold()
    assert "abcdefghijklmnopqrstuvwxyz" not in serialized


def test_configured_bridge_version_mismatch_has_repair_action(tmp_path: Path) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)
    bridge = settings.data_dir.parent / "furnished-finder-bridge"
    bridge.mkdir()
    (bridge / "manifest.json").write_text('{"version":"0.1.0"}', encoding="utf-8")
    repository.set_connector_state(
        "furnished_finder",
        "working_zero",
        message="Old heartbeat",
        metadata={"version": "0.1.0"},
        configured=True,
        attempted=True,
        succeeded=True,
    )

    report = run_diagnostics(settings, repository, scanner, mailbox, apify)
    check = next(item for item in report.checks if item.key == "furnished_finder")

    assert check.status == "attention"
    assert check.owner == "Repair"
    assert check.metadata["expected_version"] == BRIDGE_VERSION


def test_unexpected_local_port_and_paths_with_spaces_are_explained(
    tmp_path: Path, monkeypatch
) -> None:
    spaced = tmp_path / "Evan Home With Spaces"
    settings, _, repository, mailbox, apify, scanner = components(spaced)
    monkeypatch.setenv("SF_HOUSING_PORT", "8000")

    report = run_diagnostics(
        settings, repository, scanner, mailbox, apify, request_port=8123
    )
    port = next(item for item in report.checks if item.key == "local_port")

    assert port.status == "attention"
    assert port.owner == "Repair"
    assert settings.data_dir.is_dir()


def test_connectivity_timeout_is_bounded_unknown_and_does_not_mutate_state(
    tmp_path: Path, monkeypatch
) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)

    class Source:
        platform = "Fixture"
        mode = "automatic"
        connector_key = None
        search_url = "https://example.test/search?token=never-report-this"

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            raise httpx.ReadTimeout("fixture timeout")

    monkeypatch.setattr("sf_housing.diagnostics.httpx.Client", Client)
    before = repository.recent_scans()

    report = run_diagnostics(
        settings,
        repository,
        scanner,
        mailbox,
        apify,
        sources=[Source()],
        include_connectivity=True,
    )
    probe = next(item for item in report.checks if item.key == "public_connectivity")

    assert probe.status == "unknown"
    assert repository.recent_scans() == before
    assert "never-report-this" not in json.dumps(report.to_dict())


def test_diagnostic_order_is_stable(tmp_path: Path) -> None:
    settings, _, repository, mailbox, apify, scanner = components(tmp_path)

    first = run_diagnostics(settings, repository, scanner, mailbox, apify)
    second = run_diagnostics(settings, repository, scanner, mailbox, apify)

    assert [item.key for item in first.checks] == [item.key for item in second.checks]


# --------------------------------------------------------------------------
# the support page as somebody actually uses it
# --------------------------------------------------------------------------


def test_the_verdict_carries_the_evidence_for_itself(tmp_path: Path) -> None:
    """"Everything is working" is a claim. The counts it rests on belong beside
    it, not at the bottom of the page."""
    import re

    settings = settings_for(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        page = client.get("/support").text

    hero = re.search(r'<section class="hero compact support-hero">(.*?)</section>', page, re.S)
    assert hero, "the verdict block is gone"
    assert "working</span>" in hero.group(1), "the tally sits with the verdict"


def test_the_page_says_how_to_reach_a_person(tmp_path: Path) -> None:
    """A failing check states its own next step, but some problems are not on
    that list, and a page with no way to ask anybody anything is a dead end."""
    from sf_housing.diagnostics import SUPPORT_EMAIL

    settings = settings_for(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        page = client.get("/support").text

    assert SUPPORT_EMAIL in page
    assert f"mailto:{SUPPORT_EMAIL}" in page, "and makes it one click"
    assert "Still not right?" in page


def test_an_empty_contact_renders_no_contact_block(tmp_path: Path) -> None:
    """A fork must never ship somebody else's inbox."""
    import re

    settings = settings_for(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        page = client.get("/support").text
    page = re.sub(r"mailto:[^\"]+", "", page)  # sanity: the assertion below is about rendering

    from sf_housing.app import create_app as build
    import sf_housing.app as app_module

    original = app_module.SUPPORT_EMAIL
    app_module.SUPPORT_EMAIL = ""
    try:
        blank = build(settings=settings, sources=[], enable_scheduler=False)
        with TestClient(blank) as client:
            without = client.get("/support").text
    finally:
        app_module.SUPPORT_EMAIL = original

    assert "mailto:" not in without, "no address configured means no mail link"
    assert "Copy the report" in without, "but the report is still offered"


def test_only_the_connection_test_reaches_the_internet(tmp_path: Path) -> None:
    """Opening Support must tell nobody it was opened."""
    settings = settings_for(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        page = client.get("/support").text

    assert "only thing on this page that reaches the internet" in page
    assert "/support?probe=1" in page, "and it is opt-in"


def test_the_things_to_fix_read_as_one_list_not_a_stack_of_alarms() -> None:
    """Most of what lands here is a listing site declining for a few hours, and
    it retries itself. Five separately bordered cards, each with a coloured
    edge, said something louder than that before a word had been read. One
    frame, hairline rows, and a dot carrying the only colour on the row."""
    from tests.test_deal_profile import block_body, stylesheet

    style = stylesheet()
    row = block_body(style, ".fix-row {")
    frame = block_body(style, ".fix-rows { ")

    assert "border-left" not in row, row
    assert "border:" not in row, "the frame belongs to the list, not to every row"
    assert "border: 1px solid var(--line)" in frame, frame
    assert ".fix-row + .fix-row { border-top:" in style, "the rows need a hairline between them"
    # Severity still shows, at the smallest size that can carry it. Round on
    # purpose: a square of colour against the leading edge of a row is the
    # side stripe again in miniature, which is the thing this replaced.
    dot = block_body(style, ".fix-row::before {")
    assert "background: var(--amber)" in dot and "border-radius: 50%" in dot, dot
    assert ".fix-row.blocked::before { background: var(--red); }" in style


def test_something_to_try_comes_before_the_archive(tmp_path: Path) -> None:
    """Somebody on this page wants to try something. The actions were at the
    foot of it, under two folds of history nobody has to read."""
    settings = settings_for(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        page = client.get("/support").text

    assert page.index("Still not right?") < page.index("What was checked")
    assert page.index("Check again") < page.index("Copy the report"), "try it again first"


def test_every_class_the_support_page_uses_is_actually_styled() -> None:
    """The committed repository shipped a Support page whose own stylesheet had
    none of its rules: fix-list, fix-row, support-fold and support-tools existed
    in the template and nowhere else, so a fresh clone rendered it unstyled.
    Template and stylesheet have to travel together."""
    import re
    from pathlib import Path as _Path

    markup = _Path("sf_housing/templates/support.html").read_text(encoding="utf-8")
    css = _Path("sf_housing/static/style.css").read_text(encoding="utf-8")

    used = set(re.findall(r'class="([^"{}]+)"', markup))
    names = {name for group in used for name in group.split() if not name.startswith("{")}
    missing = sorted(name for name in names if f".{name}" not in css)

    assert not missing, f"styled nowhere: {missing}"


# --------------------------------------------------------------------------
# showing somebody their own data
# --------------------------------------------------------------------------


def support_app(tmp_path):
    from sf_housing.app import create_app

    settings = app_settings(tmp_path) if "app_settings" in globals() else None
    if settings is None:
        from sf_housing.settings import Settings

        data = tmp_path / "data"
        settings = Settings(
            data_dir=data,
            preferences_path=data / "config" / "preferences.yaml",
            database_path=data / "housing.sqlite3",
            log_path=data / "housing.log",
        )
    return create_app(settings=settings, sources=[], enable_scheduler=False), settings


def test_the_button_opens_the_data_folder_not_the_program_directory(tmp_path, monkeypatch):
    """The folder above this one also holds runtimes, cache and python -- some
    hundreds of megabytes of machinery that is not the user's. Opening that
    would show somebody a program directory and call it their data."""
    from fastapi.testclient import TestClient

    import sf_housing.app as app_module

    opened: list = []
    monkeypatch.setattr(app_module, "_reveal_folder", lambda folder: opened.append(folder) or True)
    application, settings = support_app(tmp_path)

    with TestClient(application) as client:
        response = client.post("/support/show-data-folder", follow_redirects=False)

    assert response.status_code == 303
    assert opened == [settings.data_dir], opened
    assert opened[0] != settings.data_dir.parent


def test_a_computer_with_no_file_browser_is_still_told_where_the_data_is(
    tmp_path, monkeypatch
):
    """The path is worth more than the apology. Somebody whose file browser
    will not open can still copy this and get there another way."""
    from urllib.parse import unquote

    from fastapi.testclient import TestClient

    import sf_housing.app as app_module

    monkeypatch.setattr(app_module, "_reveal_folder", lambda folder: False)
    application, settings = support_app(tmp_path)

    with TestClient(application) as client:
        response = client.post("/support/show-data-folder", follow_redirects=False)

    assert response.status_code == 303
    location = unquote(response.headers["location"])
    assert "error=" in location
    assert str(settings.data_dir) in location, location


def test_a_first_run_with_no_data_folder_yet_says_so_rather_than_failing(
    tmp_path, monkeypatch
):
    """Before the first check there is nothing to open, and a 500 there reads
    as the app being broken rather than as it being new."""
    from urllib.parse import unquote

    from fastapi.testclient import TestClient

    import sf_housing.app as app_module

    application, settings = support_app(tmp_path)
    monkeypatch.setattr(app_module, "_reveal_folder", lambda folder: True)
    import shutil

    shutil.rmtree(settings.data_dir, ignore_errors=True)

    with TestClient(application) as client:
        response = client.post("/support/show-data-folder", follow_redirects=False)

    assert response.status_code == 303
    assert "no data folder yet" in unquote(response.headers["location"])


def test_opening_a_window_on_somebody_elses_machine_needs_the_local_dashboard(tmp_path):
    """It starts a subprocess. Any page on the internet being able to make
    that happen is not something to leave lying around."""
    from fastapi.testclient import TestClient

    application, _ = support_app(tmp_path)

    with TestClient(application) as client:
        refused = client.post(
            "/support/show-data-folder",
            headers={"host": "127.0.0.1:8000", "origin": "https://evil.example"},
            follow_redirects=False,
        )

    assert refused.status_code == 403, refused.status_code


def test_the_page_says_where_the_data_is_and_how_to_read_it(tmp_path):
    """A file nobody can interpret is not open data."""
    from fastapi.testclient import TestClient

    application, settings = support_app(tmp_path)
    with TestClient(application) as client:
        page = client.get("/support").text

    assert str(settings.data_dir) in page, "the path is not shown"
    assert "listings" in page and "scan_runs" in page and "source_runs" in page
    assert "-wal" in page, "copying a live database without its -wal file loses recent homes"
    assert "_json" in page, "the internal columns are not marked as unsupported"


def test_this_stays_a_button_and_never_becomes_a_way_off_the_machine() -> None:
    """The footer promises this app runs entirely on this computer, and this
    section is where that is proved. An upload, a share link or a send-to-
    support would quietly make it false."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    start_marker = 'class="support-data"'
    body = (root / "sf_housing/templates/support.html").read_text(encoding="utf-8")
    section = body[body.index(start_marker) : body.index("</section>", body.index(start_marker))]

    assert "/support/show-data-folder" in section
    for leak in ("http://", "https://", "upload", "mailto:"):
        assert leak not in section.casefold(), f"the data section offers {leak}"
