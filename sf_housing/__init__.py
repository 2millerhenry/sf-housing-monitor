"""Local San Francisco housing listing monitor."""

import os

__version__ = "0.4.3"

# The one place the donation link is configured.
#
# Put your own donation page here — Ko-fi, GitHub Sponsors, whatever you
# actually collect money with — and a quiet line appears in the dashboard
# footer and on the Support page. Leave it empty and nothing is rendered at
# all, so a release can never ship a donate link that goes nowhere.
_DEFAULT_DONATE_URL = "https://ko-fi.com/millerhenry"

DONATE_URL = os.environ.get("SF_HOUSING_DONATE_URL", _DEFAULT_DONATE_URL).strip()
