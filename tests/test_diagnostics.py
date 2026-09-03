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
    assert "Copy redacted report" in page.text
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
