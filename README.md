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
housing portal and Zumper both date every entry, so their results are genuinely bounded to
that window.

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
| Apartment List | Automatic | Published schema.org data for SF buildings. The search feed carries no bedroom count, so each building is completed from its own page for size, address, amenities and unit count. Where a building publishes no rent per home, its smallest home stands in and the building's starting rent is kept. |
| Zumper | Automatic | Its published schema.org search feed: bedroom count, address, amenities and a real posting date. Most buildings publish no rent on the search page, so a bounded number are enriched from their building page and the rest stay marked unconfirmed. |
| Uloop | Automatic | A university off-campus housing board: rooms in shared flats, sublets and small landlords who post where students look. The only source whose cards all carry a real posting date. One board is read, not five: the San Francisco schools publish the same homes under per-board ids, so the listing's slug is its identity and reading them all would be five times the requests for one board's inventory. |
| RentSFNow | Automatic | The leasing feed of San Francisco's largest landlord: roughly 6,500 apartments across 293 mostly older, rent-controlled buildings, with the neighbourhood stated by the landlord rather than inferred. Nothing is on the page and the sitemap is almost all long-gone units, so the search plugin's own JSON endpoint is read instead. Asked for something it has not got, the site answers with recommendations in place of results; those are never stored. |
| AvalonBay | Automatic | One request to its San Francisco page, which ships the whole result set as JSON: unit by unit, with a floor, a square footage, a real move-in date and a rent. Half the buildings on that page are Equity Residential stock AvalonBay markets, which is said rather than implied, and a third of the units are in San Bruno and Pacifica, which are dropped. |
| AppFolio | Automatic | One parser for every small manager who lets through AppFolio, driven by a list in data/appfolio_managers.json so adding a manager is a row rather than code. These are older buildings let by people who post where their own tenants look. A subdomain that is not a tenant site answers 200 with AppFolio's own page-not-found, which is reported as misconfigured rather than as a manager with nothing available. |
| Redfin | Automatic | Its published schema.org cards, which pair each building's address, map pin and bedroom range with the rent quoted against that same URL. Where a building lets several sizes, the published rent belongs to its smallest home, so a larger home keeps the building's starting rate as context and its own rent stays unconfirmed. No detail pages are read: a Redfin building page publishes rents for its neighbours and none for the home being viewed. |
| Rent.com | Automatic | Cards to find San Francisco buildings, then each building's own page for the number of homes it holds, a rent per bedroom count, and the earliest published move-in date. Asked for a bedroom count and a rent ceiling together it pads the results with other Bay Area cities, so only the bedroom count is ever requested and the padding it names in its own payload is dropped. |
| Zillow | After one-time setup | Your own saved-search emails. Connect an email account once in **Alerts** with an app password; Google sign-in remains as an advanced fallback. |
| HotPads | After one-time setup | Official saved-search emails through the same email connection. |
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
- Whether checking is actually happening is observed, not assumed. The dashboard shows when the
  last check finished and when the next one runs, `/health` reports the scheduler's real state
  under `scheduled_checking`, and Support raises it if the scheduler has stopped or two
  scheduled checks have passed without one completing. All three read the same computation.
- Email is read with an app password over IMAP: messages are opened without being marked as
  read, only known alert senders are searched, message bodies are never stored, and the
  password is written to this machine alone with owner-only permissions. Gmail's OAuth path
  remains for accounts that cannot make app passwords, and needs a Google OAuth client from
  whoever builds the release. Outlook.com is not supported: Microsoft no longer allows app
  passwords for mail.
- Apify and the Chrome bridge are optional, capped, and reported separately.

## Using this responsibly

This tool queries Craigslist, Zillow, Facebook, and similar sites on your behalf. Automated
access may conflict with those sites' terms of service, and their terms can change at any time.
You are responsible for deciding what you point it at and for complying with the rules of the
sites you use. It is provided as-is, with no warranty — see [LICENSE](LICENSE).

## Keeping it working

The app is free, runs entirely on your own machine, and collects nothing. What it costs is
maintenance: the sites it reads change their pages without warning, and each change has to be
found and fixed before that source goes quiet.

If it helped you find somewhere to live, a one-off contribution is welcome and entirely
optional. Nothing in the app is gated, degraded, or nagged behind it — there is one line in the
dashboard footer, and that is the whole of the ask.

Set your donation page in two places to turn it on:

- `_DEFAULT_DONATE_URL` in [`sf_housing/__init__.py`](sf_housing/__init__.py) — controls the
  footer line and the Support page note. Empty means nothing is shown.
- [`.github/FUNDING.yml`](.github/FUNDING.yml) — controls the Sponsor button on this repository.

## License

[MIT](LICENSE).
