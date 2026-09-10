# How it decides, and what it promises

Two things worth understanding if you are relying on this: how a home gets its score, and
what the app does when something goes wrong.

## How a home is scored

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

## What it promises when things break

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
