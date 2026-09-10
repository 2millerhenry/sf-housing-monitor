# Running from source

For anyone who would rather read the code than trust a download, or wants to change it.

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
[`tests/fixtures/benchmark_profile.yaml`](../tests/fixtures/benchmark_profile.yaml); it is a test
fixture for the ranking benchmark, and the application never reads it.

Inspect or restart the installed background service:

```bash
launchctl print gui/$(id -u)/com.sfhousing.monitor
```


## If you fork this

The donation link lives in two places, and both take an empty value meaning "show nothing":

- `_DEFAULT_DONATE_URL` in [`sf_housing/__init__.py`](../sf_housing/__init__.py) — the line in
  the app's own footer and on its Support page.
- [`.github/FUNDING.yml`](../.github/FUNDING.yml) — the Sponsor button on the repository.

The app root, the login-service label and the Windows task name are deliberately still called
`SF Housing Monitor`. They are where an existing installation keeps its data and how its
scheduled checks are registered, so renaming them would orphan both.
