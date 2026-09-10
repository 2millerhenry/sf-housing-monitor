# SF Home Finder

Finding a place in San Francisco is miserable right now. Rent is about as high as it has ever
been, the tech money is back, and anything decent is gone before you have finished reading it.
You end up with fourteen tabs open, checking the same sites at midnight, and still missing
things.

I built this for my own search. Figured other people might get some use out of it, so here it is.

It watches eighteen rental sites for you — the big ones, plus the smaller local boards where
the cheaper rooms and the rent-controlled places actually turn up — and puts everything worth
seeing on one page, in the order you would want it.

**It never contacts anyone on your behalf.** No automated emails to landlords, no forms filled
in, no applications, nothing sent in your name. It reads what is already public and hands it to
you. A landlord sees nothing from this they would not see from you browsing.

It runs on your own laptop. No account, no server, no subscription, and your search never
leaves your machine.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/shortlist-dark.png">
  <img alt="The shortlist: studios ranked by how well they match, each row showing the score, rent, neighborhood, source and what still needs confirming" src="docs/screenshots/shortlist.png">
</picture>

## How it works

**You describe what you want, once.** Your budget, the neighborhoods you would live in,
whether you are after a whole place or a room in a shared house, how long a lease, when you
need to be in. Five minutes, and you never do it again.

**It goes and looks, twice a day.** Ten in the morning and six in the evening. You do not have
to remember, keep a tab open, or do anything at all — as long as your Mac is awake and logged
in, it checks.

**You look at one page when it suits you.** Everything is ranked, best fit first, with the
reason it scored that way and anything still worth confirming. Star what you like, pass on what
you do not, leave yourself notes. What you have already dealt with does not come back.

## Install it

**You need:** a Mac with Apple Silicon (M1 or later) on macOS 15.6 or newer. Intel Macs and
Windows are not supported yet.

Open **Terminal** (press `Cmd` + `Space`, type `Terminal`, press Return), then paste this line
and press Return:

```
curl -fsSL https://github.com/2millerhenry/sf-housing-monitor/raw/HEAD/install.sh | bash
```

That is the whole thing. It asks for no password and opens your browser when it is done.

It takes a few minutes, and most of that is one download: the app brings its own copy of Python
rather than touching the one your Mac came with, so nothing else on your machine changes and
removing it later leaves no trace. If the window looks like it is sitting still, it is working.

<details>
<summary>Rather click than type? Download the ZIP instead.</summary>

<br>

1. **[Download the ZIP](https://github.com/2millerhenry/sf-housing-monitor/releases/latest)**
   and double-click it to unpack.
2. Open the folder. **Control-click** `2 Install SF Home Finder.command`, choose **Open**,
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

### Nothing quietly thrown away

A home that just misses your budget does not vanish — it waits in **Near matches**, where you
can still see it. And if you change your mind in a month, everything ever collected is scored
again against the new answer, so widening your search does not mean starting from an empty
page. [How scoring works](docs/how-it-works.md).

### A number that shows what your deal is costing you

![The shortlist cut-off slider reading "60 and up, 446 homes"](docs/screenshots/match-slider.png)

Tighten your budget or drop a neighborhood and the count moves as you type, so you can see the
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

Only the ZIP does — the one-line command does not.

Apple charges 99 dollars a year to sign an app so that macOS opens it without complaint. This
project does not pay it, so you get *"cannot be opened because it is from an unidentified
developer"* the first time. It is a statement about a receipt, not about the app.

Control-click → **Open** → **Open** is Apple's own way through it, and it is how most free Mac
software gets installed. You only do it once; the installer clears the flag for everything else
in the folder.

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

Double-click **Verify SF Home Finder.command**. It runs a check over the whole app and
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

Then this will not help you much as it stands — the sources, neighborhoods and the city
housing portal are all SF-specific. It is MIT licensed, so you are welcome to fork it.

## For developers

- [Running from source, and the test suite](docs/development.md)
- [Where the listings come from](docs/sources.md)
- [How scoring works, and what it promises when a source breaks](docs/how-it-works.md)

## Worth knowing

This reads sites like Craigslist and Zillow on your behalf, the same pages you could open
yourself. Some of those sites have rules about automated reading, and they change them when
they like. Where you point it, and whether that is alright with the site, is your call.

It comes with no warranty. If it misses the apartment you would have loved, that is bad
luck rather than something anyone owes you for — see [LICENSE](LICENSE).

## Keeping it going

This is free and it stays free. I wrote it because I needed it, and it is more use to other
people than it is sitting on my laptop.

What it costs is upkeep. These sites redesign their pages without telling anyone, and when one
does, that source goes quiet until somebody fixes it — which is me, in the evenings.

If it helped you find somewhere, or just saved you a few weeks of refreshing, there is a donate
link in the app's footer and a Sponsor button on this repo. Entirely optional, and genuinely
so: nothing is locked, nothing is nagged, and nothing gets worse if you skip it.

## License

[MIT](LICENSE).
