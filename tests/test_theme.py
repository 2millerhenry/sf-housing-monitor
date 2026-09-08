"""The dark theme, and the promises it has to keep.

A theme is only finished when it is honest in three places: the page must not
flash the wrong colours on the way in, every colour the stylesheet reaches for
must exist in both palettes, and the control must say which theme is on.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from sf_housing.app import create_app
from tests.test_dashboard import app_settings

ROOT = Path(__file__).resolve().parents[1]
STYLE = (ROOT / "sf_housing/static/style.css").read_text(encoding="utf-8")
BASE = (ROOT / "sf_housing/templates/base.html").read_text(encoding="utf-8")


def block(selector: str) -> str:
    """The declarations of the first rule opened by this selector."""
    start = STYLE.index(selector) + len(selector)
    start = STYLE.index("{", start) + 1
    return STYLE[start : STYLE.index("}", start)]


def tokens(text: str) -> dict[str, str]:
    return {
        name: value.strip()
        for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", text)
    }


def test_the_two_dark_palettes_never_drift_apart() -> None:
    """It is written twice because a media query cannot be merged with an
    attribute selector. Two copies is a maintenance trap unless something
    checks them, so this is that check."""
    from_system = tokens(block(':root:not([data-theme="light"])'))
    from_choice = tokens(block(':root[data-theme="dark"]'))

    assert from_system, "the system-preference palette is missing"
    assert from_system == from_choice


def test_the_dark_theme_answers_for_every_colour_the_light_one_defines() -> None:
    """A token defined only in light silently keeps its light value on a dark
    ground, which is the classic unreadable-in-dark bug."""
    light = tokens(block(":root"))
    dark = tokens(block(':root[data-theme="dark"]'))
    colours = {
        name
        for name, value in light.items()
        if "oklch" in value or "rgb" in value or value.startswith("#")
    }

    assert colours - set(dark) == set(), f"no dark value for {sorted(colours - set(dark))}"


def test_every_colour_the_stylesheet_reaches_for_actually_exists() -> None:
    """A typo in a var() name is invisible: the property is simply dropped and
    the element inherits something that happens to look close enough."""
    declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", STYLE))
    for template in (ROOT / "sf_housing/templates").glob("*.html"):
        # Some are set on the element itself, by the template that knows the
        # value -- the per-source mark colours, for instance.
        declared |= set(re.findall(r"(--[a-z0-9-]+)\s*:", template.read_text(encoding="utf-8")))
    # A use is answerable if the token is declared somewhere -- some are set on
    # the element by script -- or if the call carries its own fallback.
    unanswered = {
        name
        for name, fallback in re.findall(r"var\((--[a-z0-9-]+)\s*(,?)", STYLE)
        if not fallback and name not in declared
    }

    assert unanswered == set(), f"resolves to nothing: {sorted(unanswered)}"


def test_an_explicit_light_choice_outranks_a_dark_computer() -> None:
    """Choosing light on a Mac set to dark has to win, or the control does
    nothing for exactly the people most likely to press it."""
    guarded = STYLE[STYLE.index("@media (prefers-color-scheme: dark)") :]

    assert ':root:not([data-theme="light"])' in guarded[:400]


def test_the_theme_is_settled_before_the_page_is_painted() -> None:
    """Reading the stored choice from a deferred script means a dark reader
    watches the page load white and then turn over."""
    head = BASE[: BASE.index("</head>")]

    assert "theme-init.js" in head, "the stored choice is read after the paint"
    # Deferring it would put the whole point of a head script back on the floor.
    tag = head[head.index("theme-init.js") - 200 : head.index("theme-init.js") + 120]
    assert "defer" not in tag and "async" not in tag, tag

    init = (ROOT / "sf_housing/static/theme-init.js").read_text(encoding="utf-8")
    assert "localStorage" in init
    assert "sf-theme" in init
    assert "data-theme" in init


def test_the_shared_layout_carries_no_inline_script() -> None:
    """base.html is on every page, including the listing page, which is built
    to be safe to serve whatever a source wrote. An inline script there is an
    inline script everywhere, and the theme is not worth that exception."""
    import re

    for tag in re.findall(r"<script\b[^>]*>", BASE):
        assert "src=" in tag, f"base.html carries an inline script: {tag}"


def test_the_dark_theme_tells_the_browser_its_own_controls_are_dark() -> None:
    """Without color-scheme the scrollbars, form controls and autofill stay in
    the other theme, which is worse than not having a dark mode."""
    assert "color-scheme: dark" in block(':root[data-theme="dark"]')


def test_a_brand_logo_keeps_a_pale_tile_in_the_dark(tmp_path: Path) -> None:
    """The source logos are dark marks drawn for a light page. On a dark
    surface they become dark on dark."""
    assert "--logo-tile" in block(':root[data-theme="dark"]')
    assert "var(--logo-tile)" in STYLE


def test_the_control_reports_which_theme_is_on(tmp_path: Path) -> None:
    """It is a toggle, so it has to be pressed when dark is on -- including
    when dark came from the computer rather than from a click."""
    application = create_app(settings=app_settings(tmp_path), sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        page = client.get("/").text

    assert "data-theme-toggle" in page
    assert 'aria-pressed="false"' in page[page.index("data-theme-toggle") - 120 : page.index("data-theme-toggle") + 120]

    script = (ROOT / "sf_housing/static/theme-toggle.js").read_text(encoding="utf-8")
    assert "prefers-color-scheme" in script, "the button must follow the computer too"
    assert "aria-pressed" in script


def test_the_toggle_follows_the_computer_until_somebody_chooses() -> None:
    """With no stored choice the CSS is already following the Mac. A button
    that decides "not dark" on its own says "off" over a dark page."""
    script = (ROOT / "sf_housing/static/theme-toggle.js").read_text(encoding="utf-8")
    decision = script[script.index("const isDark") : script.index("const sync")]

    assert "system.matches" in decision, "the computer's preference is never consulted"
    assert "dataset.theme" in decision, "an explicit choice has to win"
