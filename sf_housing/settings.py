from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    preferences_path: Path
    database_path: Path
    log_path: Path
    # Public-source work stays inside one bounded scan even with separate room,
    # small-unit, and 2–3 bedroom searches plus their detail reads.
    #
    # Raised from 110 when Movoto began reading its whole inventory, and again
    # when raising the per-source cap let Craigslist double what it collects:
    # one scan spent 65 seconds there and skipped sixteen later sources. No
    # single source can run away with this now -- SOURCE_HARD_CEILING_SECONDS
    # bounds each one -- so the budget only has to cover the healthy total,
    # which measures around 165 seconds across 23 sources.
    #
    # Four minutes is a hard ceiling rather than a target: it is how long
    # somebody who pressed the button on the dashboard is willing to watch a
    # progress bar. A scan that would run longer stops and says which sources
    # it did not reach, which is the honest outcome -- the nightly sweep is
    # what collects anything a bounded scan had to leave.
    request_timeout_seconds: float = 8.0
    scan_max_seconds: float = 240.0
    # Nobody is waiting on the nightly sweep, and its whole job is to reach the
    # depth a four-minute scan cannot. Held to a budget all the same, so a
    # source that stops answering at 3am cannot run until morning.
    deep_scan_max_seconds: float = 900.0
    gmail_client_secret_path: Path | None = None
    gmail_token_path: Path | None = None
    gmail_pending_state_path: Path | None = None
    apify_token_path: Path | None = None
    imap_credential_path: Path | None = None

    @classmethod
    def from_environment(cls) -> "Settings":
        data_dir = Path(os.environ.get("SF_HOUSING_DATA_DIR", PROJECT_ROOT / "data")).expanduser().resolve()
        preferences_path = Path(
            os.environ.get("SF_HOUSING_PREFERENCES", data_dir / "config" / "preferences.yaml")
        ).expanduser().resolve()
        return cls(
            data_dir=data_dir,
            preferences_path=preferences_path,
            database_path=data_dir / "housing.sqlite3",
            log_path=data_dir / "sf_housing.log",
            gmail_client_secret_path=data_dir / "gmail-client-secret.json",
            gmail_token_path=data_dir / "gmail-token.json",
            gmail_pending_state_path=data_dir / "gmail-oauth-state.json",
            apify_token_path=data_dir / "apify-token.txt",
            imap_credential_path=data_dir / "imap-credential.json",
        )
