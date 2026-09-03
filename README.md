# SF Housing Monitor

A small local service that finds and organizes San Francisco housing listings on your own
computer. It checks free public sources on a schedule, scores what it finds against a housing
profile you fill in yourself, and shows the results on a private dashboard at
`http://127.0.0.1:8000`.

Nothing is hosted. There is no account, no server, and no cost to run: the app, its database,
and every credential you add stay on the machine you install it on.

It keeps three searches separate, each with its own budget and rules that you set:

- private rooms in shared homes,
- entire studios and one-bedrooms,
- entire 2–3 bedroom homes to split, priced per person as well as in total.

## Install

Download the ZIP for your platform from the
[Releases page](https://github.com/2millerhenry/sf-housing-monitor/releases), extract it, and
run the installer inside. The first install downloads a private Python runtime; it does not
use `sudo`, Homebrew, an administrator account, or your system Python.

**The build is not code-signed**, so both operating systems will warn you the first time. Real
signing needs a paid Apple Developer account and a Windows code-signing certificate; this
project has neither, so it asks for one approval instead of hiding the fact.

**macOS** (Apple Silicon, macOS 15.6 or later):

1. Double-click `Install SF Housing Monitor.command`.
2. macOS blocks it. Control-click the same file, choose **Open**, then **Open** again.
3. The installer clears the download quarantine flag for the rest of the release, so Open,
   Verify, Repair, and Uninstall do not each ask again.

**Windows** (x64, Windows 10 or later):

1. Extract the ZIP first — do not run it from inside the ZIP.
2. Double-click `Install SF Housing Monitor.cmd`.
3. If SmartScreen appears, choose **More info**, then **Run anyway**.

The installer verifies every payload file against a checksum before using it, and stops without
changing anything if a file does not match. When it finishes, your browser opens the dashboard.

Your profile, listings, decisions, and tokens live in one folder — `~/Library/Application
Support/SF Housing Monitor` on macOS, `%LOCALAPPDATA%\SF Housing Monitor` on Windows — and
uninstalling preserves them unless you explicitly type `DELETE` when asked.

## First run

A new installation starts blank. There is no bundled profile, no sample listings, and no
pre-filled budget or neighborhood: **Your deal** asks what you are looking for, and only what
you answer is used.

Saving that form the first time triggers one initial discovery scan, so the dashboard opens on
real listings instead of an empty page — a first run typically stores several hundred. The free
public sources are queried newest-first and return what they currently list. Where a source
publishes a post date, that first scan keeps only the **last seven days**; a listing with no
usable date is kept rather than discarded, since most public sources publish none. The city
housing portal is the one free source that dates every entry, so its results are genuinely
bounded to that window.

After that, scans run at 10:00 and 18:00 America/Los_Angeles, and each later scan updates
`last_seen` instead of inserting duplicates, so your stars, notes, and dismissals survive.

## Running from source

Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m sf_housing serve
```

One scan without the dashboard:

```bash
python -m sf_housing scan
```

The test suite:

```bash
pytest
```

A source checkout keeps its data under `data/`, which is git-ignored. Override locations with
`SF_HOUSING_DATA_DIR` and `SF_HOUSING_PREFERENCES` to run more than one instance. A complete,
fully-populated example of the profile schema lives in
[`tests/fixtures/benchmark_profile.yaml`](tests/fixtures/benchmark_profile.yaml); it is a test
fixture for the ranking benchmark, and the application never reads it.

Inspect or restart the installed background service:

```bash
launchctl print gui/$(id -u)/com.sfhousing.monitor
```

## What it checks

| Platform | Mode | Notes |
|---|---|---|
| Craigslist | Automatic | Public SF rooms/share and apartments searches. Separate room, 0–1 bedroom, broad 2–3 bedroom, and exact 3-bedroom searches each get a bounded detail-page budget. |
| Listings Project | Automatic | The public SF Bay Area collection, filtered to explicit San Francisco rentals and sublets. Each card keeps its direct lister-contact page. |
| Abacus (small buildings) | Automatic | The public San Francisco availability feed, preserving the manager's Apply Now route. Building size stays unknown unless stated. |
| SpareRoom | Automatic | Public SF result cards: price, area, type, and short description. |
| SF Housing Portal | Automatic | The city's own below-market-rate portal (DAHLIA) over its public JSON API. No key, no scraping — one entry per unit type, with real rents and application deadlines. |
| Apartment List | Automatic | Published schema.org data for SF buildings. Building-level starting rents; the feed does not say how many bedrooms, so the home size stays unconfirmed. |
| Zillow | After one-time setup | Your own saved-search emails, imported locally after a read-only Gmail connection in **Alerts**. |
| HotPads | After one-time setup | Official saved-search emails through the same read-only Gmail connection. |
| Roomies | After one-time setup | Realtime or daily listing-alert emails through the same connection. |
| Facebook Marketplace | After one-time setup | A capped Apify free-tier connector reads ten newest SF Property Rentals cards per scan. No Facebook credentials are used. |
| Furnished Finder | After one-time Chrome setup | A local Chrome bridge reads cards already visible in your own browser session for up to three saved searches. Needs Chrome and the monitor running at check time. |

Sources that cannot run are shown in **Source health** with a direct link to the equivalent
search. They are deliberately not presented as working integrations.

## Scoring behavior

- Only criteria you configure participate in the weighted score.
- A configured criterion the listing does not describe gets neutral (50%) credit, not a failure.
- Known out-of-area locations never qualify for the main results, however good the price. They
  stay stored and visible under **All stored**.
- An explicit match gets full credit; an explicit mismatch gets low or zero credit.
- The three strongest supported matches become the displayed reasons. The highest-weight
  explicit mismatch becomes the concern; with none, the most important unknown is shown.
- A configured dealbreaker phrase subtracts 20 points and is reported as the main concern.
- Whole-unit and split shortlists require explicit evidence of an entire home, apply your exact
  per-bedroom caps, and show both total and per-person cost. A known building above your unit
  ceiling stays in the archive; unknown size stays reviewable with a warning.
- Everything fetched is retained in SQLite, including results below the display threshold, so
  changing your profile later rescores the whole history instead of starting over.

## Reliability boundaries

- Thread and cross-process locks stop scheduled, manual, command-line, and service scans from
  overlapping. The process lock is released by the operating system if a scan dies partway.
- Each platform is isolated: a parser, timeout, or HTTP failure is recorded, logged, and shown
  in the dashboard without stopping later sources.
- The freshness watchdog derives its state from durable source runs. A successful zero-result
  check reads as "Working, no matches", while stale data and repeated failures show one
  recovery action. After two consecutive automatic failures a source pauses briefly.
- HTTP requests use a descriptive user agent, an 8-second timeout, a small connection pool, and
  bounded detail reads. The public-source path is capped at 110 seconds.
- The app does not bypass CAPTCHAs, login gates, or bot protections. Public page HTML can change
  at any time; when it does, the adapter fails visibly instead of silently reporting no listings.
- Scans need the computer awake and logged in. Missed scheduled runs are caught up after wake or
  reboot; a machine that is asleep or off does no network work.
- Gmail uses exactly `gmail.readonly`, searches only supported alert senders, never stores whole
  mailboxes or displays message bodies, and applies 15-second timeouts. Connecting Gmail
  requires whoever builds the release to supply their own Google OAuth client.
- Apify and the Chrome bridge are optional, capped, and reported separately.

## Using this responsibly

This tool queries Craigslist, Zillow, Facebook, and similar sites on your behalf. Automated
access may conflict with those sites' terms of service, and their terms can change at any time.
You are responsible for deciding what you point it at and for complying with the rules of the
sites you use. It is provided as-is, with no warranty — see [LICENSE](LICENSE).

## License

[MIT](LICENSE).
