"""Shared locations for test fixtures."""

from __future__ import annotations

from pathlib import Path


# A complete, illustrative deal profile. It exercises the whole scoring feature
# space, so both the ranking benchmark and the profile-level scoring tests read
# it. The application never loads it: a real installation starts blank.
BENCHMARK_PROFILE = Path(__file__).resolve().parent / "fixtures" / "benchmark_profile.yaml"
