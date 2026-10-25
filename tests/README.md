# Transitous foundation tests

Application code and loaders live here; **captured response bodies do not**. The
corpus is scratch data kept outside the checkout (see amendment 02 of the T1
implementation plan), so the suite must be told where it is.

## Running

```sh
TRANSITOUS_TEST_FIXTURES="$TRANSITOUS_TEST_FIXTURES" \
PYTHONDONTWRITEBYTECODE=1 .venv-dev/bin/python -m pytest -q -p no:cacheprovider
```

`TRANSITOUS_TEST_FIXTURES` must point at the directory holding the response
bodies plus their provenance manifest (`README.md`, one row per file). Point it
at your own scratch copy; no capture path is baked into this tree.

If the variable is unset, or the directory does not exist, collection stops with
an explicit error naming `TRANSITOUS_TEST_FIXTURES` — the suite never skips
quietly or falls back to a repository copy, because a green run must mean the
captures were actually read.

## What is in here

| file | role |
| --- | --- |
| `fixture_support.py` | external-corpus loader, manifest verifier (hashes, kind labels, gzip rule, URL-vs-constraints agreement), credential scan |
| `test_fixtures_integrity.py` | manifest/provenance gates, loader behaviour, non-vacuity of the checker, corpus-diagnostic guards |
| `test_time_model.py` | §2.1–§2.3 forms, frame choice, display-zone provenance, host-zone invariance |
| `test_zone_resolution.py` | §4.3 authority order, 500 m inference bound, request budget, zone provenance |
| `test_dst_policy.py` | fold/gap policy, both hemispheres, no request on rejection |
| `test_http_layer.py` | §4.2 serialisation, error taxonomy, timetable window, excerpt hygiene |
| `test_foundation_mappers.py` | §3.1 duration vectors, calendars, place/cancellation mapping |

Fixtures above 300 KB are stored gzipped (`*.json.gz`); the loader returns the
uncompressed logical bytes, which is what the manifest hashes.
