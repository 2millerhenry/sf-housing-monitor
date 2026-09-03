from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .classification import classify_listing
from .connectors import CONNECTOR_STATES, ConnectorStatus
from .models import ListingCandidate, ScoreResult


SCHEMA_VERSION = 3


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    ignored = {
        "fbclid",
        "gclid",
        "listing_click",
        "search_id",
        "search_results",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
    query = urlencode(sorted((key, value) for key, value in parse_qsl(parts.query) if key not in ignored))
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), query, ""))


SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    source_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    price INTEGER,
    neighborhood TEXT,
    listing_type TEXT,
    summary TEXT,
    housing_kind TEXT NOT NULL DEFAULT 'room',
    unit_type TEXT,
    building_units INTEGER,
    match_reasons_json TEXT NOT NULL DEFAULT '[]',
    concern TEXT NOT NULL,
    score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
    confidence INTEGER NOT NULL DEFAULT 0 CHECK(confidence BETWEEN 0 AND 100),
    eligibility TEXT NOT NULL DEFAULT 'eligible' CHECK(eligibility IN ('eligible', 'needs_verification', 'ineligible')),
    eligibility_reasons_json TEXT NOT NULL DEFAULT '[]',
    unknowns_json TEXT NOT NULL DEFAULT '[]',
    score_details_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    first_found TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    published_at TEXT,
    availability_state TEXT NOT NULL DEFAULT 'unknown',
    original_url TEXT NOT NULL,
    opened_at TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'saved', 'dismissed')),
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(platform, source_id)
);

CREATE INDEX IF NOT EXISTS idx_listings_results
ON listings(status, score DESC, first_found DESC);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    listings_seen INTEGER NOT NULL DEFAULT 0,
    listings_added INTEGER NOT NULL DEFAULT 0,
    listings_updated INTEGER NOT NULL DEFAULT 0,
    sources_failed INTEGER NOT NULL DEFAULT 0,
    message TEXT
);

CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_run_id INTEGER NOT NULL REFERENCES scan_runs(id),
    platform TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    source_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    listings_seen INTEGER NOT NULL DEFAULT 0,
    listings_added INTEGER NOT NULL DEFAULT 0,
    listings_updated INTEGER NOT NULL DEFAULT 0,
    fetched INTEGER NOT NULL DEFAULT 0,
    parsed INTEGER NOT NULL DEFAULT 0,
    classified INTEGER NOT NULL DEFAULT 0,
    deduplicated INTEGER NOT NULL DEFAULT 0,
    hard_filtered INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    search_url TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_runs_latest
ON source_runs(platform, id DESC);

CREATE TABLE IF NOT EXISTS source_initializations (
    source_key TEXT PRIMARY KEY,
    initialized_at TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT
);

