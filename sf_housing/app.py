from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from statistics import median
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import yaml
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .apify import ApifyTokenError, ApifyTokenStore
from . import __version__
from .connectors import GMAIL_PROVIDERS, ConnectorStatus
from .database import DatabaseUnreadableError, Repository
from .deal_profile import (
    BEDROOM_PATHS,
    SPLIT_PATHS,
    SF_NEIGHBORHOODS,
    DealProfileError,
    canonical_document,
    deal_profile_from_form,
    profile_form_values,
)
from .diagnostics import SUPPORT_EMAIL, report_json, run_diagnostics
from .freshness import evaluate_source_freshness
from .furnished_finder_bridge import (
    BRIDGE_VERSION as FURNISHED_FINDER_BRIDGE_VERSION,
    FurnishedFinderBridgeError,
    PLATFORM as FURNISHED_FINDER_PLATFORM,
    cards_to_candidates,
    validated_search_url,
)
from .gmail_alerts import GmailAlertError, GmailAlertMailbox
from .imap_alerts import AlertMailboxRouter, ImapAlertError, ImapAlertMailbox, host_for_address
from .liveness import describe_age, next_run_label, schedule_health
from .preferences import (
    setting_int,
    PreferenceError,
    Preferences,
    ensure_preferences,
    load_preferences,
    save_deal_profile,
    save_preferences,
)
from .potrero import (
    BOUNDARY_NOTE,
    OFFICIAL_SITE_AUDIT,
    POTRERO_CONTACTS,
    POTRERO_LISTINGS,
    SOURCE_COVERAGE,
    VERIFIED_DATE,
    preferred_count,
    shortlist,
)
from .scanner import Scanner
from .scheduling import PACIFIC, build_scheduler, startup_scan_due
from .settings import Settings
from .sources import (
    FacebookGroupsSource,
    FacebookMarketplaceSource,
    GmailHousingAlertSource,
    FurnishedFinderSource,
    ListingSource,
    default_sources,
)


PACKAGE_DIR = Path(__file__).resolve().parent
FACEBOOK_GROUP_NAMES = {
    "https://www.facebook.com/groups/1105487206638421/": "SF/Bay Area housing, rentals, sublets & roommates",
}


def _reveal_folder(folder: Path) -> bool:
    """Open a folder in the platform file manager.

    ``check=False`` suppresses a non-zero exit code but not a missing binary,
    so the Windows release used to answer this route with a 500 instead of a
    message. Explorer also exits non-zero on success, hence no return-code
    check on either platform.
    """
    command = ["explorer", str(folder)] if os.name == "nt" else ["/usr/bin/open", str(folder)]
    try:
        subprocess.run(command, check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _profile_search_areas(preferences: Preferences) -> list[tuple[str, str]]:
    """Derive setup links only from the active canonical geography."""
    if preferences.deal_profile.anywhere_in_sf:
        return [("San Francisco", "san-francisco-ca")]
    names = list(
        dict.fromkeys(
            preferences.list_value("ideal_neighborhoods")
            + preferences.list_value("preferred_neighborhoods")
            + preferences.list_value("acceptable_neighborhoods")
        )
    )
    return [
        (
            name,
            re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") + "-san-francisco-ca",
        )
        for name in names[:10]
    ]


def _zillow_url_with_room_price_cap(url: str, minimum: int, maximum: int) -> str:
    """Keep a saved Zillow room-search URL, but align its price ceiling to the deal."""
    parsed = urlparse(url)
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    for index, (key, value) in enumerate(query_items):
        if key != "searchQueryState":
            continue
        try:
            state = json.loads(value)
            filters = state.setdefault("filterState", {})
            if not isinstance(filters, dict):
                return url
            price_key = "mp" if isinstance(filters.get("mp"), dict) else "price"
            price = filters.setdefault(price_key, {"min": minimum})
            if not isinstance(price, dict):
                return url
            price["max"] = maximum
            query_items[index] = (key, json.dumps(state, separators=(",", ":")))
        except (TypeError, ValueError, json.JSONDecodeError):
            return url
        return urlunparse(parsed._replace(query=urlencode(query_items)))
    return url


def _prepared_zillow_searches(preferences: Preferences | None = None) -> list[dict[str, str]]:
    """Use the user's saved Zillow URLs when available, with safe defaults."""
    if preferences and "private_room" not in preferences.deal_profile.enabled_paths:
        return []
    configured = (preferences.data.get("zillow_searches") if preferences else None) or []
    budget = preferences.section("budget") if preferences else {}
    room_minimum = setting_int(budget.get("min_monthly"), 800)
    room_maximum = setting_int(budget.get("max_monthly"), 2700)
    searches: list[dict[str, str]] = []
    if isinstance(configured, list):
        for item in configured:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            url = str(item.get("url", "")).strip()
            parsed = urlparse(url)
            if (
                name
                and parsed.scheme == "https"
                and parsed.netloc in {"zillow.com", "www.zillow.com"}
                and parsed.path.endswith("/rentals/")
            ):
                searches.append(
                    {
                        "name": name,
                        "url": _zillow_url_with_room_price_cap(url, room_minimum, room_maximum),
                    }
                )
    if searches:
        return searches

    state = quote(
        json.dumps(
            {
                "filterState": {
                    "price": {"min": room_minimum, "max": room_maximum},
                    "fr": {"value": True},
                    "fsba": {"value": False},
                }
            },
            separators=(",", ":"),
        )
    )
    return [
        {
            "name": name,
            "url": f"https://www.zillow.com/{slug}/rentals/?searchQueryState={state}",
        }
        for name, slug in (_profile_search_areas(preferences) if preferences else [("San Francisco", "san-francisco-ca")])
    ]


def _prepared_zillow_unit_searches(preferences: Preferences) -> list[dict[str, str]]:
    """Prepared whole-unit searches that can become Zillow Instant alerts."""
    if not set(preferences.deal_profile.enabled_paths).intersection({"studio", "one_bedroom"}):
        return []
    max_monthly = setting_int(preferences.section("whole_unit").get("max_monthly"), 3000)
    state = quote(
        json.dumps(
            {
                "filterState": {
                    "price": {"min": 0, "max": max_monthly},
                    "beds": {"min": 0, "max": 1},
                    "fr": {"value": True},
                    "fsba": {"value": False},
                }
            },
            separators=(",", ":"),
        )
    )
    return [
        {
            "name": name,
            "url": f"https://www.zillow.com/{slug}/rentals/?searchQueryState={state}",
        }
        for name, slug in _profile_search_areas(preferences)
    ]


def _prepared_zillow_split_searches(preferences: Preferences) -> list[dict[str, str]]:
    """Prepared exact-bedroom searches so each split keeps its own hard cap."""
    searches: list[dict[str, str]] = []
    enabled = set(preferences.deal_profile.enabled_paths)
    for bedroom_count, section_name, default_occupants, default_cap in (
        (2, "two_bedroom", 2, 2700),
        (3, "three_bedroom", 3, 2500),
    ):
        if section_name not in enabled:
            continue
        settings = preferences.section(section_name)
        max_monthly = int(settings.get("occupants", default_occupants)) * int(
            settings.get("max_per_person", default_cap)
        )
        state = quote(
            json.dumps(
                {
                    "filterState": {
                        "price": {"min": 0, "max": max_monthly},
                        "beds": {"min": bedroom_count, "max": bedroom_count},
                        "fr": {"value": True},
                        "fsba": {"value": False},
                    }
                },
                separators=(",", ":"),
            )
        )
        searches.extend(
            {
                "name": name,
                "bedrooms": str(bedroom_count),
                "url": f"https://www.zillow.com/{slug}/rentals/?searchQueryState={state}",
            }
            for name, slug in _profile_search_areas(preferences)
        )
    return searches


def _prepared_facebook_searches(preferences: Preferences) -> list[dict[str, str]]:
    """Use Marketplace's actual keyword-search route for its free native alerts."""
    if "private_room" not in preferences.deal_profile.enabled_paths:
        return []
    areas = [name for name, _ in _profile_search_areas(preferences)]
    return [
        {
            "name": area,
            "url": (
                "https://www.facebook.com/marketplace/114952118516947/search/?"
                + urlencode({"query": f"private room {area}"})
            ),
        }
        for area in areas[:5]
    ]


def _prepared_facebook_unit_searches(preferences: Preferences) -> list[dict[str, str]]:
    if not set(preferences.deal_profile.enabled_paths).intersection({"studio", "one_bedroom"}):
        return []
    areas = [name for name, _ in _profile_search_areas(preferences)]
    return [
        {
            "name": area,
            "url": (
                "https://www.facebook.com/marketplace/114952118516947/search/?"
                + urlencode({"query": f"studio 1 bedroom {area}"})
            ),
        }
        for area in areas[:5]
    ]


# Where each site's San Francisco search actually lives. Every one of these was
# opened in a browser and checked: hotpads.com/san-francisco-ca/apartments-for-rent
# and apartments.com/apartments/san-francisco-ca/ both load real results, and
# roomies.com/rooms-for-rent/san-francisco--ca -- the obvious guess -- returns
# "We couldn't find what you were looking for", so it is /san-francisco-ca.
ALERT_SETUP_SEARCHES = {
    "HotPads": "https://hotpads.com/san-francisco-ca/apartments-for-rent",
    "Apartments.com": "https://www.apartments.com/apartments/san-francisco-ca/",
    "Roomies": "https://www.roomies.com/san-francisco-ca",
}


def _prepared_facebook_split_searches(preferences: Preferences) -> list[dict[str, str]]:
    enabled = set(preferences.deal_profile.enabled_paths)
    areas = [name for name, _ in _profile_search_areas(preferences)]
    return [
        {
            "name": area,
            "bedrooms": str(bedrooms),
            "url": (
                "https://www.facebook.com/marketplace/114952118516947/search/?"
                + urlencode({"query": f"{bedrooms} bedroom apartment {area}"})
            ),
        }
        for bedrooms in (2, 3, 4)
        if BEDROOM_PATHS[bedrooms] in enabled
        for area in areas[:5]
    ]


def _prepared_facebook_sublet_searches(preferences: Preferences) -> list[dict[str, str]]:
    """Native-alert searches for the long-enough Facebook sublets we admit."""
    areas = [name for name, _ in _profile_search_areas(preferences)]
    return [
        {
            "name": area,
            "url": (
                "https://www.facebook.com/marketplace/114952118516947/search/?"
                + urlencode({"query": f"sublet 6 months {area}"})
            ),
        }
        for area in areas[:5]
    ]


def _prepared_facebook_groups(preferences: Preferences) -> list[dict[str, str]]:
    return [
        {"name": FACEBOOK_GROUP_NAMES.get(url, f"Facebook group {index}"), "url": url}
        for index, url in enumerate(FacebookGroupsSource._group_urls(preferences), start=1)
    ]


def configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(path) for handler in root.handlers):
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)


