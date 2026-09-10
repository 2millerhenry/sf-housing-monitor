# SF Housing Monitor

Finding a place in San Francisco is miserable right now. Rent is about as high as it has ever
been, the tech money is back, and anything decent is gone before you have finished reading it.
You end up with fourteen tabs open, checking the same sites at midnight, and still missing
things.

I built this for my own search. Figured other people might get some use out of it, so here it is.

It reads eighteen of the big rental sites twice a day, plus the smaller local ones where the
cheaper rooms and the rent-controlled places actually turn up. Everything it finds is ranked
against what *you* said you wanted — your budget, your neighbourhoods, a whole place or a room
in a house — and lands on one page you can look at over coffee instead of hunting for it.

**It never contacts anyone on your behalf.** No automated emails to landlords, no forms filled
in, no messages sent, no applications. It reads what is already public and hands it to you. A
landlord sees nothing from this that they would not see from you browsing.

It runs on your own laptop. No account, no server, no subscription, and your search never
leaves your machine.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/shortlist-dark.png">
  <img alt="The shortlist: studios ranked by how well they match, each row showing the score, rent, neighbourhood, source and what still needs confirming" src="docs/screenshots/shortlist.png">
</picture>

## Install it

**You need:** a Mac with Apple Silicon (M1 or later) on macOS 15.6 or newer. Intel Macs and
Windows are not supported yet.

Open **Terminal** (press `Cmd` + `Space`, type `Terminal`, press Return), then paste this line
and press Return:

```
curl -fsSL https://github.com/2millerhenry/sf-housing-monitor/raw/HEAD/install.sh | bash
```

That is the whole thing. It takes a few minutes, asks for no password, and opens your browser
when it is ready. Fill in **Your deal**, press save, and the first search starts.

<details>
<summary>Rather click than type? Download the ZIP instead.</summary>

<br>

1. **[Download the ZIP](https://github.com/2millerhenry/sf-housing-monitor/releases/latest)**
   and double-click it to unpack.
2. Open the folder. **Control-click** `2 Install SF Housing Monitor.command`, choose **Open**,
   then **Open** again.
3. Wait a few minutes. Your browser opens on its own.

Control-click instead of double-click, because a browser marks its downloads and macOS blocks
unsigned apps opened the normal way. [Why that is safe to click through](#why-does-my-mac-warn-me).
The command above has no such warning, because `curl` is not a browser and does not add that
mark — the file is identical either way.

</details>

## What you get

### Eighteen sites checked for you, with no accounts

![Eighteen sources already work: Zillow, Trulia, Redfin, Craigslist, Movoto, ApartmentGuide, Zumper, Rent.com, Apartment List, AvalonBay, UDR, AppFolio, RentSFNow, Abacus, SF Housing Portal, SpareRoom, Uloop and Listings Project](docs/screenshots/sources.png)

They run twice a day without you doing anything. Three more sites — HotPads, Apartments.com and
Roomies — only ever send listings by email, so those need one inbox connected. That is
optional, and the eighteen keep running either way.
[The full list, and what is read from each](docs/sources.md).

### Ranked against your deal, not theirs

Every home gets a score out of 100 for how well it fits what you actually asked for, and the
row tells you why it scored that way and what still needs checking. Nothing is thrown away:
homes below your line wait in **Near matches**, and if you change your mind later, everything
ever collected is re-ranked against the new answer. [How scoring works](docs/how-it-works.md).

### A number that shows what your deal is costing you

![The shortlist cut-off slider reading "60 and up, 446 homes"](docs/screenshots/match-slider.png)

Tighten your budget or drop a neighbourhood and the count moves as you type, so you can see the
price of being fussy before you commit to it.

## Questions

### Is it really free?

Yes, and there is nothing to buy later. It runs on your own machine, so there is no server to
pay for. If it finds you somewhere to live there is one optional donation line in the footer,
and that is the whole of the ask — nothing is locked, degraded, or nagged.

### Where does my information go?

Nowhere. Your answers, the listings, your notes and any password you add are written to one
folder on your Mac and never leave it. There is no account and no analytics. Uninstalling keeps
your data unless you explicitly type `DELETE` when it asks.

### Why does my Mac warn me?

Only the ZIP does this — the one-line command does not. If you downloaded the ZIP through a
browser, it happens because the app is not code-signed. Signing requires a paid Apple Developer account, which this
project does not have, so macOS says *"cannot be opened because it is from an unidentified
developer"* the first time.

Control-click → **Open** → **Open** is Apple's own way through that, and it is how most free
Mac software is installed. The installer then clears the flag for the rest of the release, so
Open, Verify, Repair and Uninstall do not each ask again.

**Do not turn Gatekeeper off system-wide to avoid this.** It protects everything else on your
machine. If you would rather not trust a download at all, the whole source is here and you can
[build it yourself](docs/development.md).

### Do I have to connect my email?

No. The eighteen main sites need nothing. Connecting an inbox only adds HotPads,
Apartments.com and Roomies, and it uses a read-only app password — a separate one-purpose
password, never your real one — that stays on your machine. Messages are opened without being
marked read, only known housing senders are searched, and no message is ever stored.

### Does it run when my laptop is shut?

No. It needs the Mac awake and logged in. If it misses a check because you were asleep or away,
it catches up when you come back.

### Can I check right now instead of waiting?

Yes — there is a **Check for new homes** button, once a day. It is limited to once because
these are other people's websites, and hammering them is how you get blocked from a source
entirely.

### Something looks broken. What do I do?

Double-click **Verify SF Housing Monitor.command**. It runs a check over the whole app and
names one specific thing to do for each problem it finds. **Repair** fixes most of them without
touching your deal, your saved homes or your notes.

### Is pasting a command from the internet safe?

It is worth being suspicious of, so here is what that one does. It reads the
[release list](https://github.com/2millerhenry/sf-housing-monitor/releases), downloads the same
ZIP the button gives you, checks every file inside it against a checksum, and runs the
installer. It never asks for your password, because nothing here needs an administrator.
It is [forty lines long and you can read it first](install.sh) — or skip it entirely and use
the ZIP.

### What if I am not looking in San Francisco?

Then this will not help you much as it stands — the sources, neighbourhoods and the city
housing portal are all SF-specific. It is MIT licensed, so you are welcome to fork it.

## For developers

- [Running from source, and the test suite](docs/development.md)
- [Where the listings come from](docs/sources.md)
- [How scoring works, and what it promises when a source breaks](docs/how-it-works.md)

## Using this responsibly

This tool queries Craigslist, Zillow, Facebook, and similar sites on your behalf. Automated
access may conflict with those sites' terms of service, and their terms can change at any time.
You are responsible for deciding what you point it at and for complying with the rules of the
sites you use. It is provided as-is, with no warranty — see [LICENSE](LICENSE).

## Keeping it going

This is free and it stays free. I wrote it because I needed it, and it is more use to other
people than it is sitting on my laptop.

What it costs is upkeep. These sites redesign their pages without telling anyone, and when one
does, that source goes quiet until somebody fixes it — which is me, in the evenings.

If it helped you find somewhere, or just saved you a few weeks of refreshing, there is a donate
link in the app's footer and a Sponsor button on this repo. Entirely optional, and genuinely
so: nothing is locked, nothing is nagged, and nothing gets worse if you skip it.

If you are forking this, the donation link lives in two places:
`_DEFAULT_DONATE_URL` in [`sf_housing/__init__.py`](sf_housing/__init__.py) and
[`.github/FUNDING.yml`](.github/FUNDING.yml). Empty means nothing is shown.

## License

[MIT](LICENSE).
