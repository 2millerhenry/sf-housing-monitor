#!/usr/bin/env python3
"""Build the text of a release page.

The release page is the download page -- the repository's website field points
at it -- so most people reading it have never seen the app. It leads with what
the app is and how to get it, and keeps the version's own changes and the
checksum folded away, because neither is what a first-time reader came for.

Written once here rather than by hand each release, so the page a stranger
lands on does not depend on which day it was published.

    python scripts/release_page.py 0.4.5 > body.md
    gh release create v0.4.5 dist/...zip --notes-file body.md
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "2millerhenry/sf-housing-monitor"
RAW = f"https://raw.githubusercontent.com/{REPO}/main/docs/screenshots"


def changes_for(version: str) -> str:
    """This version's entry from the release notes, without its heading.

    The notes file is the record; repeating it in the release page by hand is
    how the two drift apart.
    """
    notes = (ROOT / "release_assets" / "RELEASE_NOTES.txt").read_text()
    heading = f"SF Home Finder {version}\n"
    if heading not in notes:
        raise SystemExit(f"No entry for {version} in RELEASE_NOTES.txt")
    body = notes.split(heading, 1)[1]
    for line in body.splitlines():
        if line.startswith("SF Home Finder "):
            body = body.split(line, 1)[0]
            break
    # The notes file is hard-wrapped for a terminal; GitHub honours those breaks
    # literally and the result reads like a ransom note. Paragraphs are kept,
    # the wrapping inside them is not.
    return "\n\n".join(
        " ".join(paragraph.split()) for paragraph in body.strip().split("\n\n")
    )


def main() -> None:
    version = sys.argv[1] if len(sys.argv) > 1 else None
    if not version:
        raise SystemExit("usage: release_page.py VERSION")
    archive = ROOT / "dist" / f"SF-Home-Finder-{version}-macOS-arm64.zip"
    if not archive.is_file():
        raise SystemExit(f"Build it first: {archive} is missing")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    print(f"""Finding a place in San Francisco is miserable. This watches 18 rental sites for you, twice a day, on your own laptop — ranked against what you actually want.

<img src="{RAW}/shortlist.png" alt="The shortlist: homes ranked by how well they match, each row showing the score, rent, neighborhood and source">

## Install

Paste this into Terminal:

```
curl -fsSL https://github.com/{REPO}/raw/HEAD/install.sh | bash
```

*19 MB · about 2–3 minutes · no password, no admin · Apple Silicon Mac (M1 or later), macOS 15.6+*

Your browser opens by itself when it is done. Fill in **Your deal**, press save, and the first search starts.

## Opening it later

Type `homefinder` in a terminal, or open **http://127.0.0.1:8000** and bookmark it. It runs on its own, so there is never anything to start.

## Free, private, and quiet

No account, no server, no subscription. It collects nothing about you and your search never leaves your laptop. It never emails a landlord or acts in your name — it reads what is already public and hands it to you.

<details>
<summary>Prefer to click? Download the ZIP below</summary>

<br>

Unpack it, then **Control-click** `2 Install SF Home Finder.command` and choose **Open**, twice. Control-click rather than double-click because the app is not code-signed; the command above has no such prompt, since macOS only marks what a browser downloaded.

</details>

<details>
<summary>What changed in {version}, and checksum</summary>

<br>

{changes_for(version)}

```
{digest}  {archive.name}
```

</details>""")


if __name__ == "__main__":
    main()
