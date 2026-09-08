from __future__ import annotations

import logging
import json
import queue
import threading
import time
from collections.abc import Callable
from typing import Any
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx

from .classification import classify_listing
from .connectors import GMAIL_PROVIDERS, aggregate_gmail_status, connector_state_for_error
from .coverage import coverage_is_fresh, fetch_count, missing_coverage_targets
from .database import Repository, utc_now
from .filelock import release as release_file_lock, try_acquire as try_acquire_file_lock
from .freshness import source_is_in_backoff, source_key as watchdog_source_key
from .models import ListingCandidate, ScanOutcome
from .preferences import Preferences
from .scoring import score_listing
from .sources import (
    ListingSource,
    SourceError,
    facebook_coordinate_neighborhood,
    visible_sf_area_hint,
)


LOGGER = logging.getLogger(__name__)

# Which runs the app started by itself. Two places used to spell this set out
# separately -- which sources a run may touch, and whether a failing source is
# allowed its cooldown -- so adding a third kind of automatic run meant finding
# both or silently getting a lesser scan than the one being caught up.
AUTOMATIC_TRIGGERS = frozenset({"scheduled", "startup_catchup", "catch_up", "deep_sweep"})

# The nightly run that reads the sources which cannot be read deeply every time.
# Most sources are simply read in full on every scan: measured end to end that
# costs about 150 seconds twice a day, which is nothing. Two are different.
# Trulia and Redfin answer 403 and 202 once they have had enough, and a refused
# source returns nothing at all -- so reading either of them to the bottom is
# worth doing once a day at an hour nobody is waiting, and not worth doing at
# 10:00 when a shallow read that works is better than a deep one that is
# turned away.
DEEP_SWEEP_TRIGGER = "deep_sweep"
# A connector test is a person asking, but it still has to reach the sources a
# schedule would, or the thing they are testing is not the thing that runs.
FULL_SOURCE_TRIGGERS = AUTOMATIC_TRIGGERS | {"connector_test"}

# Rechecking is bounded by time, not by a count. A fixed six-per-source meant a
# sixty-home shortlist took days to cycle, so "no stale results" was a direction
# rather than a promise. The scan finishes well inside its allowance, and the
# leftover time is spent confirming homes instead of being given back.
#
# Every source still gets a fair share of what is left, so one slow source
# cannot spend the whole allowance and starve the rest, and each is guaranteed a
# small floor so it always makes progress even when the share is thin.
RECHECK_FLOOR_PER_SOURCE = 3
# The longest any one source may hold the scan. An HTTP read timeout bounds
# each chunk of a response, not the whole of it, so a server that trickles
# bytes keeps its connection open for as long as it likes: AvalonBay took 200
# seconds over a single request that normally takes two, and the sixteen
# sources queued behind it were all skipped to keep the scan inside its limit.
# Set above the slowest healthy source rather than near it -- Craigslist reads
# several searches and their detail pages and wants about a minute -- so this
# only ever catches a source that has stopped behaving.
SOURCE_HARD_CEILING_SECONDS = 75.0

RECHECK_HARD_CEILING = 250

# Scans run at 10:00 and 18:00, so the gaps are eight hours and sixteen. The
# window has to be shorter than the shorter gap, or a home that goes quiet is
# passed over for a whole cycle and can reach a full day unconfirmed. At seven
# hours every scan re-examines anything the previous one did not confirm, which
# puts the worst case at sixteen hours rather than twenty-four.
RECHECK_AFTER = timedelta(hours=7)

# What the reader is told. A home nobody has confirmed for a day is not a home
# the app can vouch for, whatever its score says.
CONFIRMATION_STALE_AFTER = timedelta(hours=24)

# How far back the one-off `initial_discovery` scan reaches when a profile
# first goes active, so a new install opens on real listings instead of an
# empty dashboard. Listings with no parseable timestamp are kept regardless.
INITIAL_DISCOVERY_WINDOW = timedelta(days=7)