def _pacific_datetime(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(PACIFIC).strftime("%b %-d, %Y %-I:%M %p")
    except (ValueError, TypeError):
        return value


def _compact_pacific_date(value: str | None, *, now: datetime | None = None) -> str:
    """Format a listing discovery date for a narrow comparison column."""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value).astimezone(PACIFIC)
        current = (now or datetime.now(PACIFIC)).astimezone(PACIFIC)
        days_ago = (current.date() - parsed.date()).days
        if days_ago == 0:
            return "Today"
        if days_ago == 1:
            return "Yesterday"
        if parsed.year == current.year:
            return parsed.strftime("%b %-d")
        return parsed.strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        return value


def _was_checked_today(value: str | None, *, now: datetime | None = None) -> bool:
    """Whether a source or verification pass saw the listing today in Pacific time."""
    if not value:
        return False
    try:
        checked = datetime.fromisoformat(value).astimezone(PACIFIC)
        current = (now or datetime.now(PACIFIC)).astimezone(PACIFIC)
        return checked.date() == current.date()
    except (ValueError, TypeError):
        return False


def _compact_move_in_date(value: str | None) -> str:
    """Show a source-provided move-in date without inventing a timezone."""
    if not value:
        return "Not stated"
    try:
        return date.fromisoformat(value).strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        return value


_RELATIVE_POSTED_AGE = re.compile(
    r"\b(?:listed|posted)\s+(?:(?P<instant>just now|today|yesterday)|(?P<count>\d+)\s+(?P<unit>minutes?|hours?|days?)\s+ago)\b",
    re.IGNORECASE,
)


def _published_at(value: object) -> datetime | None:
    """Parse a source-supplied listing timestamp without treating discovery as posting."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1_000
            return datetime.fromtimestamp(timestamp, tz=UTC)
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _relative_posted_age(text: str | None) -> timedelta | None:
    """Interpret only explicit source text such as 'Listed yesterday' or 'Posted 2 days ago'."""
    match = _RELATIVE_POSTED_AGE.search(text or "")
    if not match:
        return None
    instant = (match.group("instant") or "").casefold()
    if instant in {"just now", "today"}:
        return timedelta(0)
    if instant == "yesterday":
        return timedelta(days=1)
    count = int(match.group("count") or 0)
    unit = (match.group("unit") or "").casefold()
    if unit.startswith("minute"):
        return timedelta(minutes=count)
    if unit.startswith("hour"):
        return timedelta(hours=count)
    if unit.startswith("day"):
        return timedelta(days=count)
    return None


def _is_recently_posted(listing: dict[str, object], *, now: datetime | None = None) -> bool:
    """Whether the source says this listing was posted no more than two days ago."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    metadata = listing.get("metadata")
    timestamp = metadata.get("listing_timestamp") if isinstance(metadata, dict) else None
    posted_at = _published_at(timestamp)
    if posted_at is not None:
        age = current.astimezone(UTC) - posted_at.astimezone(UTC)
        return timedelta(0) <= age <= timedelta(days=2)
    relative_age = _relative_posted_age(str(listing.get("summary") or ""))
    return relative_age is not None and relative_age <= timedelta(days=2)


def _typical_scan_seconds(scans: list[dict[str, object]], trigger: str | None = None) -> int | None:
    """Return an honest median duration from comparable completed scans."""
    durations: list[float] = []
    for scan in scans:
        if trigger and scan.get("trigger") != trigger:
            continue
        if scan.get("status") not in {"completed", "completed_with_errors"}:
            continue
        try:
            started = datetime.fromisoformat(str(scan["started_at"]))
            finished = datetime.fromisoformat(str(scan["finished_at"]))
        except (KeyError, TypeError, ValueError):
            continue
        duration = (finished - started).total_seconds()
        if 0 <= duration <= 600:
            durations.append(duration)
    if not durations:
        return None
    return max(1, round(median(durations)))


# The shortlist cut-off, in the steps the slider offers. Thirty is low enough to
# be "show me nearly everything" without including homes the deal already ruled
# out; ninety-five leaves a shortlist that can still have something on it.
CUTOFF_STOPS = tuple(range(30, 100, 5))


def _safe_return(value: str | None) -> str:
    """A redirect target that can only be a page of this app.

    Rejecting a leading "//" is not enough on its own. A browser normalises a
    backslash to a forward slash before resolving a URL, so "/\\evil.test"
    leaves as "//evil.test" and points off this machine entirely; it also strips
    control characters first, so those can reconstruct the same thing. Every
    caller of this either redirects a form post or renders a back link, so
    anything that is not a plain local path goes to the dashboard instead.
    """
    candidate = str(value or "")
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    if "\\" in candidate or any(character <= "\x1f" or character == "\x7f" for character in candidate):
        return "/"
    return candidate


def _preference_form_values(preferences: Preferences) -> dict[str, object]:
    budget = preferences.section("budget")
    lease = preferences.section("lease")
    availability = preferences.section("availability")
    household = preferences.section("household")
    return {
        "minimum_score": preferences.minimum_score,
        "min_monthly": budget.get("min_monthly", 800),
        "ideal_monthly": budget.get("ideal_monthly", 1500),
        "max_monthly": budget.get("max_monthly", 2700),
        "sweet_spot_min": budget.get("sweet_spot_min", 1200),
        "sweet_spot_max": budget.get("sweet_spot_max", 1800),
        "ideal_neighborhoods": ", ".join(preferences.list_value("ideal_neighborhoods")),
        "preferred_neighborhoods": ", ".join(preferences.list_value("preferred_neighborhoods")),
        "acceptable_neighborhoods": ", ".join(preferences.list_value("acceptable_neighborhoods")),
        "ideal_min_months": lease.get("ideal_min_months", 3),
        "ideal_max_months": lease.get("ideal_max_months", 8),
        "max_months": lease.get("max_months", 12),
        "preferred_by": availability.get("preferred_by", ""),
        "latest_by": availability.get("latest_by", ""),
        "ideal_people": household.get("ideal_people", 3),
        "max_people": household.get("max_people", 5),
        "whole_unit_max_monthly": preferences.section("whole_unit").get("max_monthly", 3000),
        "whole_unit_max_building_units": preferences.section("whole_unit").get(
            "max_building_units", 50
        ),
        "two_bedroom_occupants": preferences.section("two_bedroom").get("occupants", 2),
        "two_bedroom_max_per_person": preferences.section("two_bedroom").get(
            "max_per_person", 2700
        ),
        "two_bedroom_max_building_units": preferences.section("two_bedroom").get(
            "max_building_units", 50
        ),
        "three_bedroom_occupants": preferences.section("three_bedroom").get("occupants", 3),
        "three_bedroom_max_per_person": preferences.section("three_bedroom").get(
            "max_per_person", 2500
        ),
        "three_bedroom_max_building_units": preferences.section("three_bedroom").get(
            "max_building_units", 50
        ),
    }