CREATE TABLE IF NOT EXISTS connector_states (
    connector_key TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    configured_at TEXT,
    last_attempt_at TEXT,
    last_success_at TEXT,
    observed_items INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
"""


class Repository:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(listings)")}
            if "opened_at" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN opened_at TEXT")
            if "housing_kind" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN housing_kind TEXT NOT NULL DEFAULT 'room'")
            if "unit_type" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN unit_type TEXT")
            if "building_units" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN building_units INTEGER")
            if "confidence" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN confidence INTEGER NOT NULL DEFAULT 0")
            if "eligibility" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN eligibility TEXT NOT NULL DEFAULT 'eligible'")
            if "eligibility_reasons_json" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN eligibility_reasons_json TEXT NOT NULL DEFAULT '[]'")
            if "unknowns_json" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN unknowns_json TEXT NOT NULL DEFAULT '[]'")
            if "published_at" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN published_at TEXT")
            if "availability_state" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN availability_state TEXT NOT NULL DEFAULT 'unknown'")
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_listings_housing_results
                   ON listings(housing_kind, status, score DESC, first_found DESC)"""
            )
            source_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(source_runs)")
            }
            source_column_migrations = {
                "provider": "TEXT NOT NULL DEFAULT ''",
                "source_key": "TEXT NOT NULL DEFAULT ''",
                "listings_updated": "INTEGER NOT NULL DEFAULT 0",
                "fetched": "INTEGER NOT NULL DEFAULT 0",
                "parsed": "INTEGER NOT NULL DEFAULT 0",
                "classified": "INTEGER NOT NULL DEFAULT 0",
                "deduplicated": "INTEGER NOT NULL DEFAULT 0",
                "hard_filtered": "INTEGER NOT NULL DEFAULT 0",
                "active": "INTEGER NOT NULL DEFAULT 0",
                "archived": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, declaration in source_column_migrations.items():
                if name not in source_columns:
                    connection.execute(f"ALTER TABLE source_runs ADD COLUMN {name} {declaration}")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_runs_identity ON source_runs(source_key, id DESC)"
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()

    def integrity_check(self) -> tuple[bool, str]:
        """Return SQLite's bounded integrity verdict for support diagnostics."""
        try:
            with self.connection() as connection:
                row = connection.execute("PRAGMA integrity_check(1)").fetchone()
        except sqlite3.Error as exc:
            return False, f"{type(exc).__name__}: {exc}"
        verdict = str(row[0] if row is not None else "No result")
        return verdict.casefold() == "ok", verdict

    def schema_version(self) -> int:
        try:
            with self.connection() as connection:
                row = connection.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error:
            return -1
        return int(row[0]) if row is not None else 0

    def find_listing(self, platform: str, source_id: str, url: str) -> sqlite3.Row | None:
        canonical = canonicalize_url(url)
        with self.connection() as connection:
            return connection.execute(
                """SELECT * FROM listings
                   WHERE (platform = ? AND source_id = ?) OR canonical_url = ?
                   LIMIT 1""",
                (platform, source_id, canonical),
            ).fetchone()

    def upsert_listing(
        self,
        listing: ListingCandidate,
        result: ScoreResult,
        seen_at: str | None = None,
    ) -> tuple[int, bool]:
        listing = classify_listing(listing)
        now = seen_at or utc_now()
        canonical = canonicalize_url(listing.original_url)
        reasons = json.dumps(result.reasons, ensure_ascii=False)
        score_details = json.dumps(result.details, ensure_ascii=False)
        eligibility_reasons = json.dumps(result.eligibility_reasons, ensure_ascii=False)
        unknowns = json.dumps(result.unknowns, ensure_ascii=False)
        metadata = json.dumps(listing.metadata, ensure_ascii=False)
        published_at = listing.metadata.get("listing_timestamp")
        if not isinstance(published_at, str) or not published_at.strip():
            published_at = None
        with self.connection() as connection:
            existing = connection.execute(
                """SELECT id FROM listings
                   WHERE (platform = ? AND source_id = ?) OR canonical_url = ?
                   LIMIT 1""",
                (listing.platform, listing.source_id, canonical),
            ).fetchone()
            if existing:
                listing_id = int(existing["id"])
                connection.execute(
                    """UPDATE listings SET
                        platform = ?, source_id = ?, canonical_url = ?, title = ?, price = ?,
                        neighborhood = ?, listing_type = ?, summary = ?, housing_kind = ?,
                        unit_type = ?, building_units = ?, match_reasons_json = ?,
                        concern = ?, score = ?, confidence = ?, eligibility = ?,
                        eligibility_reasons_json = ?, unknowns_json = ?, score_details_json = ?,
                        metadata_json = ?, last_seen = ?, published_at = COALESCE(?, published_at),
                        original_url = ?
                       WHERE id = ?""",
                    (
                        listing.platform,
                        listing.source_id,
                        canonical,
                        listing.title,
                        listing.price,
                        listing.neighborhood,
                        listing.listing_type,
                        listing.summary,
                        listing.housing_kind,
                        listing.unit_type,
                        listing.building_units,
                        reasons,
                        result.concern,
                        result.score,
                        result.confidence,
                        result.eligibility,
                        eligibility_reasons,
                        unknowns,
                        score_details,
                        metadata,
                        now,
                        published_at,
                        listing.original_url,
                        listing_id,
                    ),
                )
                created = False
            else:
                cursor = connection.execute(
                    """INSERT INTO listings (
                        platform, source_id, canonical_url, title, price, neighborhood,
                        listing_type, summary, housing_kind, unit_type, building_units,
                        match_reasons_json, concern, score, confidence, eligibility,
                        eligibility_reasons_json, unknowns_json, score_details_json, metadata_json,
                        first_found, last_seen, published_at, original_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        listing.platform,
                        listing.source_id,
                        canonical,
                        listing.title,
                        listing.price,
                        listing.neighborhood,
                        listing.listing_type,
                        listing.summary,
                        listing.housing_kind,
                        listing.unit_type,
                        listing.building_units,
                        reasons,
                        result.concern,
                        result.score,
                        result.confidence,
                        result.eligibility,
                        eligibility_reasons,
                        unknowns,
                        score_details,
                        metadata,
                        now,
                        now,
                        published_at,
                        listing.original_url,
                    ),
                )
                listing_id = int(cursor.lastrowid)
                created = True
            connection.commit()
        return listing_id, created

    def update_score(
        self,
        listing_id: int,
        result: ScoreResult,
        neighborhood: str | None = None,
        listing: ListingCandidate | None = None,
    ) -> None:
        with self.connection() as connection:
            classified = classify_listing(listing) if listing is not None else None
            values = (
                result.score,
                json.dumps(result.reasons, ensure_ascii=False),
                result.concern,
                result.confidence,
                result.eligibility,
                json.dumps(result.eligibility_reasons, ensure_ascii=False),
                json.dumps(result.unknowns, ensure_ascii=False),
                json.dumps(result.details, ensure_ascii=False),
            )
            if classified is not None:
                connection.execute(
                    """UPDATE listings
                       SET score = ?, match_reasons_json = ?, concern = ?, confidence = ?,
                           eligibility = ?, eligibility_reasons_json = ?, unknowns_json = ?,
                           score_details_json = ?,
                           neighborhood = COALESCE(?, neighborhood), housing_kind = ?, unit_type = ?,
                           building_units = ?
                       WHERE id = ?""",
                    (
                        *values,
                        neighborhood,
                        classified.housing_kind,
                        classified.unit_type,
                        classified.building_units,
                        listing_id,
                    ),
                )
            elif neighborhood:
                connection.execute(
                    """UPDATE listings
                       SET score = ?, match_reasons_json = ?, concern = ?, confidence = ?,
                           eligibility = ?, eligibility_reasons_json = ?, unknowns_json = ?,
                           score_details_json = ?,
                           neighborhood = ?
                       WHERE id = ?""",
                    (*values, neighborhood, listing_id),
                )
            else:
                connection.execute(
                    """UPDATE listings
                       SET score = ?, match_reasons_json = ?, concern = ?, confidence = ?,
                           eligibility = ?, eligibility_reasons_json = ?, unknowns_json = ?,
                           score_details_json = ?
                       WHERE id = ?""",
                    (*values, listing_id),
                )
            connection.commit()

    def all_candidates(self) -> list[tuple[int, ListingCandidate]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM listings").fetchall()
        return [(int(row["id"]), self._row_to_candidate(row)) for row in rows]

    @staticmethod
    def _row_to_candidate(row: sqlite3.Row) -> ListingCandidate:
        return ListingCandidate(
            platform=row["platform"],
            source_id=row["source_id"],
            title=row["title"],
            original_url=row["original_url"],
            price=row["price"],
            neighborhood=row["neighborhood"],
            listing_type=row["listing_type"],
            summary=row["summary"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            housing_kind=row["housing_kind"] or "room",
            unit_type=row["unit_type"],
            building_units=row["building_units"],
        )

    def query_listings(
        self,
        minimum_score: int = 60,
        sort: str = "score",
        neighborhood: str = "",
        platform: str = "",
        home_style: str = "",
        housing_kind: str = "room",
        unit_type: str = "",
        unit_types: tuple[str, ...] = (),
        view: str = "active",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if view not in {"saved", "dismissed", "all", "near_matches"}:
            clauses.append("score >= ?")
            parameters.append(minimum_score)
        if view == "saved":
            clauses.append("status = 'saved'")
        elif view == "dismissed":
            clauses.append("status = 'dismissed'")
        elif view == "all":
            pass
        elif view == "near_matches":
            clauses.append("status IN ('active', 'saved')")
            clauses.append("(eligibility = 'ineligible' OR score < ?)")
            parameters.append(minimum_score)
        else:
            clauses.append("status IN ('active', 'saved')")
            clauses.append("eligibility IN ('eligible', 'needs_verification')")
        if neighborhood:
            clauses.append("neighborhood = ? COLLATE NOCASE")
            parameters.append(neighborhood)
        if platform:
            clauses.append("platform = ? COLLATE NOCASE")
            parameters.append(platform)
        if housing_kind in {"room", "whole_unit"}:
            clauses.append("housing_kind = ?")
            parameters.append(housing_kind)
        valid_unit_types = {"studio", "one_bedroom", "two_bedroom", "three_bedroom", "four_bedroom"}
        if unit_type in valid_unit_types:
            clauses.append("unit_type = ?")
            parameters.append(unit_type)
        elif unit_types:
            selected_types = tuple(value for value in unit_types if value in valid_unit_types)
            if selected_types:
                clauses.append("unit_type IN (" + ", ".join("?" for _ in selected_types) + ")")
                parameters.extend(selected_types)
        home_style_terms = {
            "house": ("house",),
            "shared_flat": ("apartment", "flat"),
            "townhouse": ("townhouse", "town house"),
            "victorian": ("victorian",),
            "condo": ("condo",),
        }
        if home_style in home_style_terms:
            visible_text = "LOWER(COALESCE(listing_type, '') || ' ' || COALESCE(title, '') || ' ' || COALESCE(summary, ''))"
            style_clauses = [f"{visible_text} LIKE ?" for _ in home_style_terms[home_style]]
            clauses.append("(" + " OR ".join(style_clauses) + ")")
            parameters.extend(f"%{term}%" for term in home_style_terms[home_style])
        order_by = {
            "score": "score DESC, confidence DESC, COALESCE(published_at, first_found) DESC",
            "price": "price IS NULL, price ASC, score DESC",
            "newest": "first_found DESC, score DESC",
            "available": "score DESC, first_found DESC",
            # A click is recorded on every outbound listing link. Keep untouched
            # homes at the top, then preserve the normal recommendation order
            # within each group so the sort stays useful rather than arbitrary.
            "unopened": "opened_at IS NOT NULL ASC, score DESC, first_found DESC",
        }.get(sort, "first_found DESC, score DESC")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM listings{where} ORDER BY {order_by}"
        with self.connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        items = [self._dashboard_row(row) for row in rows]
        if sort == "available":
            items.sort(
                key=lambda item: (
                    not bool(item.get("available_on")),
                    str(item.get("available_on") or "9999-12-31"),
                    -int(item["score"]),
                    str(item["first_found"]),
                )
            )
        elif sort == "contact":
            # A direct application is the shortest route to a real response;
            # a direct lister page is next best.  This only changes display
            # order, never the matching score or the user's safeguards.
            items.sort(
                key=lambda item: (
                    int(item.get("contact_rank", 2)),
                    -int(item["score"]),
                    -int(item["id"]),
                )
            )
        return items

    @staticmethod
    def _dashboard_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["match_reasons"] = json.loads(item.pop("match_reasons_json") or "[]")
        item["eligibility_reasons"] = json.loads(item.pop("eligibility_reasons_json") or "[]")
        item["unknowns"] = json.loads(item.pop("unknowns_json") or "[]")
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        application_url = item["metadata"].get("application_url")
        item["application_url"] = (
            application_url
            if isinstance(application_url, str)
            and application_url.startswith("https://abacus.appfolio.com/")
            else None
        )
        item["direct_lister"] = bool(item["metadata"].get("direct_lister")) and item["platform"] == "Listings Project"
        item["contact_rank"] = 0 if item["application_url"] else 1 if item["direct_lister"] else 2
        item["contact_ready"] = item["contact_rank"] < 2
        score_details = json.loads(item.pop("score_details_json") or "{}")
        neighborhood_details = score_details.get("neighborhood")
        item["neighborhood_priority"] = (
            neighborhood_details.get("priority")
            if isinstance(neighborhood_details, dict)
            and neighborhood_details.get("priority") in {"dream", "strong", "secondary"}
            else None
        )
        item["location_hint"] = score_details.get("location_hint")
        item["household_restriction"] = score_details.get("household_restriction")
        availability = score_details.get("availability")
        item["available_on"] = (
            availability.get("available_on")
            if isinstance(availability, dict) and isinstance(availability.get("available_on"), str)
            else None
        )
        home_facts = score_details.get("home_facts")
        item["home_facts"] = home_facts if isinstance(home_facts, dict) else {}
        item["per_person_monthly"] = score_details.get("per_person_monthly")
        item["occupants"] = score_details.get("occupants")
        sublease = score_details.get("sublease")
        item["sublease_months"] = (
            sublease.get("minimum_months")
            if isinstance(sublease, dict)
            and sublease.get("is_sublease") is True
            and sublease.get("main_results_eligible") is True
            and isinstance(sublease.get("minimum_months"), int)
            else None
        )
        return item

    def filter_options(
        self,
        minimum_score: int,
        housing_kind: str = "room",
        unit_types: tuple[str, ...] = (),
    ) -> tuple[list[str], list[str]]:
        type_clause = ""
        type_parameters: list[Any] = []
        selected_types = tuple(
            value
            for value in unit_types
            if value in {"studio", "one_bedroom", "two_bedroom", "three_bedroom", "four_bedroom"}
        )
        if selected_types:
            type_clause = " AND unit_type IN (" + ", ".join("?" for _ in selected_types) + ")"
            type_parameters.extend(selected_types)
        with self.connection() as connection:
            neighborhoods = [
                row[0]
                for row in connection.execute(
                    f"""SELECT DISTINCT neighborhood FROM listings
                       WHERE score >= ? AND housing_kind = ?
                         {type_clause}
                         AND neighborhood IS NOT NULL AND neighborhood != ''
                       ORDER BY neighborhood COLLATE NOCASE""",
                    (minimum_score, housing_kind, *type_parameters),
                )
            ]
            platforms = [
                row[0]
                for row in connection.execute(
                    f"""SELECT DISTINCT platform FROM listings
                       WHERE score >= ? AND housing_kind = ? {type_clause}
                       ORDER BY platform COLLATE NOCASE""",
                    (minimum_score, housing_kind, *type_parameters),
                )
            ]
        return neighborhoods, platforms

    def set_listing_status(self, listing_id: int, status: str) -> bool:
        if status not in {"active", "saved", "dismissed"}:
            raise ValueError("Invalid listing status")
        with self.connection() as connection:
            cursor = connection.execute("UPDATE listings SET status = ? WHERE id = ?", (status, listing_id))
            connection.commit()
        return cursor.rowcount == 1

    def set_listing_note(self, listing_id: int, note: str) -> bool:
        with self.connection() as connection:
            cursor = connection.execute("UPDATE listings SET note = ? WHERE id = ?", (note, listing_id))
            connection.commit()
        return cursor.rowcount == 1

    def mark_listing_opened(self, listing_id: int) -> bool:
        """Keep the first time a user opened a listing without changing review status."""
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE listings SET opened_at = COALESCE(opened_at, ?) WHERE id = ?",
                (utc_now(), listing_id),
            )
            connection.commit()
        return cursor.rowcount == 1

    def open_listing_url(self, listing_id: int) -> str | None:
        """Record an open and return the stored destination as one local operation."""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT original_url FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE listings SET opened_at = COALESCE(opened_at, ?) WHERE id = ?",
                (utc_now(), listing_id),
            )
            connection.commit()
        return str(row["original_url"])

    def begin_scan(self, trigger: str) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO scan_runs(trigger, status, started_at) VALUES (?, 'running', ?)",
                (trigger, utc_now()),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def finish_scan(
        self,
        run_id: int,
        status: str,
        seen: int = 0,
        added: int = 0,
        updated: int = 0,
        failed: int = 0,
        message: str | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """UPDATE scan_runs SET status = ?, finished_at = ?, listings_seen = ?,
                   listings_added = ?, listings_updated = ?, sources_failed = ?, message = ?
                   WHERE id = ?""",
                (status, utc_now(), seen, added, updated, failed, message, run_id),
            )
            connection.commit()

    def begin_source_run(
        self,
        scan_run_id: int,
        platform: str,
        search_url: str,
        provider: str = "",
        source_key: str = "",
    ) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO source_runs(
                       scan_run_id, platform, provider, source_key, status, started_at, search_url
                   ) VALUES (?, ?, ?, ?, 'running', ?, ?)""",
                (scan_run_id, platform, provider, source_key, utc_now(), search_url),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def finish_source_run(
        self,
        source_run_id: int,
        status: str,
        seen: int = 0,
        added: int = 0,
        message: str | None = None,
        *,
        provider: str | None = None,
        source_key: str | None = None,
        updated: int = 0,
        fetched: int = 0,
        parsed: int = 0,
        classified: int = 0,
        deduplicated: int = 0,
        hard_filtered: int = 0,
        active: int = 0,
        archived: int = 0,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """UPDATE source_runs SET status = ?, finished_at = ?, listings_seen = ?,
                   listings_added = ?, listings_updated = ?, fetched = ?, parsed = ?,
                   classified = ?, deduplicated = ?, hard_filtered = ?, active = ?,
                   archived = ?, message = ?, provider = COALESCE(?, provider),
                   source_key = COALESCE(?, source_key) WHERE id = ?""",
                (
                    status,
                    utc_now(),
                    seen,
                    added,
                    updated,
                    fetched,
                    parsed,
                    classified,
                    deduplicated,
                    hard_filtered,
                    active,
                    archived,
                    message,
                    provider,
                    source_key,
                    source_run_id,
                ),
            )
            connection.commit()

    def source_initialized(self, source_key: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status FROM source_initializations WHERE source_key = ?", (source_key,)
            ).fetchone()
        return row is not None and row["status"] == "success"

    def mark_source_initialized(self, source_key: str, status: str, message: str | None = None) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO source_initializations(source_key, initialized_at, status, message)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(source_key) DO UPDATE SET
                     initialized_at = excluded.initialized_at,
                     status = excluded.status,
                     message = excluded.message""",
                (source_key, utc_now(), status, message),
            )
            connection.commit()

    def set_connector_state(
        self,
        connector_key: str,
        state: str,
        *,
        message: str | None = None,
        observed_items: int | None = None,
        metadata: dict[str, Any] | None = None,
        configured: bool = False,
        attempted: bool = False,
        succeeded: bool = False,
    ) -> None:
        if state not in CONNECTOR_STATES:
            raise ValueError(f"Invalid connector state: {state}")
        now = utc_now()
        with self.connection() as connection:
            current = connection.execute(
                "SELECT * FROM connector_states WHERE connector_key = ?", (connector_key,)
            ).fetchone()
            previous_items = int(current["observed_items"]) if current else 0
            previous_metadata = json.loads(current["metadata_json"] or "{}") if current else {}
            connection.execute(
                """INSERT INTO connector_states(
                       connector_key, state, configured_at, last_attempt_at, last_success_at,
                       observed_items, message, metadata_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(connector_key) DO UPDATE SET
                     state = excluded.state,
                     configured_at = COALESCE(connector_states.configured_at, excluded.configured_at),
                     last_attempt_at = COALESCE(excluded.last_attempt_at, connector_states.last_attempt_at),
                     last_success_at = COALESCE(excluded.last_success_at, connector_states.last_success_at),
                     observed_items = MAX(connector_states.observed_items, excluded.observed_items),
                     message = excluded.message,
                     metadata_json = excluded.metadata_json""",
                (
                    connector_key,
                    state,
                    now if configured else None,
                    now if attempted else None,
                    now if succeeded else None,
                    max(previous_items, observed_items or 0),
                    (message or "")[:1000],
                    json.dumps({**previous_metadata, **(metadata or {})}, ensure_ascii=False),
                ),
            )
            connection.commit()

    def connector_state(self, connector_key: str) -> ConnectorStatus | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM connector_states WHERE connector_key = ?", (connector_key,)
            ).fetchone()
        if row is None:
            return None
        return ConnectorStatus(
            key=str(row["connector_key"]),
            state=str(row["state"]),
            configured_at=row["configured_at"],
            last_attempt_at=row["last_attempt_at"],
            last_success_at=row["last_success_at"],
            observed_items=int(row["observed_items"]),
            message=str(row["message"] or ""),
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def connector_states(self) -> dict[str, ConnectorStatus]:
        with self.connection() as connection:
            rows = connection.execute("SELECT connector_key FROM connector_states").fetchall()
        return {
            str(row["connector_key"]): status
            for row in rows
            if (status := self.connector_state(str(row["connector_key"]))) is not None
        }

    def recent_scans(self, limit: int = 8) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_source_runs(self) -> list[dict[str, Any]]:
        """Return one latest run per durable source, not merely per display platform.

        Older databases predate ``source_key``.  Their empty key deliberately
        falls back to platform so a repair/install migration preserves useful
        history instead of making it disappear from the dashboard.
        """
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT source_runs.* FROM source_runs
                   JOIN (
                     SELECT CASE WHEN source_key <> '' THEN source_key ELSE platform END AS identity,
                            MAX(id) AS latest_id
                     FROM source_runs
                     GROUP BY CASE WHEN source_key <> '' THEN source_key ELSE platform END
                   ) latest ON latest.latest_id = source_runs.id
                   ORDER BY CASE source_runs.platform
                     WHEN 'Facebook Marketplace' THEN 1 WHEN 'Craigslist' THEN 2
                     WHEN 'Listings Project' THEN 3 WHEN 'Abacus (small buildings)' THEN 4
                     WHEN 'Zillow' THEN 5 WHEN 'HotPads' THEN 6
                     WHEN 'SpareRoom' THEN 7 WHEN 'Roomies' THEN 8 ELSE 99 END"""
            ).fetchall()
        return [dict(row) for row in rows]

    def source_run_history(
        self,
        *,
        source_key: str,
        platform: str,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """Read the bounded durable history for one source identity.

        Source keys make same-named providers distinguishable going forward.
        The platform fallback keeps pre-v3 records visible after migration.
        This is intentionally read-only: the watchdog derives state from scan
        history rather than persisting a competing health record.
        """
        safe_limit = max(1, min(int(limit), 50))
        # Version 3 used the class/source name without a provider suffix.
        # Preserve that history after the provider-qualified key migration.
        legacy_key = source_key.rsplit("::", 1)[0]
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM source_runs
                   WHERE source_key IN (?, ?) OR (source_key = '' AND platform = ?)
                   ORDER BY id DESC LIMIT ?""",
                (source_key, legacy_key, platform, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]
