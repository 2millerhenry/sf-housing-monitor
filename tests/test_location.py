from sf_housing.location import declared_outside_sf_area_hint


def test_outside_sf_city_without_state_needs_clear_location_grammar() -> None:
    assert declared_outside_sf_area_hint("Two rooms available in Central Sunnyvale (94085)") == (
        "Sunnyvale (outside SF)"
    )
    assert declared_outside_sf_area_hint("Room for rent in North San Jose") == "San Jose (outside SF)"
    assert declared_outside_sf_area_hint("Private room in Walnut Creek") == "Walnut Creek (outside SF)"


def test_richmond_without_state_remains_ambiguous_with_sf_richmond_district() -> None:
    assert declared_outside_sf_area_hint("Private room available in Richmond District") is None
    assert declared_outside_sf_area_hint("Private room available in Richmond, CA") == (
        "Richmond (outside SF)"
    )