def _comma_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def create_app(
    settings: Settings | None = None,
    sources: list[ListingSource] | None = None,
    enable_scheduler: bool = True,
) -> FastAPI:
    active_settings = settings or Settings.from_environment()
    configure_logging(active_settings.log_path)
    initial_preferences = ensure_preferences(active_settings.preferences_path)
    repository = Repository(active_settings.database_path)
    try:
        repository.initialize()
    except DatabaseUnreadableError:
        # The service log is what Repair and the install log surface, so the
        # recovery has to be readable there rather than only in a traceback.
        logging.getLogger(__name__).error(
            "Startup stopped: the housing database could not be opened. "
            "Run Repair SF Housing Monitor; the most recent backup is restored and "
            "the existing file is left untouched."
        )
        raise
    gmail_mailbox = GmailAlertMailbox(
        active_settings.gmail_client_secret_path or active_settings.data_dir / "gmail-client-secret.json",
        active_settings.gmail_token_path or active_settings.data_dir / "gmail-token.json",
        active_settings.gmail_pending_state_path or active_settings.data_dir / "gmail-oauth-state.json",
    )
    imap_mailbox = ImapAlertMailbox(
        active_settings.imap_credential_path or active_settings.data_dir / "imap-credential.json"
    )
    # An app password needs no cloud project, so when one is saved it is the
    # mailbox the alert sources read. The Gmail OAuth path stays available for
    # accounts app passwords cannot serve -- Advanced Protection, some managed
    # Workspace accounts, and Outlook.com.
    mailbox = AlertMailboxRouter(imap_mailbox, gmail_mailbox)
    apify_tokens = ApifyTokenStore(
        active_settings.apify_token_path or active_settings.data_dir / "apify-token.txt"
    )
    if repository.connector_state("gmail") is None:
        gmail_state = (
            "configured_unverified"
            if gmail_mailbox.is_connected or gmail_mailbox.has_client_secret
            else "not_configured"
        )
        repository.set_connector_state(
            "gmail",
            gmail_state,
            message=(
                "Gmail is authorized but has not completed a saved-search check yet."
                if gmail_mailbox.is_connected
                else "Google OAuth is ready for authorization."
                if gmail_mailbox.has_client_secret
                else gmail_mailbox.client_configuration_error
                or "Add the owner-provided Google OAuth client to connect Gmail."
            ),
            configured=gmail_mailbox.has_client_secret,
        )
    if repository.connector_state("apify") is None:
        repository.set_connector_state(
            "apify",
            "configured_unverified" if apify_tokens.is_configured else "not_configured",
            message=(
                "The token is stored locally and needs one bounded test."
                if apify_tokens.is_configured
                else "Optional Facebook coverage is not configured."
            ),
            configured=apify_tokens.is_configured,
        )
    if repository.connector_state("furnished_finder") is None:
        repository.set_connector_state(
            "furnished_finder",
            "not_configured",
            message="Install the bundled Chrome bridge when you want Furnished Finder coverage.",
        )
    active_sources = sources if sources is not None else default_sources(mailbox, apify_tokens)
    scanner = Scanner(
        repository,
        lambda: load_preferences(active_settings.preferences_path),
        active_sources,
        timeout_seconds=active_settings.request_timeout_seconds,
        max_scan_seconds=active_settings.scan_max_seconds,
    )
    if initial_preferences.profile_active:
        scanner.rescore_all(initial_preferences)
    scheduler = build_scheduler(scanner)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if enable_scheduler:
            scheduler.start()
            logging.getLogger(__name__).info("Scheduler started for 10:00 and 18:00 America/Los_Angeles")
            if load_preferences(active_settings.preferences_path).profile_active and startup_scan_due(
                repository.recent_scans(20)
            ):
                scanner.start_scan("startup_catchup")
                logging.getLogger(__name__).info("Started catch-up scan for a missed schedule")
        yield
        if enable_scheduler and scheduler.running:
            scheduler.shutdown(wait=False)

    application = FastAPI(title="SF Housing Monitor", version=__version__, lifespan=lifespan)
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )

    @application.middleware("http")
    async def local_request_protection(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            host = request.headers.get("host", "").split(":", 1)[0].casefold()
            is_test_client = host == "testserver"
            origin = request.headers.get("origin", "")
            is_bridge = request.url.path.startswith("/integrations/furnished-finder/")
            if is_bridge:
                try:
                    content_length = int(request.headers.get("content-length", "0") or 0)
                except ValueError:
                    return JSONResponse({"detail": "Invalid request size."}, status_code=400)
                if content_length > 1_000_000:
                    return JSONResponse({"detail": "Bridge payload is too large."}, status_code=413)
                bridge_version = request.headers.get("x-sfh-bridge-version", "")
                if not is_test_client and bridge_version != FURNISHED_FINDER_BRIDGE_VERSION:
                    return JSONResponse(
                        {"detail": "Install the current bundled Furnished Finder bridge."},
                        status_code=409,
                    )
                if not is_test_client and not origin.startswith("chrome-extension://"):
                    return JSONResponse({"detail": "Bridge origin is not allowed."}, status_code=403)
            elif not is_test_client:
                allowed_origins = {
                    f"http://127.0.0.1:{request.url.port or 8000}",
                    f"http://localhost:{request.url.port or 8000}",
                }
                if origin not in allowed_origins:
                    return JSONResponse(
                        {"detail": "This action must come from the local dashboard."},
                        status_code=403,
                    )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response
    application.state.settings = active_settings
    application.state.repository = repository
    application.state.scanner = scanner
    application.state.scheduler = scheduler
    application.state.scheduler_enabled = enable_scheduler
    application.state.mailbox = mailbox
    application.state.apify_tokens = apify_tokens
    application.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.globals["app_version"] = __version__

    def asset_version() -> str:
        """Bust the cache when a static file actually changes.

        Versioning assets by the app version meant an edited stylesheet kept the
        same URL, so browsers served the previous copy and the page rendered new
        markup against old rules.
        """
        static_dir = PACKAGE_DIR / "static"
        try:
            newest = max(path.stat().st_mtime_ns for path in static_dir.glob("*.*"))
        except (OSError, ValueError):
            return __version__
        return f"{__version__}-{newest:x}"

    templates.env.globals["asset_version"] = asset_version
    templates.env.filters["pacific_datetime"] = _pacific_datetime
    templates.env.filters["compact_pacific_date"] = _compact_pacific_date
    templates.env.filters["compact_move_in_date"] = _compact_move_in_date

    @application.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        sort: str = "score",
        neighborhood: str = "",
        platform: str = "",
        home_style: str = "",
        housing: str = "room",
        unit_type: str = "",
        area_priority: str = "",
        view: str = "active",
        message: str = "",
        scan: str = "",
    ):
        valid_sorts = {"score", "contact", "available", "newest", "price", "unopened"}
        sort = sort if sort in valid_sorts else "score"
        view = view if view in {"active", "saved", "dismissed", "near_matches", "all"} else "active"
        preferences = load_preferences(active_settings.preferences_path)
        if not preferences.profile_active:
            return RedirectResponse("/preferences?welcome=1", status_code=303)
        enabled_paths = set(preferences.deal_profile.enabled_paths)
        # One tab per home size the user actually chose, in the order a person
        # thinks about them. Lumping "studios & 1-bedrooms" and "2-3 bedrooms"
        # hid which size a result was, and left a fourth bedroom nowhere to go.
        enabled_housing_modes = [
            mode
            for mode, path in (
                ("room", "private_room"),
                ("studio", "studio"),
                ("one_bedroom", "one_bedroom"),
                ("two_bedroom", "two_bedroom"),
                ("three_bedroom", "three_bedroom"),
                ("four_bedroom", "four_bedroom"),
            )
            if path in enabled_paths
        ]
        if not enabled_housing_modes:
            enabled_housing_modes = ["room"]
        # Older links said whole_unit or lumped the splits together; send them to
        # the first size they actually cover rather than 404ing a bookmark.
        legacy_modes = {
            "whole_unit": ("studio", "one_bedroom"),
            "two_bedroom": ("two_bedroom", "three_bedroom", "four_bedroom"),
        }
        requested_mode = housing
        if requested_mode in legacy_modes:
            requested_mode = next(
                (mode for mode in legacy_modes[requested_mode] if mode in enabled_housing_modes),
                enabled_housing_modes[0],
            )
        housing_mode = requested_mode if requested_mode in enabled_housing_modes else enabled_housing_modes[0]
        housing_kind = "room" if housing_mode == "room" else "whole_unit"
        mode_unit_types = () if housing_mode == "room" else (housing_mode,)
        # The tab is the size now, so a separate size dropdown would only be a
        # second way to say the same thing.
        selected_unit_type = ""
        selected_area_priority = (
            area_priority
            if housing_mode != "room" and area_priority in {"dream_strong", "secondary"}
            else ""
        )
        query_unit_type = selected_unit_type
        listings = repository.query_listings(
            minimum_score=preferences.minimum_score,
            sort=sort,
            neighborhood=neighborhood,
            platform=platform,
            home_style=home_style,
            housing_kind=housing_kind,
            unit_type=query_unit_type,
            unit_types=mode_unit_types,
            view=view,
        )
        if selected_area_priority == "dream_strong":
            listings = [
                listing
                for listing in listings
                if listing.get("neighborhood_priority") in {"dream", "strong"}
            ]
        elif selected_area_priority == "secondary":
            listings = [
                listing for listing in listings if listing.get("neighborhood_priority") == "secondary"
            ]
        current_time = datetime.now(UTC)
        for listing in listings:
            listing["is_recently_posted"] = _is_recently_posted(listing, now=current_time)
            listing["was_checked_today"] = _was_checked_today(
                str(listing.get("last_seen") or ""), now=current_time
            )
        # An empty shortlist with nothing else on the page reads as a broken
        # search. Almost always the search worked and the deal is narrow, so
        # when little or nothing survives, say what held the rest back. Only
        # computed on the thin path, so the normal one pays nothing.
        exclusion_summary: list[dict[str, object]] = []
        # Shown whenever the deal is holding back more homes than it is letting
        # through, not only when the shortlist is empty. "There should be more
        # listings than this" is the right instinct, and the answer is almost
        # never that a source broke -- it is which limit is costing what, which
        # the page had no way of saying.
        if view == "active":
            exclusion_summary = repository.exclusion_summary(
                preferences.minimum_score, housing_kind, mode_unit_types
            )
        option_minimum = preferences.minimum_score if view == "active" else 0
        neighborhoods, stored_platforms = repository.filter_options(
            option_minimum, housing_kind, mode_unit_types
        )
        # A source selector is also a way to inspect whether a connected source has
        # found anything yet. Keep configured sources visible even when every one of
        # their current listings is below the active match threshold.
        platforms = sorted(
            {*(str(item) for item in stored_platforms), *(source.platform for source in active_sources)},
            key=str.casefold,
        )
        latest_by_source = {
            str(item.get("source_key") or item["platform"]): item
            for item in repository.latest_source_runs()
        }
        source_statuses = []
        for source in active_sources:
            health = evaluate_source_freshness(repository, source, now=current_time)
            latest = latest_by_source.get(health.key)
            if latest:
                status = dict(latest)
            else:
                status = {
                    "platform": source.platform,
                    "search_url": source.search_url,
                    "finished_at": None,
                    "listings_seen": 0,
                    "listings_added": 0,
                }
            # Preserve the raw source-run record for debugging, but render the
            # derived freshness truth so an old success cannot look healthy.
            status.update(
                {
                    "platform": source.platform,
                    "status": health.status,
                    "freshness_label": health.label,
                    "freshness_explanation": health.explanation,
                    "recovery_action": health.action,
                    "failure_streak": health.failure_streak,
                    "last_success_at": health.last_success_at,
                    "next_retry_at": health.next_retry_at,
                }
            )
            source_statuses.append(status)
        source_attention_count = sum(
            1 for status in source_statuses if status["status"] in {"attention", "stale", "backoff"}
        )
        view_query_parts = [f"view={quote(view)}"]
        if housing_mode != "room":
            view_query_parts.append(f"housing={housing_mode}")
        if neighborhood:
            view_query_parts.append(f"neighborhood={quote(neighborhood)}")
        if platform:
            view_query_parts.append(f"platform={quote(platform)}")
        if home_style:
            view_query_parts.append(f"home_style={quote(home_style)}")
        if selected_unit_type and housing_mode in {"whole_unit", "two_bedroom"}:
            view_query_parts.append(f"unit_type={quote(selected_unit_type)}")
        if selected_area_priority:
            view_query_parts.append(f"area_priority={quote(selected_area_priority)}")
        return_to = "/?" + "&".join([f"sort={quote(sort)}", *view_query_parts])
        sort_urls = {
            sort_name: "/?" + "&".join([f"sort={sort_name}", *view_query_parts])
            for sort_name in ("score", "contact", "price", "available", "newest", "unopened")
        }
        recent_scans = repository.recent_scans()
        scan_progress = scanner.progress
        schedule_state = schedule_health(
            scheduler,
            recent_scans,
            scan_running=scanner.is_running,
            managed=enable_scheduler,
        )
        dashboard_liveness = {
            "state": schedule_state.state,
            "summary": schedule_state.summary,
            "last_checked": describe_age(schedule_state.last_finished_at, datetime.now(UTC)),
            "next_check": next_run_label(schedule_state),
            "ok": schedule_state.ok,
        }
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "listings": listings,
                "sort": sort,
                "selected_neighborhood": neighborhood,
                "selected_platform": platform,
                "selected_home_style": home_style,
                "selected_unit_type": selected_unit_type,
                "selected_area_priority": selected_area_priority,
                "housing_mode": housing_mode,
                "enabled_housing_modes": enabled_housing_modes,
                "enabled_paths": enabled_paths,
                "view": view,
                "message": message,
                "neighborhoods": neighborhoods,
                "platforms": platforms,
                "minimum_score": preferences.minimum_score,
                "room_min_monthly": setting_int(preferences.section("budget").get("min_monthly"), 800),
                "room_max_monthly": setting_int(preferences.section("budget").get("max_monthly"), 2700),
                "whole_unit_max_monthly": int(
                    preferences.section("whole_unit").get("max_monthly", 3000)
                ),
                "two_bedroom_occupants": setting_int(preferences.section("two_bedroom").get("occupants"), 2),
                "two_bedroom_max_per_person": setting_int(preferences.section("two_bedroom").get("max_per_person"), 2700),
                "three_bedroom_occupants": setting_int(preferences.section("three_bedroom").get("occupants"), 3),
                "three_bedroom_max_per_person": setting_int(preferences.section("three_bedroom").get("max_per_person"), 2500),
                "profile_incomplete": preferences.profile_incomplete,
                "source_statuses": source_statuses,
                "source_attention_count": source_attention_count,
                "scans": recent_scans,
                "liveness": dashboard_liveness,
                "scan_running": scanner.is_running,
                # A scan can finish before the redirected dashboard response
                # renders. Preserve one short, truthful completion state so a
                # user always gets visible feedback after pressing Check.
                "scan_feedback": scanner.is_running or scan == "starting",
                "scan_progress": scan_progress,
                "typical_scan_seconds": _typical_scan_seconds(
                    repository.recent_scans(20), str(scan_progress.get("trigger") or "manual")
                ),
                "return_to": return_to,
                "sort_urls": sort_urls,
                "exclusion_summary": exclusion_summary,
                "excluded_total": sum(int(item["count"]) for item in exclusion_summary),
            },
        )

    @application.post("/scan")
    def manual_scan(return_to: str = Form("/")):
        if not load_preferences(active_settings.preferences_path).profile_active:
            return RedirectResponse(
                "/preferences?welcome=1&error=Finish+Your+deal+before+checking+sources",
                status_code=303,
            )
        if not scanner.start_scan("manual"):
            destination = _safe_return(return_to)
            separator = "&" if "?" in destination else "?"
            return RedirectResponse(
                destination + separator + "message=Scan+already+running", status_code=303
            )
        destination = _safe_return(return_to)
        separator = "&" if "?" in destination else "?"
        return RedirectResponse(destination + separator + "scan=starting", status_code=303)

    @application.get("/potrero", response_class=HTMLResponse)
    def potrero_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="potrero.html",
            context={
                "boundary_note": BOUNDARY_NOTE,
                "contacts": POTRERO_CONTACTS,
                "listings": POTRERO_LISTINGS,
                "official_audit": OFFICIAL_SITE_AUDIT,
                "preferred_count": preferred_count(),
                "shortlist": shortlist(),
                "source_coverage": SOURCE_COVERAGE,
                "verified_date": VERIFIED_DATE,
            },
        )

    @application.post("/listings/{listing_id}/status")
    def listing_status(
        listing_id: int,
        status: str = Form(...),
        return_to: str = Form("/"),
    ):
        try:
            updated = repository.set_listing_status(listing_id, status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not updated:
            raise HTTPException(status_code=404, detail="Listing not found")
        return RedirectResponse(_safe_return(return_to), status_code=303)

    @application.post("/listings/{listing_id}/note")
    def listing_note(
        listing_id: int,
        note: str = Form(""),
        return_to: str = Form("/"),
    ):
        if len(note) > 4000:
            raise HTTPException(status_code=400, detail="Note must be 4,000 characters or fewer")
        if not repository.set_listing_note(listing_id, note.strip()):
            raise HTTPException(status_code=404, detail="Listing not found")
        return RedirectResponse(_safe_return(return_to), status_code=303)

    @application.post("/listings/{listing_id}/opened", status_code=204)
    def listing_opened(listing_id: int):
        if not repository.mark_listing_opened(listing_id):
            raise HTTPException(status_code=404, detail="Listing not found")
        return Response(status_code=204)

    @application.get("/listings/{listing_id}", response_class=HTMLResponse)
    def listing_detail(request: Request, listing_id: int):
        """One home on its own page.

        A listing worth acting on had nowhere to be read in full: the table
        truncates, the source's own description was never shown at all, and the
        only link out of a row went straight to the source. The starred card is
        the same job, so this reuses it rather than growing a second layout.
        """
        listing = repository.listing(listing_id)
        if listing is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        origin = _safe_return(request.query_params.get("from"))
        # Acting on the home keeps the reader on the home; leaving is what the
        # one back link is for.
        own_url = f"/listings/{listing_id}"
        if origin != "/":
            own_url += "?from=" + quote(origin, safe="")
        back_labels = {
            "saved": "Back to starred listings",
            "dismissed": "Back to passed listings",
            "near_matches": "Back to near matches",
            "all": "Back to the archive",
        }
        origin_view = dict(parse_qsl(urlparse(origin).query)).get("view", "")
        return templates.TemplateResponse(
            request=request,
            name="listing.html",
            context={
                "listing": listing,
                "return_to": own_url,
                "back_to": origin,
                "back_label": back_labels.get(origin_view, "Back to the shortlist"),
                "view": origin_view,
            },
        )

    @application.get("/listings/{listing_id}/open")
    def open_listing(listing_id: int):
        original_url = repository.open_listing_url(listing_id)
        if original_url is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        return RedirectResponse(original_url, status_code=307)

    def deal_page_context(
        preferences: Preferences,
        *,
        message: str = "",
        error: str = "",
        welcome: bool = False,
        values: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "preferences_text": active_settings.preferences_path.read_text(encoding="utf-8"),
            "preferences_path": active_settings.preferences_path,
            "form_values": _preference_form_values(preferences),
            "deal_values": values or profile_form_values(preferences.deal_profile),
            "neighborhood_options": SF_NEIGHBORHOODS,
            "minimum_score": preferences.minimum_score,
            # What each stop on the slider would actually put on the shortlist,
            # so the number means something while it is being dragged.
            # Only the home shapes this deal actually shows, so the number under
            # the slider is the number of rows the tabs will hold.
            "shortlist_counts": repository.shortlist_counts(
                CUTOFF_STOPS,
                kinds=sorted(
                    {
                        "room" if path == "private_room" else "whole_unit"
                        for path in preferences.deal_profile.enabled_paths
                    }
                ),
            ),
            "cutoff_stops": CUTOFF_STOPS,
            "message": message,
            "error": error,
            "welcome": welcome or not preferences.profile_active,
        }

    @application.get("/preferences", response_class=HTMLResponse)
    def preferences_page(
        request: Request,
        message: str = "",
        error: str = "",
        welcome: int = 0,
    ):
        preferences = load_preferences(active_settings.preferences_path)
        return templates.TemplateResponse(
            request=request,
            name="preferences.html",
            context=deal_page_context(
                preferences,
                message=message,
                error=error,
                welcome=bool(welcome),
            ),
        )

    @application.post("/preferences/deal", response_class=HTMLResponse)
    async def update_deal(request: Request):
        current = load_preferences(active_settings.preferences_path)
        form = await request.form()
        if scanner.is_running:
            return templates.TemplateResponse(
                request=request,
                name="preferences.html",
                status_code=409,
                context=deal_page_context(
                    current,
                    error="A source check is still running. Wait for it to finish, then save again.",
                    welcome=not current.profile_active,
                ),
            )
        try:
            profile = deal_profile_from_form(form)
        except DealProfileError as exc:
            return templates.TemplateResponse(
                request=request,
                name="preferences.html",
                status_code=400,
                context=deal_page_context(
                    current,
                    error=str(exc),
                    welcome=not current.profile_active,
                ),
            )

        first_activation = not current.profile_active
        # How close a match has to be before it reaches the shortlist. Anything
        # under it is kept in near matches rather than thrown away, so this
        # only moves the line, never the homes.
        try:
            requested_score = int(str(form.get("minimum_score", current.minimum_score)).strip() or current.minimum_score)
        except (TypeError, ValueError):
            requested_score = current.minimum_score
        minimum_score = min(95, max(30, requested_score))
        preferences = save_deal_profile(
            active_settings.preferences_path,
            profile,
            current,
            technical={"minimum_score": minimum_score},
        )
        rescored = scanner.rescore_all(preferences)
        if first_activation:
            scanner.start_scan("initial_discovery")
            return RedirectResponse(
                "/?message=Your+deal+is+ready%3B+checking+free+public+sources+now&scan=starting",
                status_code=303,
            )
        return RedirectResponse(
            f"/?message=Your+deal+was+saved%3B+{rescored}+stored+listings+were+reranked",
            status_code=303,
        )

    @application.post("/preferences/deal/preview")
    async def preview_deal_profile(request: Request):
        """Describe the deal the form currently holds, without saving anything.

        The review section reads the profile's own summary, so it has to come
        from the same code that writes it -- rebuilding that sentence in the
        browser would be two descriptions of one deal, free to disagree.
        """
        try:
            profile = deal_profile_from_form(await request.form(), state="draft")
        except (DealProfileError, ValueError) as exc:
            return JSONResponse({"ok": False, "reason": str(exc)}, status_code=200)
        return JSONResponse({"ok": True, "summary": profile.summary()})

    @application.post("/preferences/deal/reset")
    async def reset_deal_profile(request: Request):
        """Put the deal back to a blank draft, and touch nothing else.

        Starting over means starting the answers over. The homes already
        collected, the ones starred, the notes written against them and every
        first-found date stay exactly where they are, and are reranked against
        whatever deal comes next.
        """
        current = load_preferences(active_settings.preferences_path)
        if scanner.is_running:
            return templates.TemplateResponse(
                request=request,
                name="preferences.html",
                status_code=409,
                context=deal_page_context(
                    current,
                    error="A source check is still running. Wait for it to finish, then start over.",
                ),
            )
        # Built through the same constructor the form uses, so a reset profile
        # is exactly the profile a new install has, with the technical settings
        # left alone.
        save_deal_profile(
            active_settings.preferences_path,
            deal_profile_from_form({}, state="draft"),
            current,
        )
        LOGGER_APP = logging.getLogger(__name__)
        LOGGER_APP.info("Deal profile reset to a blank draft on request")
        return RedirectResponse(
            "/preferences?welcome=1&message=Your+deal+was+cleared%3B+your+saved+homes+and+notes+were+kept",
            status_code=303,
        )

    @application.post("/preferences/draft")
    async def save_preferences_draft(request: Request):
        current = load_preferences(active_settings.preferences_path)
        if current.profile_active:
            return JSONResponse({"saved": False, "reason": "active_profile"}, status_code=409)
        try:
            profile = deal_profile_from_form(await request.form(), state="draft")
            save_deal_profile(active_settings.preferences_path, profile, current)
        except (DealProfileError, ValueError) as exc:
            return JSONResponse({"saved": False, "reason": str(exc)}, status_code=422)
        return JSONResponse({"saved": True, "summary": profile.summary()})

    @application.post("/preferences/profile", response_class=HTMLResponse)
    def update_preference_profile(
        request: Request,
        minimum_score: int = Form(...),
        min_monthly: int = Form(...),
        ideal_monthly: int = Form(...),
        max_monthly: int = Form(...),
        sweet_spot_min: int = Form(...),
        sweet_spot_max: int = Form(...),
        ideal_neighborhoods: str = Form(...),
        preferred_neighborhoods: str = Form(...),
        acceptable_neighborhoods: str = Form(""),
        ideal_min_months: int = Form(...),
        ideal_max_months: int = Form(...),
        max_months: int = Form(...),
        preferred_by: str = Form(""),
        latest_by: str = Form(""),
        ideal_people: int = Form(...),
        max_people: int = Form(...),
        whole_unit_max_monthly: int = Form(3000),
        whole_unit_max_building_units: int = Form(50),
        two_bedroom_occupants: int = Form(2),
        two_bedroom_max_per_person: int = Form(2700),
        two_bedroom_max_building_units: int = Form(50),
        three_bedroom_occupants: int = Form(3),
        three_bedroom_max_per_person: int = Form(2500),
        three_bedroom_max_building_units: int = Form(50),
    ):
        current_text = active_settings.preferences_path.read_text(encoding="utf-8")
        current_preferences = load_preferences(active_settings.preferences_path)
        submitted = {
            "minimum_score": minimum_score,
            "min_monthly": min_monthly,
            "ideal_monthly": ideal_monthly,
            "max_monthly": max_monthly,
            "sweet_spot_min": sweet_spot_min,
            "sweet_spot_max": sweet_spot_max,
            "ideal_neighborhoods": ideal_neighborhoods,
            "preferred_neighborhoods": preferred_neighborhoods,
            "acceptable_neighborhoods": acceptable_neighborhoods,
            "ideal_min_months": ideal_min_months,
            "ideal_max_months": ideal_max_months,
            "max_months": max_months,
            "preferred_by": preferred_by,
            "latest_by": latest_by,
            "ideal_people": ideal_people,
            "max_people": max_people,
            "whole_unit_max_monthly": whole_unit_max_monthly,
            "whole_unit_max_building_units": whole_unit_max_building_units,
            "two_bedroom_occupants": two_bedroom_occupants,
            "two_bedroom_max_per_person": two_bedroom_max_per_person,
            "two_bedroom_max_building_units": two_bedroom_max_building_units,
            "three_bedroom_occupants": three_bedroom_occupants,
            "three_bedroom_max_per_person": three_bedroom_max_per_person,
            "three_bedroom_max_building_units": three_bedroom_max_building_units,
        }

        error = ""
        availability_dates: tuple[date, date] | None = None
        if preferred_by or latest_by:
            try:
                availability_dates = (date.fromisoformat(preferred_by), date.fromisoformat(latest_by))
            except ValueError:
                error = "Use valid move-in dates for both the preferred and near-term windows."
            else:
                if availability_dates[0] > availability_dates[1]:
                    error = "The preferred move-in date must be before the near-term cutoff."
        if scanner.is_running:
            error = "Wait for the current scan to finish, then save your deal again."
        elif not 0 <= minimum_score <= 100:
            error = "Minimum match score must be between 0 and 100."
        elif not (0 < min_monthly <= sweet_spot_min <= ideal_monthly <= sweet_spot_max <= max_monthly):
            error = "Budget values must run from minimum to sweet spot to ideal to maximum."
        elif not (0 < ideal_min_months <= ideal_max_months <= max_months):
            error = "Lease values must run from the shortest ideal stay to the absolute maximum."
        elif not (1 <= ideal_people <= max_people):
            error = "Ideal household size cannot be larger than the maximum household size."
        elif whole_unit_max_monthly <= 0 or not 1 <= whole_unit_max_building_units <= 500:
            error = "The whole-unit budget and building-size limit must be positive."
        elif not 1 <= two_bedroom_occupants <= 10 or two_bedroom_max_per_person <= 0 or not 1 <= two_bedroom_max_building_units <= 500:
            error = "The 2-bedroom split, per-person budget, and building-size limit must be positive."
        elif not 1 <= three_bedroom_occupants <= 10 or three_bedroom_max_per_person <= 0 or not 1 <= three_bedroom_max_building_units <= 500:
            error = "The 3-bedroom split, per-person budget, and building-size limit must be positive."
        elif not _comma_list(ideal_neighborhoods) or not _comma_list(preferred_neighborhoods):
            error = "Add at least one dream area and one strong area."

        if error:
            return templates.TemplateResponse(
                request=request,
                name="preferences.html",
                status_code=409 if scanner.is_running else 400,
                context={
                    "preferences_text": current_text,
                    "preferences_path": active_settings.preferences_path,
                    "form_values": submitted,
                    "message": "",
                    "error": error,
                },
            )

        data = deepcopy(current_preferences.data)
        data["minimum_score"] = minimum_score
        data.setdefault("budget", {}).update(
            {
                "min_monthly": min_monthly,
                "max_monthly": max_monthly,
                "ideal_monthly": ideal_monthly,
                "sweet_spot_min": sweet_spot_min,
                "sweet_spot_max": sweet_spot_max,
            }
        )
        data["ideal_neighborhoods"] = _comma_list(ideal_neighborhoods)
        data["preferred_neighborhoods"] = _comma_list(preferred_neighborhoods)
        data["acceptable_neighborhoods"] = _comma_list(acceptable_neighborhoods)
        data.setdefault("lease", {}).update(
            {
                "ideal_min_months": ideal_min_months,
                "ideal_max_months": ideal_max_months,
                "max_months": max_months,
            }
        )
        if availability_dates:
            data.setdefault("availability", {}).update(
                {
                    "preferred_by": availability_dates[0].isoformat(),
                    "latest_by": availability_dates[1].isoformat(),
                }
            )
        data.setdefault("household", {}).update(
            {"ideal_people": ideal_people, "max_people": max_people}
        )
        data.setdefault("whole_unit", {}).update(
            {
                "enabled": True,
                "max_monthly": whole_unit_max_monthly,
                "unit_types": ["studio", "one_bedroom"],
                "max_building_units": whole_unit_max_building_units,
            }
        )
        data.setdefault("two_bedroom", {}).update(
            {
                "enabled": True,
                "occupants": two_bedroom_occupants,
                "max_per_person": two_bedroom_max_per_person,
                "max_building_units": two_bedroom_max_building_units,
            }
        )
        data.setdefault("three_bedroom", {}).update(
            {
                "enabled": True,
                "occupants": three_bedroom_occupants,
                "max_per_person": three_bedroom_max_per_person,
                "max_building_units": three_bedroom_max_building_units,
            }
        )
        serialized = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100_000)
        preferences = save_preferences(active_settings.preferences_path, serialized)
        count = scanner.rescore_all(preferences)
        return RedirectResponse(
            f"/preferences?message=Your+deal+was+saved+and+{count}+listings+were+reranked",
            status_code=303,
        )

    @application.post("/preferences", response_class=HTMLResponse)
    def update_preferences(request: Request, preferences_text: str = Form(...)):
        if scanner.is_running:
            return templates.TemplateResponse(
                request=request,
                name="preferences.html",
                status_code=409,
                context={
                    "preferences_text": preferences_text,
                    "preferences_path": active_settings.preferences_path,
                    "form_values": _preference_form_values(load_preferences(active_settings.preferences_path)),
                    "message": "",
                    "error": "Wait for the active scan to finish before changing preferences.",
                },
            )
        try:
            preferences = save_preferences(active_settings.preferences_path, preferences_text)
        except PreferenceError as exc:
            return templates.TemplateResponse(
                request=request,
                name="preferences.html",
                status_code=400,
                context={
                    "preferences_text": preferences_text,
                    "preferences_path": active_settings.preferences_path,
                    "form_values": _preference_form_values(load_preferences(active_settings.preferences_path)),
                    "message": "",
                    "error": str(exc),
                },
            )
        count = scanner.rescore_all(preferences)
        return RedirectResponse(
            f"/preferences?message=Preferences+saved%3B+rescored+{count}+listings",
            status_code=303,
        )

    @application.get("/alerts", response_class=HTMLResponse)
    def alerts_page(request: Request, message: str = "", error: str = ""):
        preferences = load_preferences(active_settings.preferences_path)
        if not preferences.profile_active:
            return RedirectResponse(
                "/preferences?welcome=1&message=Finish+your+deal+first%3B+free+public+sources+start+automatically+after+you+save+it",
                status_code=303,
            )
        connector_states = repository.connector_states()
        # A provider that is checked directly does not belong on a list of
        # things email adds. Zumper moved to a direct source and kept its row
        # here, so a connected mailbox showed it waiting for an alert that
        # nothing would ever send. One rule, used by both the connected list and
        # the invitation below it.
        # An alert source reports mode "automatic" the moment a mailbox is
        # connected, so mode alone cannot answer this: filtering on it removed
        # every provider from the list as soon as it started working. What
        # matters is whether the platform has a source that reads the site
        # itself rather than reading mail about it.
        checked_directly = {
            source.platform
            for source in active_sources
            if getattr(source, "mode", "") == "automatic"
            and getattr(source, "connector_key", None) != "gmail"
        }
        gmail_providers = [
            {
                "key": key,
                "name": name,
                "state": connector_states.get(key),
            }
            for key, name in GMAIL_PROVIDERS
            if name not in checked_directly
        ]
        return templates.TemplateResponse(
            request=request,
            name="alerts.html",
            context={
                "message": message,
                "error": error,
                "has_client_secret": gmail_mailbox.has_client_secret,
                "gmail_connected": gmail_mailbox.is_connected,
                "apify_configured": apify_tokens.is_configured,
                "connector_states": connector_states,
                "gmail_state": connector_states.get("gmail"),
                "gmail_providers": gmail_providers,
                "gmail_client_error": gmail_mailbox.client_configuration_error,
                "gmail_client_kind": gmail_mailbox.client_kind,
                "email_connected": imap_mailbox.is_connected,
                "email_address": (
                    imap_mailbox.credential.address if imap_mailbox.is_connected else ""
                ),
                "email_backend": mailbox.backend,
                # Zumper is checked directly now, so promising that email adds
                # it would be a lie. Derive the list from what is actually still
                # email-only rather than from the connector-key constant.
                "email_providers": [provider["name"] for provider in gmail_providers],
                "apify_state": connector_states.get("apify"),
                "furnished_finder_state": connector_states.get("furnished_finder"),
                "bridge_version": FURNISHED_FINDER_BRIDGE_VERSION,
                "zillow_searches": _prepared_zillow_searches(preferences),
                "zillow_unit_searches": _prepared_zillow_unit_searches(preferences),
                "zillow_split_searches": _prepared_zillow_split_searches(preferences),
                "facebook_searches": _prepared_facebook_searches(preferences),
                "facebook_unit_searches": _prepared_facebook_unit_searches(preferences),
                "facebook_split_searches": _prepared_facebook_split_searches(preferences),
                "alert_setup_searches": ALERT_SETUP_SEARCHES,
                "facebook_sublet_searches": _prepared_facebook_sublet_searches(preferences),
                "facebook_groups": _prepared_facebook_groups(preferences),
                "room_min_monthly": setting_int(preferences.section("budget").get("min_monthly"), 800),
                "room_max_monthly": setting_int(preferences.section("budget").get("max_monthly"), 2700),
                "whole_unit_max_monthly": int(
                    preferences.section("whole_unit").get("max_monthly", 3000)
                ),
            },
        )

    def support_report(request: Request, *, include_connectivity: bool = False):
        return run_diagnostics(
            active_settings,
            repository,
            scanner,
            # The Ready Check reports on the Gmail OAuth files specifically, so
            # it needs that mailbox rather than whichever backend is live.
            gmail_mailbox,
            apify_tokens,
            scheduler=scheduler,
            scheduler_managed=enable_scheduler,
            request_host=request.url.hostname or "unknown",
            request_port=request.url.port or 8000,
            app_version=__version__,
            sources=active_sources,
            include_connectivity=include_connectivity,
        )

    @application.get("/support", response_class=HTMLResponse)
    def support_page(request: Request, probe: int = 0, message: str = ""):
        report = support_report(request, include_connectivity=bool(probe))
        return templates.TemplateResponse(
            request=request,
            name="support.html",
            context={
                "report": report,
                "report_json": report_json(report),
                "message": message,
                "connectivity_checked": bool(probe),
                "support_email": SUPPORT_EMAIL,
            },
        )

    @application.get("/support/report.json")
    def support_report_json(request: Request):
        return JSONResponse(support_report(request).to_dict())

    @application.post("/alerts/apify/token")
    def save_apify_token(apify_token: str = Form(...)):
        try:
            apify_tokens.save(apify_token)
        except ApifyTokenError as exc:
            return RedirectResponse(f"/alerts?error={quote(str(exc))}", status_code=303)
        repository.set_connector_state(
            "apify",
            "configured_unverified",
            message="Token saved locally. Run one bounded connection test.",
            configured=True,
        )
        return RedirectResponse(
            "/alerts?message=Facebook+automation+connected%3B+run+one+connector+test+when+ready",
            status_code=303,
        )

    @application.post("/alerts/apify/test")
    def test_apify_connector():
        if not apify_tokens.is_configured:
            return RedirectResponse("/alerts?error=Connect+Apify+first", status_code=303)
        repository.set_connector_state(
            "apify", "checking", message="Running one bounded Facebook check.", attempted=True
        )
        if not scanner.start_scan("connector_test"):
            return RedirectResponse("/?message=Scan+already+running", status_code=303)
        return RedirectResponse(
            "/?message=Facebook+connector+test+started%3B+this+page+will+refresh+when+it+finishes",
            status_code=303,
        )

    @application.post("/alerts/apify/backfill")
    def backfill_facebook():
        """Seed the local dashboard once without inflating the recurring scan."""
        if not apify_tokens.is_configured:
            return RedirectResponse("/alerts?error=Connect+Apify+first", status_code=303)
        # A fresh source instance keeps the five-card scheduled source immutable.
        # It is deliberately a one-click action, never a scheduled job.
        source = FacebookMarketplaceSource(mailbox, apify_tokens)
        source.apify.results_limit = 40
        if not scanner.start_scan("facebook_backfill", sources=[source]):
            return RedirectResponse("/?message=Scan+already+running", status_code=303)
        return RedirectResponse(
            "/?message=Facebook+backfill+started%3B+checking+up+to+40+recent+SF+cards",
            status_code=303,
        )

    @application.post("/alerts/apify/groups/test")
    def test_facebook_groups():
        if not apify_tokens.is_configured:
            return RedirectResponse("/alerts?error=Connect+Apify+first", status_code=303)
        source = FacebookGroupsSource(apify_tokens)
        if not source._group_urls(load_preferences(active_settings.preferences_path)):
            return RedirectResponse("/alerts?error=Add+a+public+Facebook+group+first", status_code=303)
        if not scanner.start_scan("facebook_groups_test", sources=[source]):
            return RedirectResponse("/?message=Scan+already+running", status_code=303)
        return RedirectResponse(
            "/?message=Facebook+Groups+check+started%3B+checking+up+to+five+newest+posts",
            status_code=303,
        )

    @application.post("/alerts/facebook-groups")
    def add_facebook_group(group_url: str = Form(...)):
        match = re.fullmatch(
            r"https://(?:www\.)?facebook\.com/groups/([A-Za-z0-9._-]+)/?",
            group_url.strip(),
            re.IGNORECASE,
        )
        if not match:
            return RedirectResponse(
                "/alerts?error=Use+a+public+Facebook+group+URL+ending+at+the+group+name+or+ID",
                status_code=303,
            )
        normalized = f"https://www.facebook.com/groups/{match.group(1)}/"
        current = load_preferences(active_settings.preferences_path)
        document = canonical_document(
            current.deal_profile,
            current.canonical if current.canonical is not None else current.data,
        )
        sources_config = document.setdefault("technical", {}).setdefault("sources", {})
        existing = FacebookGroupsSource._group_urls(current)
        if normalized not in existing and len(existing) >= 10:
            return RedirectResponse(
                "/alerts?error=The+public-group+limit+is+10%3B+remove+one+before+adding+another",
                status_code=303,
            )
        sources_config["facebook_group_urls"] = list(dict.fromkeys([*existing, normalized]))
        save_preferences(
            active_settings.preferences_path,
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100_000),
        )
        return RedirectResponse(
            "/alerts?message=Public+Facebook+group+saved%3B+run+the+bounded+group+test+when+ready",
            status_code=303,
        )

    @application.post("/alerts/facebook-groups/remove")
    def remove_facebook_group(group_url: str = Form(...)):
        current = load_preferences(active_settings.preferences_path)
        remaining = [url for url in FacebookGroupsSource._group_urls(current) if url != group_url]
        document = canonical_document(
            current.deal_profile,
            current.canonical if current.canonical is not None else current.data,
        )
        document.setdefault("technical", {}).setdefault("sources", {})[
            "facebook_group_urls"
        ] = remaining
        save_preferences(
            active_settings.preferences_path,
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100_000),
        )
        return RedirectResponse("/alerts?message=Public+Facebook+group+removed", status_code=303)

    @application.post("/alerts/apify/furnished-finder/test")
    def test_furnished_finder():
        return RedirectResponse(
            "/alerts?error=Use+the+free+Chrome+bridge+below%3A+the+old+community+helper+is+disabled+because+it+did+not+meet+the+two-minute+reliability+limit",
            status_code=303,
        )

    @application.get("/alerts/furnished-finder-bridge.zip")
    def download_furnished_finder_bridge():
        archive = PACKAGE_DIR / "static" / "furnished-finder-bridge.zip"
        if not archive.exists():
            raise HTTPException(status_code=404, detail="The Chrome bridge download is not packaged yet.")
        return FileResponse(
            archive,
            media_type="application/zip",
            filename="SF Housing Furnished Finder Bridge.zip",
        )

    @application.post("/alerts/furnished-finder/show-folder")
    def show_furnished_finder_folder():
        installed = active_settings.data_dir.parent / "furnished-finder-bridge"
        development = PACKAGE_DIR.parent / "furnished_finder_chrome_bridge"
        folder = installed if installed.is_dir() else development
        if not folder.is_dir():
            return RedirectResponse(
                "/alerts?error=The+extension+folder+is+missing%3B+run+Repair+and+try+again",
                status_code=303,
            )
        if not _reveal_folder(folder):
            return RedirectResponse(
                "/alerts?error=" + quote(f"This computer could not open a file window. The folder is: {folder}"),
                status_code=303,
            )
        return RedirectResponse(
            "/alerts?message=The+extension+folder+is+open+in+your+file+browser",
            status_code=303,
        )

    @application.post("/alerts/furnished-finder/test")
    def test_furnished_finder_bridge():
        state = repository.connector_state("furnished_finder")
        if state is None or state.state == "not_configured":
            return RedirectResponse(
                "/alerts?error=The+Chrome+bridge+has+not+contacted+this+monitor+yet",
                status_code=303,
            )
        if state.state in {"degraded", "authorization_expired"}:
            return RedirectResponse(
                f"/alerts?error={quote(state.message or 'The Chrome bridge needs attention.')}",
                status_code=303,
            )
        return RedirectResponse(
            "/alerts?message=The+Chrome+bridge+is+installed+and+can+reach+this+monitor",
            status_code=303,
        )

    @application.post("/integrations/furnished-finder/heartbeat")
    def furnished_finder_heartbeat(payload: dict[str, object] = Body(...)):
        version = str(payload.get("version") or "").strip()
        raw_urls = payload.get("search_urls")
        if not isinstance(raw_urls, list) or len(raw_urls) > 3:
            raise HTTPException(status_code=422, detail="Send zero to three saved Furnished Finder searches.")
        try:
            search_urls = [validated_search_url(value) for value in raw_urls]
        except FurnishedFinderBridgeError as exc:
            repository.set_connector_state(
                "furnished_finder", "degraded", message=str(exc), attempted=True
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if version != FURNISHED_FINDER_BRIDGE_VERSION:
            message = (
                f"Chrome bridge {version or 'unknown'} is out of date; install bundled version "
                f"{FURNISHED_FINDER_BRIDGE_VERSION}."
            )
            repository.set_connector_state(
                "furnished_finder",
                "degraded",
                message=message,
                metadata={"version": version, "search_urls": search_urls},
                attempted=True,
            )
            raise HTTPException(status_code=409, detail=message)
        state = "working_zero" if search_urls else "configured_unverified"
        message = (
            f"Chrome bridge {version} is reachable with {len(search_urls)} saved search(es)."
            if search_urls
            else "Chrome bridge is reachable. Save a Furnished Finder search to finish setup."
        )
        repository.set_connector_state(
            "furnished_finder",
            state,
            message=message,
            metadata={
                "version": version,
                "search_urls": search_urls,
                "next_check_at": str(payload.get("next_check_at") or "")[:80],
            },
            configured=True,
            attempted=True,
            succeeded=True,
        )
        return JSONResponse({"ok": True, "version": FURNISHED_FINDER_BRIDGE_VERSION})

    @application.post("/integrations/furnished-finder/import")
    def import_furnished_finder_cards(payload: dict[str, object] = Body(...)):
        """Accept a bounded public-card payload from the locally installed Chrome bridge."""
        repository.set_connector_state(
            "furnished_finder", "checking", message="Importing visible cards.", attempted=True
        )
        try:
            search_url = validated_search_url(payload.get("search_url"))
            listings = cards_to_candidates(payload.get("cards"), load_preferences(active_settings.preferences_path))
        except FurnishedFinderBridgeError as exc:
            repository.set_connector_state(
                "furnished_finder", "degraded", message=str(exc), attempted=True
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not listings:
            raise HTTPException(
                status_code=422,
                detail=(
                    "No supported private-room, studio, one-bedroom, two-bedroom, or three-bedroom cards were found. "
                    "Let the Furnished Finder results finish loading, then sync again."
                ),
            )
        outcome = scanner.import_candidates(FURNISHED_FINDER_PLATFORM, search_url, listings)
        if outcome.already_running:
            raise HTTPException(status_code=409, detail="A regular scan is already running. Try again in a moment.")
        if outcome.status == "profile_required":
            raise HTTPException(status_code=409, detail="Finish Your deal before importing Furnished Finder cards.")
        if outcome.sources_failed:
            repository.set_connector_state(
                "furnished_finder",
                "degraded",
                message="The local monitor could not save this Furnished Finder import.",
                attempted=True,
            )
            raise HTTPException(status_code=500, detail="The local monitor could not save this Furnished Finder import.")
        repository.set_connector_state(
            "furnished_finder",
            "working" if outcome.listings_seen else "working_zero",
            message=(
                f"Imported {outcome.listings_seen} supported Furnished Finder card(s)."
                if outcome.listings_seen
                else "The bridge checked successfully but found no supported cards."
            ),
            observed_items=outcome.listings_seen,
            configured=True,
            attempted=True,
            succeeded=True,
        )
        return JSONResponse(
            {
                "seen": outcome.listings_seen,
                "added": outcome.listings_added,
                "updated": outcome.listings_updated,
                "status": outcome.status,
            }
        )

    @application.post("/alerts/gmail/client-secret")
    def upload_gmail_client_secret(client_secret: UploadFile = File(...)):
        contents = client_secret.file.read(1_000_001)
        if len(contents) > 1_000_000:
            return RedirectResponse("/alerts?error=OAuth+client+file+is+too+large", status_code=303)
        try:
            gmail_mailbox.save_client_secret(contents)
        except GmailAlertError as exc:
            return RedirectResponse(f"/alerts?error={quote(str(exc))}", status_code=303)
        repository.set_connector_state(
            "gmail",
            "configured_unverified",
            message="Google OAuth client saved locally. Continue to read-only authorization.",
            configured=True,
        )
        return RedirectResponse(
            "/alerts?message=OAuth+client+saved%3B+you+can+now+connect+Gmail",
            status_code=303,
        )

    def configured_gmail_sources() -> list[ListingSource]:
        selected: list[ListingSource] = []
        for source in active_sources:
            if isinstance(source, GmailHousingAlertSource):
                selected.append(source)
            elif isinstance(source, FacebookMarketplaceSource):
                selected.append(source.alerts)
        ordered = {name: index for index, (_, name) in enumerate(GMAIL_PROVIDERS)}
        return sorted(selected, key=lambda source: ordered.get(source.platform, 99))

    @application.post("/alerts/gmail/test")
    def test_gmail_connector():
        # Either backend can be the live one, so the gate asks the router.
        if not mailbox.is_connected:
            state = (
                "authorization_expired"
                if gmail_mailbox.token_path.is_file()
                else "configured_unverified"
            )
            repository.set_connector_state(
                "gmail",
                state,
                message="Connect an email account before testing saved-search alerts.",
                configured=gmail_mailbox.has_client_secret,
                attempted=True,
            )
            return RedirectResponse(
                "/alerts?error=Connect+an+email+account+before+testing+saved-search+alerts",
                status_code=303,
            )
        if scanner.is_running:
            return RedirectResponse("/?message=Scan+already+running", status_code=303)
        gmail_sources = configured_gmail_sources()
        if not gmail_sources:
            return RedirectResponse(
                "/alerts?error=The+packaged+Gmail+providers+are+missing%3B+run+Repair",
                status_code=303,
            )
        for source in gmail_sources:
            repository.set_connector_state(
                source.connector_state_key,
                "checking",
                message=f"Testing {source.platform} saved-search alerts.",
                configured=True,
                attempted=True,
            )
        scanner.refresh_gmail_connector_state()
        if not scanner.start_scan("gmail_test", sources=gmail_sources):
            for source in gmail_sources:
                repository.set_connector_state(
                    source.connector_state_key,
                    "configured_unverified",
                    message=f"{source.platform} test did not start. Retry after the active scan finishes.",
                    configured=True,
                )
            scanner.refresh_gmail_connector_state()
            return RedirectResponse("/?message=Scan+already+running", status_code=303)
        return RedirectResponse(
            "/?message=Gmail+alert+test+started%3B+each+provider+will+report+its+own+result",
            status_code=303,
        )

    @application.post("/alerts/gmail/disconnect")
    def disconnect_gmail():
        revoked = gmail_mailbox.disconnect(revoke=True)
        for key, name in GMAIL_PROVIDERS:
            repository.set_connector_state(
                key,
                "disabled",
                message=f"{name} email coverage is paused until Gmail is connected again.",
            )
        repository.set_connector_state(
            "gmail",
            "configured_unverified" if gmail_mailbox.has_client_secret else "not_configured",
            message=(
                "Google access was revoked and the local token was removed. Connect again when wanted."
                if revoked
                else "The local Gmail token was removed. Google revocation could not be confirmed; reconnecting creates a fresh authorization."
            ),
            configured=gmail_mailbox.has_client_secret,
            attempted=True,
        )
        return RedirectResponse(
            "/alerts?message=Gmail+disconnected%3B+public+sources+continue+working",
            status_code=303,
        )

    @application.post("/alerts/email/connect")
    async def connect_email(request: Request):
        """Save an app password, then prove it works before claiming success."""
        form = await request.form()
        address = str(form.get("address") or "")
        password = str(form.get("password") or "")
        host = str(form.get("host") or "").strip()
        try:
            imap_mailbox.save_credential(address, password, host or None)
            # A saved credential is not a working one. Do one bounded, read-only
            # search now so the user is told the truth immediately.
            imap_mailbox.messages("newer_than:1d", max_results=1)
        except ImapAlertError as exc:
            # Never leave a credential behind that does not work.
            imap_mailbox.disconnect()
            repository.set_connector_state(
                "gmail",
                "not_configured",
                message=str(exc),
                attempted=True,
            )
            return RedirectResponse("/alerts?error=" + quote(str(exc)), status_code=303)

        for key, name in GMAIL_PROVIDERS:
            repository.set_connector_state(
                key,
                "configured_unverified",
                message=f"{name} alerts will be imported from {imap_mailbox.credential.address}.",
                configured=True,
            )
        repository.set_connector_state(
            "gmail",
            "configured_unverified",
            message=f"Reading alert email from {imap_mailbox.credential.address} over IMAP.",
            configured=True,
            attempted=True,
        )
        return RedirectResponse(
            "/alerts?message=" + quote(
                "Email connected. Zillow, HotPads, Apartments.com and Roomies alerts will import on the next check."
            ),
            status_code=303,
        )

    @application.post("/alerts/email/disconnect")
    def disconnect_email():
        removed = imap_mailbox.disconnect()
        for key, name in GMAIL_PROVIDERS:
            repository.set_connector_state(
                key,
                "disabled",
                message=f"{name} email coverage is paused until an account is connected again.",
            )
        repository.set_connector_state(
            "gmail",
            "not_configured",
            message=(
                "The saved app password was removed from this computer."
                if removed
                else "No saved app password was found."
            ),
            attempted=True,
        )
        return RedirectResponse(
            "/alerts?message=" + quote("Email disconnected; the free public sources keep working."),
            status_code=303,
        )

    @application.get("/alerts/gmail/connect")
    def connect_gmail(request: Request):
        callback_url = str(request.url_for("gmail_callback"))
        try:
            authorization_url = gmail_mailbox.begin_authorization(callback_url)
        except GmailAlertError as exc:
            repository.set_connector_state(
                "gmail",
                "configured_unverified" if gmail_mailbox.has_client_secret else "not_configured",
                message=str(exc),
                configured=gmail_mailbox.has_client_secret,
                attempted=True,
            )
            return RedirectResponse(f"/alerts?error={quote(str(exc))}", status_code=303)
        repository.set_connector_state(
            "gmail", "checking", message="Waiting for Google authorization.", attempted=True
        )
        return RedirectResponse(authorization_url, status_code=302)

    @application.get("/alerts/gmail/callback", name="gmail_callback")
    def gmail_callback(request: Request, state: str | None = None, code: str | None = None, error: str | None = None):
        if error:
            repository.set_connector_state(
                "gmail",
                "configured_unverified",
                message="Google did not grant read-only access. Connect again when ready.",
                configured=gmail_mailbox.has_client_secret,
                attempted=True,
            )
            return RedirectResponse(
                "/alerts?error=Google+did+not+grant+read-only+access%3B+connect+again+when+ready",
                status_code=303,
            )
        try:
            gmail_mailbox.complete_authorization(str(request.url_for("gmail_callback")), state, code)
        except GmailAlertError as exc:
            repository.set_connector_state(
                "gmail",
                "authorization_expired",
                message=str(exc),
                attempted=True,
            )
            return RedirectResponse(f"/alerts?error={quote(str(exc))}", status_code=303)
        repository.set_connector_state(
            "gmail",
            "configured_unverified",
            message="Gmail is authorized. Run one bounded saved-search test to verify each provider.",
            configured=True,
            attempted=True,
        )
        return RedirectResponse(
            "/alerts?message=Gmail+connected%3B+run+the+bounded+saved-search+alert+test",
            status_code=303,
        )

    @application.get("/health")
    def health():
        scans = repository.recent_scans(8)
        health_state = schedule_health(
            scheduler,
            scans,
            scan_running=scanner.is_running,
            managed=enable_scheduler,
        )
        return JSONResponse(
            {
                # "ok" keeps its original meaning: this process is up and serving.
                # The installer and the Open/Repair tools poll it with `curl -fsS`
                # and require ok to be true, so degrading it would turn a merely
                # unscheduled install into a failed one, which is a worse outcome
                # than installing and flagging the schedule.
                "ok": True,
                "app": "sf-housing-monitor",
                "version": __version__,
                "scan_running": scanner.is_running,
                "schedule": ["10:00 America/Los_Angeles", "18:00 America/Los_Angeles"],
                # Whether checking is actually being kept is a different question
                # from whether the server answers, and it is observed, not asserted.
                "scheduled_checking": health_state.as_dict(),
                "last_scan": scans[0] if scans else None,
            }
        )

    @application.get("/scan/status")
    def scan_status():
        progress = scanner.progress
        progress["typical_seconds"] = _typical_scan_seconds(
            repository.recent_scans(20), str(progress.get("trigger") or "manual")
        )
        progress["maximum_seconds"] = max(1, round(scanner.max_scan_seconds))
        return JSONResponse(progress)

    return application


app = create_app()