class Scanner:
    def __init__(
        self,
        repository: Repository,
        preference_loader: Callable[[], Preferences],
        sources: list[ListingSource],
        timeout_seconds: float = 15.0,
        detail_delay_seconds: float = 0.25,
        max_scan_seconds: float = 110.0,
    ):
        self.repository = repository
        self.preference_loader = preference_loader
        self.sources = sources
        self.timeout_seconds = timeout_seconds
        self.detail_delay_seconds = detail_delay_seconds
        self.max_scan_seconds = max_scan_seconds
        self._scan_lock = threading.Lock()
        self._process_lock_handle = None
        self._progress_lock = threading.Lock()
        self._progress_state: dict[str, object] = {
            "status": "idle",
            "running": False,
            "run_id": None,
            "trigger": None,
            "started_monotonic": None,
            "elapsed_seconds": 0,
            "sources_total": 0,
            "sources_completed": 0,
            "current_source": None,
            "listings_seen": 0,
            "listings_added": 0,
            "sources_failed": 0,
            # What the bar is really measuring. Counting sources, Craigslist
            # and Listings Project are a twenty-third each, so the bar shows
            # nothing for the 75 seconds the first one takes and then jumps
            # four points. Weighted by how long each source usually takes, it
            # moves at the rate the scan is actually progressing.
            "weight_total": 0.0,
            "weight_done": 0.0,
            "weight_current": 0.0,
            "current_started_monotonic": None,
        }

    @property
    def is_running(self) -> bool:
        return self._scan_lock.locked()

    @property
    def progress(self) -> dict[str, object]:
        """Return a JSON-safe snapshot of the active or most recent scan."""
        with self._progress_lock:
            snapshot = dict(self._progress_state)
        started = snapshot.pop("started_monotonic", None)
        if snapshot["running"] and isinstance(started, (int, float)):
            snapshot["elapsed_seconds"] = max(0, int(time.monotonic() - started))
        weight_total = float(snapshot.pop("weight_total", 0.0) or 0.0)
        weight_done = float(snapshot.pop("weight_done", 0.0) or 0.0)
        weight_current = float(snapshot.pop("weight_current", 0.0) or 0.0)
        current_started = snapshot.pop("current_started_monotonic", None)
        completed = int(snapshot["sources_completed"])
        total = int(snapshot["sources_total"])
        if snapshot["status"] in {"completed", "completed_with_errors"}:
            percent = 100
        elif weight_total > 0:
            # The source in flight counts for the share of itself it has had
            # time to do, so the bar keeps moving through a slow one instead
            # of waiting for it to finish. Capped just under its own weight:
            # a source running longer than usual must not borrow from the
            # next one and show progress that has not happened.
            running = 0.0
            if snapshot["running"] and weight_current > 0 and isinstance(current_started, (int, float)):
                spent = max(0.0, time.monotonic() - current_started)
                running = min(spent / weight_current, 0.95) * weight_current
            percent = round(((weight_done + running) / weight_total) * 100)
        elif total:
            percent = round((completed / total) * 100)
        else:
            percent = 0
        snapshot["percent"] = max(0, min(100, percent))
        return snapshot

    def _eligible_sources(self, trigger: str) -> list[ListingSource]:
        include_scheduled_only = trigger in FULL_SOURCE_TRIGGERS
        eligible = [
            source
            for source in self.sources
            if (
                include_scheduled_only
                or not getattr(source, "scheduled_only", False)
                or (trigger == "manual" and getattr(source, "manual_scan_enabled", False))
            )
        ]
        if trigger == "initial_discovery":
            eligible = [
                source
                for source in eligible
                if not self.repository.source_initialized(self._source_key(source))
            ]
        # Browser-backed third-party helpers can take tens of seconds even when
        # healthy. Keep the fast direct and inbox sources first so a delayed
        # Facebook helper can never prevent Craigslist, SpareRoom, or saved
        # search alerts from running inside the shared time budget.
        priority = {
            "Craigslist": 10,
            # One public JSON call, so it costs almost nothing to run early.
            "SF Housing Portal": 12,
            "Listings Project": 15,
            "Abacus (small buildings)": 20,
            # A handful of small JSON posts and no detail reads.
            "RentSFNow": 22,
            "AvalonBay": 24,
            "AppFolio": 26,
            "UDR": 27,
            "SpareRoom": 25,
            # A student board: one small page per scan, no detail reads.
            "Uloop": 28,
            "HotPads": 35,
            "Apartments.com": 40,
            "Zumper": 45,
            # Six search pages of about 700KB, no detail reads, and the most
            # reliable of the large portals -- six of six fetches answered
            # while it was being measured, including to an honest User-Agent.
            "Zillow": 44,
            # Two search pages and no detail reads, and the source most
            # likely to be turned away, so it is asked early while the
            # scan still has time to record the refusal.
            "Trulia": 46,
            "Redfin": 47,
            # Four search pages and no detail reads, so it costs about what
            # Redfin does and runs well before the sources that fetch a page
            # per building.
            "ApartmentGuide": 48,
            # Six search pages and no detail reads, and the deepest
            # inventory of the direct sources, so it is worth the pages.
            "Movoto": 49,
            "Roomies": 50,
            # A search page plus a detail page per building, like Rent.com.
            "Apartment List": 52,
            # Heaviest of the direct sources: a search page plus a detail page
            # per building, so it runs after the ones that read a single page.
            "Rent.com": 55,
            "Furnished Finder": 60,
            "Facebook Marketplace": 70,
            "Facebook Groups": 80,
        }
        return sorted(eligible, key=lambda source: (priority.get(source.platform, 60), source.platform.casefold()))

    @staticmethod
    def _source_key(source: ListingSource) -> str:
        return watchdog_source_key(source)

    @staticmethod
    def _provider(source: ListingSource) -> str:
        return str(
            getattr(source, "last_provider", getattr(source, "provider", source.__class__.__name__))
        )

    @staticmethod
    def _connector_key(source: ListingSource) -> str | None:
        value = getattr(source, "connector_key", None)
        return str(value) if value else None

    @staticmethod
    def _connector_state_key(source: ListingSource) -> str | None:
        value = getattr(source, "connector_state_key", None)
        if value:
            return str(value)
        return Scanner._connector_key(source)

    def refresh_gmail_connector_state(self) -> None:
        provider_states = {
            key: state
            for key, _ in GMAIL_PROVIDERS
            if (state := self.repository.connector_state(key)) is not None
        }
        state, message, observed, providers = aggregate_gmail_status(provider_states)
        self.repository.set_connector_state(
            "gmail",
            state,
            message=message,
            observed_items=observed,
            metadata={"providers": providers},
            configured=True,
            attempted=state != "configured_unverified",
            succeeded=state in {"working", "working_zero", "waiting_first_alert"},
        )

    @staticmethod
    def _within_initial_window(
        listing: ListingCandidate,
        *,
        now: datetime | None = None,
    ) -> bool:
        raw = listing.metadata.get("listing_timestamp")
        if not isinstance(raw, str) or not raw.strip():
            return True
        try:
            published = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return True
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        return published >= (now or datetime.now(UTC)) - INITIAL_DISCOVERY_WINDOW

    def _measure_missing_coverage(
        self, client: httpx.Client, preferences: Preferences
    ) -> None:
        """Record how many homes each disconnected source is holding.

        "Waiting for first alert" never says what the missing setup costs, so
        the chore has no stated payoff. A real number gives it one -- and only a
        real one: everything here answers None rather than guess, and a count
        that cannot be taken simply is not stored, leaving the page to show its
        invitation without one.

        Every failure is swallowed. This runs inside a scan whose job is
        collecting homes, and must never be the reason one fails.
        """
        try:
            targets = missing_coverage_targets(self.repository, preferences)
        except Exception:
            LOGGER.warning("Could not work out which sources to measure", exc_info=True)
            return
        try:
            stored = self.repository.source_coverage()
        except Exception:
            stored = {}
        for platform, url in targets:
            existing = stored.get(platform) or {}
            if coverage_is_fresh(existing.get("taken_at")):
                continue
            try:
                count = fetch_count(client, platform, url)
            except Exception:
                # A silent failure here would look identical to a source that
                # simply cannot be counted, which is the one thing that must
                # stay distinguishable.
                LOGGER.warning("Could not count %s", platform, exc_info=True)
                continue
            if count is None:
                LOGGER.info("%s could not be counted; showing no figure for it", platform)
                continue
            try:
                self.repository.record_source_coverage(platform, count)
                LOGGER.info("%s is holding %s listings", platform, count)
            except Exception:
                LOGGER.warning("Could not store the %s count", platform, exc_info=True)

    def _search_within_ceiling(
        self,
        source: ListingSource,
        client: httpx.Client,
        preferences: Preferences,
        trigger: str,
        *,
        deadline: float,
    ) -> list[ListingCandidate]:
        """Search a source, and stop waiting on it if it stops answering.

        A stalled source cannot be interrupted from outside: it is sitting in a
        socket read, and the read timeout applies to each chunk rather than to
        the whole response, so a server sending one byte at a time holds the
        connection for as long as it likes. Left alone that is not one slow
        source, it is every source after it -- one 200-second stall skipped
        sixteen of them in a single scan.

        So the search runs on a worker thread the scan can walk away from. The
        stalled source loses its own results and reports why; nothing else
        loses anything. The thread is a daemon and shares the client, which
        httpx supports, so an abandoned read finishes into a queue nobody is
        listening to and the interpreter can still exit.
        """
        def run() -> tuple[str, Any]:
            trigger_search = getattr(source, "search_for_trigger", None)
            return (
                trigger_search(client, preferences, trigger)
                if callable(trigger_search)
                else source.search(client, preferences)
            )

        # Never longer than the scan has left, and never so short that a
        # healthy source is cut off by a ceiling meant for a broken one.
        remaining = deadline - time.monotonic()
        ceiling = min(SOURCE_HARD_CEILING_SECONDS, max(self.timeout_seconds, remaining))
        outcome: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                outcome.put(("listings", run()))
            except BaseException as error:  # reported on the scan's thread
                outcome.put(("error", error))

        thread = threading.Thread(
            target=worker, name=f"search-{source.platform}", daemon=True
        )
        thread.start()
        try:
            kind, payload = outcome.get(timeout=ceiling)
        except queue.Empty:
            raise SourceError(
                f"{source.platform} stopped answering partway through and was left after "
                f"{int(ceiling)} seconds, so the rest of this scan could still run. "
                "The next check tries it again."
            ) from None
        if kind == "error":
            raise payload
        return payload

    def _recheck_absent(
        self,
        client: httpx.Client,
        source: ListingSource,
        preferences: Preferences,
        *,
        seen_source_ids: set[str],
        deadline: float,
        sources_remaining: int = 1,
    ) -> int:
        """Re-confirm shortlisted homes this source has stopped returning.

        Bounded by time rather than by a count, so the shortlist is covered in a
        day instead of a week. The page is what decides: a source that says the
        post is gone already sets ``verified_inactive`` and scoring already
        refuses it. Everything else is left exactly as it was -- a fetch that
        fails proves nothing, and neither does silence.

        ``sources_remaining`` includes this source, so the share it may spend is
        what is left divided by how many sources still have to run. One slow
        source therefore cannot spend the whole allowance, and the floor below
        keeps a thin share from meaning no progress at all.
        """
        if not hasattr(source, "enrich"):
            return 0
        now = time.monotonic()
        available = deadline - now - self.timeout_seconds
        if available <= 0:
            return 0
        share = available / max(1, int(sources_remaining))
        recheck_deadline = now + share
        floor = max(0, int(getattr(source, "recheck_floor", RECHECK_FLOOR_PER_SOURCE)))
        ceiling = max(0, int(getattr(source, "recheck_budget", RECHECK_HARD_CEILING)))
        if ceiling <= 0:
            return 0

        try:
            candidates = self.repository.shortlisted_absent_from_search(
                source.platform,
                seen_source_ids,
                preferences.minimum_score,
                limit=ceiling,
                recheck_after=RECHECK_AFTER,
            )
        except Exception:  # a recheck must never cost the scan its results
            LOGGER.warning("%s recheck lookup failed", source.platform, exc_info=True)
            return 0

        checked = 0
        for listing_id, candidate in candidates:
            # The floor is what a source is owed regardless of its share; past
            # that it stops at its share, and never past the scan's own deadline.
            limit = deadline if checked < floor else min(recheck_deadline, deadline)
            if time.monotonic() + self.timeout_seconds > limit:
                break
            try:
                refreshed = source.enrich(client, candidate)
            except Exception as exc:
                # Unreachable is not gone. Leave the home exactly as it was, and
                # leave its confirmation date alone so it is tried again rather
                # than counted as checked.
                LOGGER.info(
                    "%s could not recheck %s: %s", source.platform, candidate.original_url, exc
                )
                continue
            metadata = dict(refreshed.metadata)
            metadata["last_verified_at"] = utc_now()
            refreshed = replace(classify_listing(refreshed), metadata=metadata)
            try:
                self.repository.update_score(
                    listing_id, score_listing(refreshed, preferences), listing=refreshed
                )
            except Exception:
                LOGGER.warning("%s recheck could not be stored", source.platform, exc_info=True)
                continue
            checked += 1
            if self.detail_delay_seconds:
                time.sleep(self.detail_delay_seconds)
        return checked

    @staticmethod
    def _merge_stored(
        listing: ListingCandidate, existing: Any, stored_metadata: dict[str, Any]
    ) -> ListingCandidate:
        """Keep richer stored fields when a thin search card would overwrite them.

        Detail pages carry more than search cards, so a later scan must not erase
        what an earlier enrichment learned.
        """
        return replace(
            listing,
            price=listing.price if listing.price is not None else existing["price"],
            neighborhood=listing.neighborhood or existing["neighborhood"],
            summary=existing["summary"] or listing.summary,
            listing_type=(
                existing["listing_type"]
                if existing["listing_type"] not in (None, "", "Room/share")
                else listing.listing_type
            ),
            metadata={**stored_metadata, **listing.metadata},
        )

    def _begin_progress(self, trigger: str, sources: list[ListingSource] | None = None) -> None:
        with self._progress_lock:
            self._progress_state.update(
                {
                    "status": "running",
                    "running": True,
                    "run_id": None,
                    "trigger": trigger,
                    "started_monotonic": time.monotonic(),
                    "elapsed_seconds": 0,
                    "sources_total": len(sources) if sources is not None else len(self._eligible_sources(trigger)),
                    "sources_completed": 0,
                    "current_source": None,
                    "listings_seen": 0,
                    "listings_added": 0,
                    "sources_failed": 0,
                }
            )

    def _source_weights(self, sources: list[ListingSource]) -> dict[str, float]:
        """How long each source in this scan is expected to take.

        Read from what each one actually did on its own recent runs. A source
        nobody has timed yet is given the middle of what is known rather than
        nothing, so an unmeasured source does not silently weigh zero and let
        the bar reach 100% with work still to do.
        """
        try:
            measured = self.repository.typical_source_seconds()
        except Exception:  # progress must never cost a scan its results
            LOGGER.warning("could not read source durations for progress", exc_info=True)
            measured = {}
        known = sorted(measured.values())
        fallback = known[len(known) // 2] if known else 5.0
        return {
            source.platform: float(measured.get(source.platform, fallback))
            for source in sources
        }

    def _finish_source_progress(
        self, source_index: int, weight: float, **values: object
    ) -> None:
        """Retire a source from the bar, however it ended.

        Called for a skip and a failure as well as a success: a source that
        stops without its weight being retired leaves the bar stuck for the
        rest of the scan, which is the failure this replaced.
        """
        with self._progress_lock:
            done = float(self._progress_state.get("weight_done", 0.0) or 0.0)
            self._progress_state.update(
                {
                    **values,
                    "sources_completed": source_index,
                    "current_source": None,
                    "weight_done": done + max(0.0, float(weight)),
                    "weight_current": 0.0,
                    "current_started_monotonic": None,
                }
            )

    def _update_progress(self, **values: object) -> None:
        with self._progress_lock:
            self._progress_state.update(values)

    def _finish_progress(self, status: str) -> None:
        with self._progress_lock:
            started = self._progress_state.get("started_monotonic")
            elapsed = int(time.monotonic() - started) if isinstance(started, (int, float)) else 0
            self._progress_state.update(
                {
                    "status": status,
                    "running": False,
                    "current_source": None,
                    "elapsed_seconds": max(0, elapsed),
                }
            )

    def start_scan(self, trigger: str = "manual", sources: list[ListingSource] | None = None) -> bool:
        """Start a scan in a daemon thread after acquiring the overlap lock.

        Acquiring before the HTTP response is sent lets the dashboard reliably show
        that a user-requested scan is in progress, instead of briefly appearing idle.
        """
        if not self.preference_loader().profile_active:
            self._begin_progress(trigger, sources or [])
            self._finish_progress("profile_required")
            return False
        if not self._acquire_scan_locks():
            return False
        self._begin_progress(trigger, sources)
        thread = threading.Thread(
            target=self._run_locked_scan,
            args=(trigger, sources),
            name=f"sf-housing-{trigger}-scan",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            self._finish_progress("failed")
            self._release_scan_locks()
            raise
        return True

    def run_scan(self, trigger: str = "manual", sources: list[ListingSource] | None = None) -> ScanOutcome:
        if not self.preference_loader().profile_active:
            self._begin_progress(trigger, sources or [])
            self._finish_progress("profile_required")
            return ScanOutcome(0, "profile_required")
        if not self._acquire_scan_locks():
            run_id = self.repository.begin_scan(trigger)
            self.repository.finish_scan(run_id, "skipped", message="Another scan is already running.")
            LOGGER.warning("Skipped %s scan because another scan is running", trigger)
            return ScanOutcome(run_id, "skipped", already_running=True)

        self._begin_progress(trigger, sources)
        return self._run_locked_scan(trigger, sources)

    def import_candidates(
        self,
        platform: str,
        search_url: str,
        listings: list[ListingCandidate],
        *,
        trigger: str = "browser_bridge",
    ) -> ScanOutcome:
        """Score and store browser-collected cards under the normal scan lock.

        A browser bridge is deliberately treated as another source run, not as a
        side channel to SQLite.  That makes its status visible in the dashboard
        and prevents it from racing an ordinary scheduled scan.
        """
        if not self.preference_loader().profile_active:
            return ScanOutcome(0, "profile_required")
        if not self._acquire_scan_locks():
            return ScanOutcome(0, "skipped", already_running=True)

        self._begin_progress(trigger, [])
        self._update_progress(sources_total=1, current_source=platform)
        run_id: int | None = None
        source_run_id: int | None = None
        seen = added = updated = 0
        final_status = "failed"
        try:
            run_id = self.repository.begin_scan(trigger)
            self._update_progress(run_id=run_id)
            source_run_id = self.repository.begin_source_run(
                run_id,
                platform,
                search_url,
                provider="browser_bridge",
                source_key=f"browser_bridge:{platform}",
            )
            preferences = self.preference_loader()

            for listing in listings:
                seen += 1
                existing = self.repository.find_listing(
                    listing.platform, listing.source_id, listing.original_url
                )
                if existing is not None:
                    stored_metadata = json.loads(existing["metadata_json"] or "{}")
                    listing = replace(
                        listing,
                        price=listing.price if listing.price is not None else existing["price"],
                        neighborhood=listing.neighborhood or existing["neighborhood"],
                        summary=listing.summary or existing["summary"],
                        listing_type=listing.listing_type or existing["listing_type"],
                        metadata={**stored_metadata, **listing.metadata},
                    )
                listing = classify_listing(listing)
                result = score_listing(listing, preferences)
                matched_neighborhood = result.details.get("neighborhood", {}).get("match_label")
                if matched_neighborhood:
                    listing = replace(listing, neighborhood=str(matched_neighborhood))
                _, created = self.repository.upsert_listing(listing, result)
                if created:
                    added += 1
                else:
                    updated += 1
                self._update_progress(listings_seen=seen, listings_added=added)

            self.repository.finish_source_run(source_run_id, "success", seen=seen, added=added)
            self.repository.finish_scan(
                run_id,
                "completed",
                seen=seen,
                added=added,
                updated=updated,
            )
            self._update_progress(sources_completed=1, current_source=None)
            final_status = "completed"
            LOGGER.info("Imported %s browser cards from %s: %s new", seen, platform, added)
            return ScanOutcome(
                run_id,
                final_status,
                listings_seen=seen,
                listings_added=added,
                listings_updated=updated,
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if source_run_id is not None:
                self.repository.finish_source_run(
                    source_run_id, "error", seen=seen, added=added, message=message[:1000]
                )
            if run_id is not None:
                self.repository.finish_scan(
                    run_id,
                    "completed_with_errors",
                    seen=seen,
                    added=added,
                    updated=updated,
                    failed=1,
                    message=message[:1000],
                )
            self._update_progress(sources_completed=1, current_source=None, sources_failed=1)
            final_status = "completed_with_errors"
            LOGGER.exception("Browser bridge import from %s failed", platform)
            return ScanOutcome(
                run_id or 0,
                "completed_with_errors",
                listings_seen=seen,
                listings_added=added,
                listings_updated=updated,
                sources_failed=1,
            )
        finally:
            self._finish_progress(final_status)
            self._release_scan_locks()

    def _acquire_scan_locks(self) -> bool:
        if not self._scan_lock.acquire(blocking=False):
            return False
        lock_path = self.repository.path.parent / "scan.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            acquired = try_acquire_file_lock(handle)
        except OSError:
            handle.close()
            self._scan_lock.release()
            raise
        if not acquired:
            handle.close()
            self._scan_lock.release()
            return False
        self._process_lock_handle = handle
        return True

    def _release_scan_locks(self) -> None:
        handle = self._process_lock_handle
        self._process_lock_handle = None
        if handle is not None:
            release_file_lock(handle)
            handle.close()
        self._scan_lock.release()

    def _run_locked_scan(self, trigger: str, sources: list[ListingSource] | None = None) -> ScanOutcome:
        """Run a scan while the caller holds ``_scan_lock``."""
        run_id: int | None = None
        deadline = time.monotonic() + self.max_scan_seconds
        scan_started_at = utc_now()
        total_seen = total_added = total_updated = sources_failed = 0
        final_status = "failed"
        try:
            run_id = self.repository.begin_scan(trigger)
            self._update_progress(run_id=run_id)
            LOGGER.info("Starting %s scan (run %s)", trigger, run_id)
            preferences = self.preference_loader()
            headers = {
                "User-Agent": "SFHousingMonitor/0.1 (local personal-use monitor)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.8",
            }
            timeout = httpx.Timeout(self.timeout_seconds, connect=min(5.0, self.timeout_seconds))
            limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
            with httpx.Client(headers=headers, timeout=timeout, limits=limits, follow_redirects=True) as client:
                active_sources = sources if sources is not None else self._eligible_sources(trigger)
                weights = self._source_weights(active_sources)
                self._update_progress(
                    weight_total=sum(weights.values()), weight_done=0.0
                )
                for source_index, source in enumerate(active_sources, start=1):
                    self._update_progress(
                        current_source=source.platform,
                        weight_current=weights.get(source.platform, 0.0),
                        current_started_monotonic=time.monotonic(),
                    )
                    source_key = self._source_key(source)
                    source_run_id = self.repository.begin_source_run(
                        run_id,
                        source.platform,
                        source.search_url,
                        self._provider(source),
                        source_key,
                    )
                    if source.mode != "automatic":
                        self.repository.finish_source_run(
                            source_run_id,
                            source.mode,
                            message=source.manual_reason,
                            source_key=self._source_key(source),
                        )
                        self._finish_source_progress(source_index, weights.get(source.platform, 0.0))
                        continue
                    # A source that failed repeatedly gets a short, persisted
                    # automatic cooldown.  This avoids hammering a broken
                    # external page after wake/restart, while an explicit user
                    # check remains an intentional one-time retry.
                    if trigger in AUTOMATIC_TRIGGERS:
                        backoff = source_is_in_backoff(self.repository, source)
                        if backoff is not None:
                            self.repository.finish_source_run(
                                source_run_id,
                                "backoff",
                                message=backoff.action,
                                provider=self._provider(source),
                                source_key=self._source_key(source),
                            )
                            LOGGER.warning(
                                "%s source deferred during automatic backoff after %s failures",
                                source.platform,
                                backoff.failure_streak,
                            )
                            self._finish_source_progress(source_index, weights.get(source.platform, 0.0))
                            continue
                    if time.monotonic() + self.timeout_seconds > deadline:
                        self.repository.finish_source_run(
                            source_run_id,
                            "skipped",
                            message=(
                                f"Skipped to keep this scan within the {int(self.max_scan_seconds)}-second "
                                "time limit."
                            ),
                            source_key=self._source_key(source),
                        )
                        self._finish_source_progress(source_index, weights.get(source.platform, 0.0))
                        continue

                    source_seen = source_added = source_updated = detail_failures = 0
                    source_fetched = source_parsed = source_classified = source_deduplicated = 0
                    source_hard_filtered = source_active = source_archived = 0
                    try:
                        listings = self._search_within_ceiling(
                            source, client, preferences, trigger, deadline=deadline
                        )
                        source_fetched = len(listings)
                        source_parsed = len(listings)
                        if trigger == "initial_discovery":
                            listings = [
                                listing for listing in listings if self._within_initial_window(listing)
                            ]
                        detail_budget = max(0, int(source.detail_budget))

                        # Pass 1: resolve stored state and score provisionally,
                        # without touching the network. Spending the detail budget
                        # in arrival order meant the first ten results consumed it
                        # regardless of quality, so most homes a user actually sees
                        # never got the posting date that lives on a detail page.
                        prepared: list[dict[str, Any]] = []
                        for listing in listings:
                            source_seen += 1
                            total_seen += 1
                            existing = self.repository.find_listing(
                                listing.platform, listing.source_id, listing.original_url
                            )
                            if existing is not None:
                                source_deduplicated += 1
                            stored_metadata = (
                                json.loads(existing["metadata_json"] or "{}") if existing is not None else {}
                            )
                            needs_details = (
                                existing is None
                                or not existing["summary"]
                                or existing["summary"] == existing["title"]
                                or (
                                    listing.platform == "Craigslist"
                                    and listing.housing_kind == "whole_unit"
                                    and stored_metadata.get("craigslist_detail_checked") is not True
                                )
                                # The general form of the clause above: a source
                                # whose card is only a pointer says so, and keeps
                                # saying so until a detail page answers. Without
                                # it one rate-limited fetch strands the home with
                                # whatever thin card it arrived on.
                                or stored_metadata.get("detail_pending") is True
                            )
                            # Scored against a merged copy so an already-enriched
                            # listing is ranked on what is actually known about it.
                            # The raw card is what gets enriched, exactly as before.
                            preview = (
                                self._merge_stored(listing, existing, stored_metadata)
                                if existing is not None
                                else listing
                            )
                            # The provisional score exists to rank the queue for
                            # the detail budget, and nothing else reads it. Nine
                            # of the sources here have no detail budget at all,
                            # so for them this was scoring every listing twice
                            # and throwing one away -- 250 regex searches per
                            # listing, 1,959 listings, for a number never used.
                            provisional = 0
                            if detail_budget:
                                try:
                                    provisional = score_listing(
                                        classify_listing(preview), preferences
                                    ).score
                                except Exception:  # a thin card must never be lost to scoring
                                    provisional = 0
                            prepared.append(
                                {
                                    "listing": listing,
                                    "existing": existing,
                                    "stored_metadata": stored_metadata,
                                    "needs_details": needs_details,
                                    "provisional": provisional,
                                    "enriched": False,
                                }
                            )
                            # Keep the progress bar moving while results are being
                            # read, rather than jumping only when a source ends.
                            self._update_progress(listings_seen=total_seen)

                        # Pass 2: spend the budget on the best candidates that still
                        # need a detail page. Ties keep document order, which for a
                        # date-sorted source means the newer listing wins.
                        chosen = sorted(
                            (index for index, item in enumerate(prepared) if item["needs_details"]),
                            key=lambda index: (-prepared[index]["provisional"], index),
                        )[:detail_budget]
                        for index in sorted(chosen):
                            if time.monotonic() + self.timeout_seconds > deadline:
                                break
                            item = prepared[index]
                            try:
                                item["listing"] = source.enrich(client, item["listing"])
                                item["enriched"] = True
                            except Exception as exc:  # one expired/broken detail must not lose the search result
                                detail_failures += 1
                                LOGGER.warning(
                                    "%s detail fetch failed for %s: %s",
                                    source.platform,
                                    item["listing"].original_url,
                                    exc,
                                )
                            if self.detail_delay_seconds:
                                time.sleep(self.detail_delay_seconds)

                        # Pass 4 is below; pass 3 first, so a home this search
                        # did return is up to date before anything is rechecked.
                        # Pass 3: classify, score and store every listing.
                        for item in prepared:
                            listing = item["listing"]
                            existing = item["existing"]
                            if not item["enriched"] and existing is not None:
                                listing = self._merge_stored(listing, existing, item["stored_metadata"])
                            listing = classify_listing(listing)
                            source_classified += 1
                            result = score_listing(listing, preferences)
                            if result.eligibility == "ineligible":
                                source_hard_filtered += 1
                            if (
                                result.eligibility != "ineligible"
                                and result.score >= preferences.minimum_score
                            ):
                                source_active += 1
                            else:
                                source_archived += 1
                            matched_neighborhood = result.details.get("neighborhood", {}).get("match_label")
                            if matched_neighborhood:
                                listing = replace(listing, neighborhood=str(matched_neighborhood))
                            # A search that returns a home is the source saying it
                            # still lists it, which is a confirmation and a
                            # stronger one than re-reading a single page. Stamping
                            # it here is what keeps the recheck pass aimed only at
                            # the homes nobody has heard about.
                            listing = replace(
                                listing,
                                metadata={**listing.metadata, "last_verified_at": scan_started_at},
                            )
                            _, created = self.repository.upsert_listing(listing, result)
                            if created:
                                source_added += 1
                                total_added += 1
                            else:
                                source_updated += 1
                                total_updated += 1
                            self._update_progress(
                                listings_seen=total_seen,
                                listings_added=total_added,
                            )
                        # Pass 4: go and look at the shortlisted homes this
                        # search stopped returning. A listing is enriched once
                        # and never revisited, so a room verified live on Monday
                        # and taken down on Wednesday stayed on the shortlist
                        # looking as current as one posted this morning. Absence
                        # from one page is not proof, so this checks the page
                        # rather than inferring anything from the silence, and
                        # only runs where the search itself succeeded.
                        rechecked = self._recheck_absent(
                            client,
                            source,
                            preferences,
                            seen_source_ids={item["listing"].source_id for item in prepared},
                            deadline=deadline,
                            sources_remaining=max(1, len(active_sources) - source_index + 1),
                        )
                        message = (
                            f"Completed with {detail_failures} detail-page warning(s)."
                            if detail_failures
                            else getattr(source, "empty_result_message", None)
                            if source_seen == 0
                            else None
                        )
                        if rechecked:
                            note = f"Rechecked {rechecked} home(s) this search no longer lists."
                            message = f"{message} {note}" if message else note
                        self.repository.finish_source_run(
                            source_run_id,
                            "success",
                            seen=source_seen,
                            added=source_added,
                            message=message,
                            provider=self._provider(source),
                            source_key=self._source_key(source),
                            updated=source_updated,
                            fetched=source_fetched,
                            parsed=source_parsed,
                            classified=source_classified,
                            deduplicated=source_deduplicated,
                            hard_filtered=source_hard_filtered,
                            active=source_active,
                            archived=source_archived,
                        )
                        if trigger == "initial_discovery":
                            self.repository.mark_source_initialized(source_key, "success", message)
                        connector_key = self._connector_key(source)
                        connector_state_key = self._connector_state_key(source)
                        if connector_key and connector_state_key:
                            previous = self.repository.connector_state(connector_state_key)
                            observed = (previous.observed_items if previous else 0) + source_seen
                            alerts_seen = max(0, int(getattr(source, "last_alert_count", 0)))
                            if connector_key == "gmail":
                                state = (
                                    "working"
                                    if source_seen > 0
                                    else "degraded"
                                    if alerts_seen > 0
                                    else "waiting_first_alert"
                                )
                                state_message = (
                                    f"Imported {source_seen} {source.platform} listing(s) from a saved-search alert."
                                    if source_seen
                                    else (
                                        getattr(source, "empty_result_message", None)
                                        or f"{source.platform} alerts were found, but no supported listing was parsed."
                                    )
                                    if alerts_seen
                                    else f"No {source.platform} saved-search alert has arrived yet."
                                )
                            else:
                                state = "working" if observed > 0 else "working_zero"
                                state_message = (
                                    f"{source.platform} returned {source_seen} listing(s)."
                                    if source_seen
                                    else f"{source.platform} checked successfully with no matching listings."
                                )
                            self.repository.set_connector_state(
                                connector_state_key,
                                state,
                                message=state_message,
                                observed_items=observed,
                                attempted=True,
                                succeeded=state in {"working", "working_zero", "waiting_first_alert"},
                            )
                            if connector_key == "gmail":
                                self.refresh_gmail_connector_state()
                        LOGGER.info(
                            "%s scan succeeded: %s seen, %s new",
                            source.platform,
                            source_seen,
                            source_added,
                        )
                    except Exception as exc:
                        sources_failed += 1
                        message = f"{type(exc).__name__}: {exc}"
                        self.repository.finish_source_run(
                            source_run_id,
                            "error",
                            seen=source_seen,
                            added=source_added,
                            message=message[:1000],
                            provider=self._provider(source),
                            source_key=self._source_key(source),
                            updated=source_updated,
                            fetched=source_fetched,
                            parsed=source_parsed,
                            classified=source_classified,
                            deduplicated=source_deduplicated,
                            hard_filtered=source_hard_filtered,
                            active=source_active,
                            archived=source_archived,
                        )
                        if trigger == "initial_discovery":
                            self.repository.mark_source_initialized(source_key, "error", message[:1000])
                        connector_key = self._connector_key(source)
                        connector_state_key = self._connector_state_key(source)
                        if connector_key and connector_state_key:
                            self.repository.set_connector_state(
                                connector_state_key,
                                # A source that knows what went wrong outranks a
                                # guess made from its message text.
                                getattr(exc, "connector_state", None)
                                or connector_state_for_error(message),
                                message=message[:1000],
                                observed_items=source_seen,
                                attempted=True,
                            )
                            if connector_key == "gmail":
                                self.refresh_gmail_connector_state()
                        LOGGER.exception("%s source failed without stopping other sources", source.platform)
                    self._finish_source_progress(
                        source_index,
                        weights.get(source.platform, 0.0),
                        listings_seen=total_seen,
                        listings_added=total_added,
                        sources_failed=sources_failed,
                    )

                # Ask the disconnected sources how much they are holding. Inside
                # the client block on purpose: one line further out and the
                # client is closed, which fails every request with "Cannot send
                # a request, as the client has been closed" and looks exactly
                # like a source that cannot be counted.
                # A passenger on the scan, not part of it: it runs after every
                # source, cannot change the outcome and cannot fail it. Any
                # trigger may ask; how often these sites are really contacted is
                # bounded by how stale the stored count is.
                self._measure_missing_coverage(client, preferences)

            completed_status = "completed_with_errors" if sources_failed else "completed"
            self.repository.finish_scan(
                run_id,
                completed_status,
                seen=total_seen,
                added=total_added,
                updated=total_updated,
                failed=sources_failed,
            )
            final_status = completed_status
            LOGGER.info(
                "Finished scan %s: %s seen, %s new, %s source errors",
                run_id,
                total_seen,
                total_added,
                sources_failed,
            )
            return ScanOutcome(
                run_id,
                final_status,
                listings_seen=total_seen,
                listings_added=total_added,
                listings_updated=total_updated,
                sources_failed=sources_failed,
            )
        except Exception as exc:
            if run_id is not None:
                self.repository.finish_scan(
                    run_id,
                    "failed",
                    seen=total_seen,
                    added=total_added,
                    updated=total_updated,
                    failed=sources_failed,
                    message=f"{type(exc).__name__}: {exc}"[:1000],
                )
            LOGGER.exception("Scan %s failed before all sources could run", run_id)
            return ScanOutcome(
                run_id or 0,
                "failed",
                listings_seen=total_seen,
                listings_added=total_added,
                listings_updated=total_updated,
                sources_failed=sources_failed,
            )
        finally:
            self._finish_progress(final_status)
            self._release_scan_locks()

    def rescore_all(self, preferences: Preferences | None = None) -> int:
        active_preferences = preferences or self.preference_loader()
        if not active_preferences.profile_active:
            return 0
        candidates = self.repository.all_candidates()
        # One connection for the whole board. A connection per listing costs an
        # fsync on every close, which on 870 homes was half a minute of disk
        # sync while start-up waited on it and the dashboard was unreachable.
        with self.repository.connection() as connection:
            self._rescore(candidates, active_preferences, connection)
        LOGGER.info("Rescored %s stored listings after preference update", len(candidates))
        return len(candidates)

    def _rescore(self, candidates, active_preferences, connection) -> None:
        for listing_id, listing in candidates:
            visible_area: str | None = None
            if listing.platform == "Facebook Marketplace":
                mapped_area = facebook_coordinate_neighborhood(listing.neighborhood)
                if mapped_area:
                    listing = replace(listing, neighborhood=mapped_area)
            elif listing.platform == "Furnished Finder" and not listing.neighborhood:
                # Backfill only an explicitly written area from cards already
                # stored locally. Do not request detail pages or guess map pins.
                visible_area = visible_sf_area_hint(
                    " ".join(filter(None, [listing.title, listing.summary, listing.listing_type]))
                )
                if visible_area:
                    listing = replace(listing, neighborhood=visible_area)
            listing = classify_listing(listing)
            result = score_listing(listing, active_preferences)
            matched_neighborhood = result.details.get("neighborhood", {}).get("match_label")
            self.repository.update_score(
                listing_id,
                result,
                neighborhood=(
                    str(matched_neighborhood)
                    if matched_neighborhood
                    else visible_area
                ),
                listing=listing,
                connection=connection,
            )
        connection.commit()
