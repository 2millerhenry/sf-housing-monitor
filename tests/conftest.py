from __future__ import annotations

from pathlib import Path

import pytest

from sf_housing.database import Repository
from sf_housing.preferences import Preferences, parse_preferences


TEST_PREFERENCES = """
minimum_score: 60
budget:
  min_monthly: 1000
  max_monthly: 2000
preferred_neighborhoods: [NOPA, Inner Richmond, Mission]
acceptable_neighborhoods: [Bernal Heights]
private_room: true
property_types: [house, victorian, shared flat]
lease:
  min_months: 3
  max_months: 12
  flexible: true
household:
  min_people: 2
  max_people: 6
features:
  sunlight: true
  park_access: true
  outdoor_space: true
lifestyle_keywords: [communal, quiet]
dealbreakers: [live-in aide]
weights:
  price: 25
  neighborhood: 16
  private_room: 12
  property_type: 10
  lease: 8
  household: 7
  sunlight: 6
  park_access: 6
  outdoor_space: 5
  lifestyle: 5
sources:
  max_results_per_source: 120
  craigslist_detail_pages_per_scan: 0
"""


@pytest.fixture
def preferences() -> Preferences:
    return parse_preferences(TEST_PREFERENCES)


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    instance = Repository(tmp_path / "housing.sqlite3")
    instance.initialize()
    return instance

