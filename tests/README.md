# Transitous regression tests

The suite is self-contained: application code and test loaders live here, and so
do the **constructed** example bodies it reads (`fixtures/`). There is no capture
corpus, no external data path and no network access.

## Running

From a fresh checkout, create the development environment once:

```sh
python3 -m venv .venv-dev
.venv-dev/bin/pip install -r tools/dev-requirements.txt
```

Installing pytest may need the package index. Then run the tests, which
themselves are offline:

```sh
.venv-dev/bin/python -m pytest -q -p no:cacheprovider
```

`tools/dev-requirements.txt` pins the only test dependency. The suite does not
read `TRANSITOUS_TEST_FIXTURES` and does not require `PYTHONPATH` or
`PYTHONHOME`; the interpreter still honours those variables if they are set, but
no value is needed for a green run. It also needs no setup or data-generation
step. A missing required fixture fails loudly with its checked-in path rather
than skipping.

## What the inputs are

`fixtures/` holds small JSON documents shaped like the pinned API responses. They
were **constructed for these tests** against the documented schema; the numbers
in them express the vectors the assertions name (for example the 14100-second
Ayr–Newcastle itinerary). They are not captured live responses, not copies of
production responses and not derived from one. `fixtures/README.md` is the
manifest: each file's kind (`synthetic`), SHA-256 and purpose, checked by
`test_fixtures_integrity.py`. Every filename carries a `synthetic_` prefix so no
body can be mistaken for a recording.

Because the inputs are constructed, the suite verifies request assembly, mapping
and failure classification against explicit examples. It is not evidence that the
live service currently returns those shapes; live checks are out of scope.

| file | role |
| --- | --- |
| `fixture_support.py` | checked-in fixture loader (no environment, fresh object per call), manifest verifier, token scan |
| `test_fixtures_integrity.py` | manifest coverage/hashes, synthetic-only labelling, path safety, loader isolation, non-vacuity of the checker |
| `test_time_model.py` | §2.1–§2.3 forms, frame choice, display-zone provenance, host-zone invariance |
| `test_zone_resolution.py` | §4.3 authority order, 500 m inference bound, request budget, zone provenance |
| `test_dst_policy.py` | fold/gap policy, both hemispheres, no request on rejection |
| `test_http_layer.py` | §4.2 serialisation, error taxonomy, timetable window, excerpt hygiene |
| `test_foundation_mappers.py` | §3.1 duration vectors, calendars, place/cancellation mapping |
| `test_plan_contract.py` | §4.4–§4.6 request contract, caps, cursors, continuation |
| `test_plan_durations.py` | §3.1 decomposition vectors and conservation |
| `test_plan_cancellations.py` | §4.13 walking-failure and cancellation attribution |
| `test_boards.py` | §4.9–§4.11 departures, trip and map segments |
| `test_geocode_stops.py` | §4.7–§4.8 match identity, filters, nearby stops |
| `test_one_to_many.py` | §4.12 street, intermodal and reachable paths |
| `test_backend_boundary.py` | the 13-operation backend, its error boundary, raw endpoints, shared mode vocabulary and import safety |
| `test_tools_call_e2e.py` | the 13-tool JSON-RPC surface through a fake transport |
