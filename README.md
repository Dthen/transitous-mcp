# transitous-mcp

MCP server for free public transit routing via [Transitous](https://transitous.org).

Routing data comes from the Transitous API, which aggregates open public-transport
feeds. See the [Transitous data sources](https://transitous.org/sources/) for the
feed list and licences. Map, stop and address data are ©
[OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, available
under the Open Data Commons Open Database License (ODbL).

Requests carry the stable contact
`HermesAgent/1.0 (transitous-mcp; +https://github.com/Dthen/transitous-mcp)` as their
`User-Agent`. Please keep that contact intact if you fork the server: it is how the
public API operator can reach a heavy user.

## Tools

The stateless server exposes a frozen 13-tool surface. Tool names and the MCP
protocol are unchanged; the application result contract is not (see below).

| Tool | Returns |
| --- | --- |
| `transit_plan` | Itineraries between two endpoints, with the effective-query echo and continuation |
| `transit_geocode` | Ranked place, address and stop matches |
| `transit_departures` | A stop's departure board, scheduled and realtime kept apart |
| `transit_nearby_stops` | The nearest stops inside a radius, with stop metadata |
| `transit_reverse_geocode` | The closest places, stops or addresses to a coordinate |
| `transit_trip` | One trip's full stop sequence |
| `transit_health` | **Raw provider payload** from the health endpoint |
| `transit_reachable` | Places reachable within a travel-time budget |
| `transit_refresh` | **Raw provider payload** for a refreshed itinerary |
| `transit_rentals` | **Raw provider payload** of nearby shared vehicles (GBFS) |
| `transit_one_to_many` | Street durations from one point to many |
| `transit_one_to_many_transit` | Street duration plus the transit Pareto set per destination |
| `transit_map_trips` | Active trips inside a bounding box and time window |

Ten of the thirteen tools return one canonical envelope. `transit_refresh`,
`transit_rentals` and `transit_health` are the only exceptions: their descriptions
say so, and their successful payload is the upstream response unchanged.

## Native command line

The same application backend is available as a standalone `transitous` command.
It calls `transit_core.backend.run(operation, arguments)` directly — no MCP
client, protocol session, server process or discovery step — and never imports
the MCP adapter. Both interfaces ship in one distribution:

| entry point | form |
| --- | --- |
| `transitous` | installed executable (`transitous_cli.cli:main`) |
| `python -m transitous_cli` | module equivalent |
| `transitous-mcp` | installed MCP server (`transitous_mcp.server:main`) |
| `python -m transitous_mcp` | module equivalent |
| `/usr/bin/python3 /path/to/repo/server.py` | unchanged source checkout launcher |

Installing the package is optional for the MCP source-script workflow above: the
root `server.py` remains a thin compatibility launcher over
`transitous_mcp.server`.

### Install

Install a noneditable wheel in an isolated environment. It has no runtime
dependency:

```sh
python3 -m venv ~/.local/venvs/transitous
~/.local/venvs/transitous/bin/pip install --no-index --no-deps /absolute/path/to/transitous_mcp-3.0.0-py3-none-any.whl
~/.local/venvs/transitous/bin/transitous --version
~/.local/venvs/transitous/bin/transitous --help
```

`pipx install /absolute/path/to/transitous_mcp-3.0.0-py3-none-any.whl` is an
optional way to put the executable on `PATH`. Building from source and installing
development dependencies may contact the package index:

```sh
python3 -m pip install build
python3 -m build
python3 -m pip install --no-index --no-deps dist/transitous_mcp-3.0.0-py3-none-any.whl
```

The wheel is the offline runtime install; the source build and
`tools/dev-requirements.txt` are development/network paths. The editable source
pointer (`-e .[dev]`) is development-only and never needed at runtime.

### Commands

`T` and `Z` mean an unmodified time string and IANA zone string. Every optional
argument is absent unless supplied; the CLI does not send `None`, empty lists or
copies of domain defaults.

| Command | Arguments |
| --- | --- |
| `plan` | `[--from-stop-id ID \| --from-lat LAT --from-lon LON] [--to-stop-id ID \| --to-lat LAT --to-lon LON] [--time T] [--arrive-by \| --no-arrive-by] [--timezone Z] [--origin-timezone Z] [--destination-timezone Z] [--max-transfers N] [--transit-mode MODE]... [--num-itineraries N] [--max-itineraries N] [--max-results N] [--search-window-seconds N] [--continuation FILE\|-] [--include-direct \| --no-include-direct] [--max-access-walk-minutes N] [--max-egress-walk-minutes N] [--min-transfer-minutes N]` |
| `geocode` | `QUERY [--type KIND]... [--mode MODE]... [--near LAT,LON] [--near-bias NUMBER] [--num-results N] [--language TEXT]` |
| `departures` | `STOP_ID [--count N] [--time T] [--window SECONDS] [--timezone Z] [--endpoint-timezone Z]` |
| `nearby-stops` | `LAT LON [--radius METRES]` |
| `reverse-geocode` | `LAT LON [--type KIND]... [--num-results N]` |
| `trip` | `TRIP_ID` |
| `health` | no operation arguments |
| `reachable` | `LAT LON [--max-time-min N] [--time T] [--arrive-by \| --no-arrive-by] [--timezone Z] [--endpoint-timezone Z]` |
| `refresh` | `ITINERARY_ID` |
| `rentals` | `LAT LON [--radius METRES]` |
| `one-to-many` | `--from LAT,LON --destination LAT,LON ... [--mode PROFILE] [--max-sec NUMBER] [--arrive-by \| --no-arrive-by]` |
| `one-to-many-transit` | `--from LAT,LON --destination LAT,LON ... [--time T] [--arrive-by \| --no-arrive-by] [--max-transfers N] [--transit-mode MODE]... [--max-access-walk-minutes N] [--max-egress-walk-minutes N] [--max-travel-minutes N] [--timezone Z] [--endpoint-timezone Z]` |
| `map-trips` | `--bbox MIN_LAT,MIN_LON,MAX_LAT,MAX_LON --start-time T --end-time T [--zoom N] [--timezone Z]` |

Option names are exact: no abbreviations, no aliases, case-sensitive. Repeatable
list options (`--type`, `--mode`, `--transit-mode`, `--destination`) append in
caller order and are never split, sorted or deduplicated; `geocode --language
"a,b"` is one string. Repeating a scalar or boolean uses the last supplied value.
`--from LAT,LON`, `--destination LAT,LON`, `--near LAT,LON` and `--bbox` are the
only comma grammars. Integers are signed base-10 tokens; coordinates, `--near-bias`
and `--max-sec` are finite decimal/exponent numbers. A malformed number, tuple or
bounding box is a CLI usage error before any request. Leading-dash values use the
`=` spelling (`--from=-1.2,-4.6`, `--from-lon=-4.6`) or `--` before an opaque
identifier (`trip -- '-opaque-id'`).

In `plan`, endpoints are not parser-required because a continuation object supplies
them; the core owns completeness, mixed-form (`mixed_endpoint_form`) and
contradiction errors, and the existing 1..50 destination limit. One-to-many
requires `--from` and at least one `--destination`; `map-trips` requires `--bbox`,
`--start-time` and `--end-time`, as the table shows.

### Output and exit status

Human-readable output is the default, whether or not stdout is a TTY. `--json`
selects exactly one JSON operation document, structurally equal to the backend
result: no wrapper, added status or exit-code field. It may appear before the
command or among its options; repetition is harmless. `--` ends option
recognition, so a later `--json` is a literal operand, and `--time=--json` is a
time value, not an output selector. `--help` and `--version` are always plain
management text, even with `--json`.

| status | meaning |
| --- | --- |
| 0 | completed answer (`ok`, `no_routes`, empty data, raw payload not marked as an error), or help/version |
| 2 | CLI syntax/numeric/input-file error, or a backend error marked by the shared classifier with outcome `invalid_input` |
| 1 | every other marked backend error, a CLI internal error, or an output failure |
| 130 | keyboard interruption |

A marked backend error is a dict with a truthy `error` member. A raw `outcome`
string without that marker is not a failure, and a raw error dict with an absent or
unrecognised outcome is status 1. `health`, `refresh` and `rentals` remain decoded
raw payloads on success; human mode prints the whole payload as pretty JSON unless
it is marked as an error.

Human output is intentionally lossy and never a second routing engine: plan prints
endpoints, the effective search frame, query window/counts, active constraints,
truncation with its reason and dropped count, the no-routes reason when present,
and every returned itinerary, direct street-only alternative and leg in backend
order, with canonical timestamp display values (zone/offset provenance visible) and
the statement that it is a bounded search. Other commands print every returned row
in order; optional metadata not projected is available with `--json`. Backend
warnings are printed to stderr in order and are never deduplicated.

CLI-local failures (usage, continuation input, unexpected internal fault,
interruption) are not disguised as upstream errors. In JSON mode, before any
stdout document is written, the single diagnostic is:

```json
{"error":"Human-readable bounded message","outcome":"cli_error","error_type":"usage","error_details":{}}
```

`error_type` is `usage`, `input_file`, `internal_error` or `interrupted`. In human
mode the same diagnostic goes to stderr and stdout stays empty. Core-classified
backend errors keep their original shape and are written unchanged.

Stream ownership is explicit. After any stdout write attempt, a broken pipe,
failed flush or interruption returns 1 (or 130 for interruption) without appending
a replacement document. A missing stdout or an unusable/closed stdout descriptor is
an output failure: status 1, no traceback, and no shutdown error. When stderr is
missing or broken, the diagnostic is omitted but the caller's classified status is
preserved (`2` for usage/input, `1` for a backend error, `0` for success) — a broken
diagnostic stream never becomes a different exit status. An unreadable or closed
continuation stdin (`--continuation -`) is a local `input_file` error (status 2)
before any backend call, exactly like an unreadable continuation file. A real
interruption — including one delivered while a stderr diagnostic or warning is being
written — returns 130 with no traceback and no replacement document.

### Time

No CLI-side time resolution, host-zone fallback or date parsing happens. Explicit
instants, naive local clocks, date-only strings and omission reach the core
unchanged; see [Time semantics](#time-semantics) for the direction-dependent
endpoint frame. `map-trips` needs `--timezone` only to read a naive window, not for
offset-bearing instants.

### Continuation

`plan --continuation FILE` accepts one previously returned `continuation.previous`
or `continuation.next` object (`{cursor, query}`), or `-` to read stdin once to EOF.
The file must be UTF-8 JSON without a BOM, duplicate keys or NaN/Infinity, and the
decoded document must be one object; anything else is a local `input_file` error
(status 2) before any HTTP request. The whole object is forwarded unchanged: the
core alone validates the frozen query, and a full result object or `{previous,
next}` is the core's canonical `invalid_input` error. Ordinary plan flags supplied
alongside the object are forwarded too; a contradiction is named by the core as
`cursor_without_identical_query`.

```sh
transitous plan --from-stop-id "$FROM_STOP" --to-stop-id "$TO_STOP" \
  --time 2026-10-27T09:00:00Z --json > result.json
# First check the call succeeded and continuation.next is non-null.
jq '.continuation.next' result.json > next.json
transitous plan --continuation next.json --json
```

The fixed time above illustrates syntax; it is not a promise of live timetable
coverage.

## Result contract

`transit_core` owns every journey rule; `server.py` is a thin adapter over it.

- **One outcome per call.** `outcome` is one of `ok`, `no_routes`,
  `invalid_input`, `unsupported`, `out_of_range`, `upstream_rejected`,
  `transport_error`, `malformed_upstream`, `constraint_violation`. An error result
  has exactly `error`, `outcome`, `error_type` and `error_details`; `error_type` is
  a named reason (for example `ambiguous_local_time`) and `error_details` carries
  the machine-readable remedy. The MCP result sets `isError: true` for every error
  outcome and never emits `structuredContent` or an `outputSchema`.
- **Empty is not invalid.** A structurally valid empty answer is `no_routes` with
  `no_routes_reason` (`street_only_alternatives_returned` or `not_determined`) and
  `constraints_active`, which lists the restrictions that were supplied. It never
  claims which of them caused the empty set, and no relaxed control request is
  issued to manufacture a cause.
- **One timestamp object.** Every reported instant is
  `{iso, epoch_seconds, local, local_date, utc_offset_seconds, zone, zone_source,
  display}`, rendered in the zone that owns it. There is no second ISO field and no
  bare clock string. `zone_source` names the provenance (`response_place`,
  `caller_endpoint`, `caller_timezone`, `inferred_nearby_stop`, `unavailable`); a
  payload without a zone renders in its true offset with `zone: null` and a
  warning, never a guessed zone.
- **Numbers are honest.** Durations and distances are seconds and metres, integers
  where the source is integral. An unknown value is `null`, never `0`. A genuine
  zero stays `0`. Optional members that the pinned schema does not require are
  tri-state or `null`, never inferred.

## Time semantics

Each tool documents which endpoint owns the frame:

- `transit_plan`: a naive clock or date-only value is read in the **origin** for a
  `depart_after` search and in the **destination** for an `arrive_by` search. A
  supplied `timezone` is a deliberate caller frame and overrides both readings;
  `origin_timezone` / `destination_timezone` declare an endpoint's display zone and
  end zone discovery for that endpoint.
- `transit_departures`, `transit_reachable`, `transit_one_to_many_transit`: a naive
  clock is read at the single `one` endpoint (the stop, or the endpoint that is the
  origin or destination for the requested direction). `endpoint_timezone` declares
  it explicitly.
- `transit_map_trips`: a bounding box has no single endpoint, so a naive
  `start_time`/`end_time` requires an explicit caller `timezone`; without one the
  call is `invalid_input` / `bbox_window_requires_explicit_zone`. Explicit-offset
  instants need no zone.
- An explicit offset instant (`2026-10-27T09:00:00Z`) is preserved; Transitous
  accepts it and the server never re-reads or shifts it.
- Zone discovery is bounded to **one** request per call, and only against the
  direction-relevant endpoint: the stop endpoint for a stop id, or one reverse
  geocode for a coordinate, accepted only when the nearest stop is within 500 m.
  That inference is evidence about the neighbourhood, not a lookup of the exact
  coordinate. Caller input and explicit instants cost zero discovery requests.

### DST rejection and remedies

A wall clock that the effective zone skips or repeats is rejected as soon as that
zone is known, and before the request that would answer the call is sent:

- `invalid_input` / `nonexistent_local_time` names the gap (`error_details.gap`).
- `invalid_input` / `ambiguous_local_time` gives both candidate offsets and
  instants (`error_details.candidates`) so the ambiguity is visible.

When the zone comes from caller input — `timezone`, or an endpoint's zone
declaration — the check costs no request at all. When the zone has to be discovered,
the single bounded discovery request described above happens first (a stop lookup, or
a reverse geocode for a coordinate), and the rejection still precedes the request
that would answer the call.

Each reason carries its own remedy in `error_details.remedy`:

- A skipped (nonexistent) clock needs a valid local clock, or an explicit instant
  that carries its own offset.
- A repeated (ambiguous) clock needs an explicit offset that selects the intended
  occurrence, or a timezone whose clock is valid and unambiguous at that reading.

Supplying a `timezone` does not by itself make an invalid or repeated reading valid:
the clock is then read in that zone, and it is rejected if that zone skips or repeats
it too. Without an offset no fold is chosen, and no clock is moved silently.
Transitous, unlike some providers, accepts explicit instants and inherits none of
their restrictions.

## Stop ids, coordinates and metadata

Each `transit_plan` endpoint independently uses a stop id (`from_stop_id`,
`to_stop_id`, sent verbatim) or a coordinate pair. Using both forms for one endpoint
is `mixed_endpoint_form`; using different forms for the two endpoints is the
intended station-to-station comparison. A stop-id endpoint removes that endpoint's
access/egress walk and nothing else.

`transit_geocode` and `transit_reverse_geocode` report `match_type`, `match_id`,
`stop_id`, `modes`, `timezone`, `importance`, areas and the other `Match` members.
`stop_id` is populated only for a `STOP` match and is then equal to `match_id`; the
geocode `Match` schema has no parent grouping, so no `parent_id` is synthesised.
`transit_nearby_stops` reads the `Place` schema, so it does report `parent_id`,
`vertex_type` and modes when the payload supplies them, and it returns the nearest
20 stops inside the requested radius — nearest-first within the window that was
requested, not a guaranteed global nearest set (a clamped antimeridian box or a
polar box is narrower than the circle).

`types` restricts match kinds and `modes` filters stops by available transport
modes; both reach the wire as one comma-joined value in the caller's order and are
never enforced client-side. `near` biases ranking and filters nothing. `RAIL` is a
parent mode covering high-speed, long-distance, night, regional, suburban and subway
classes, so it does not separate mainline from Metro; `REGIONAL_RAIL`,
`LONG_DISTANCE`, `METRO` and `SUBWAY` are the values that distinguish service
classes.

## Search context and continuation

`transit_plan` reports the search it actually ran and the caps it applied:

- `query.control`, `requested_instant`, `effective_instant`,
  `search_window_seconds`, `minimum_requested`, `upstream_count`,
  `returned_count`, `max_results`, `truncated`, `truncation_reason`,
  `dropped_count`, both upstream page cursors, `requested_modes`,
  `applied_modes` (the exact comma-joined value sent) and `realtime_present`.
- Results are in upstream order and are never re-sorted, de-duplicated or reduced
  to "the best". The set is a bounded search context, **not a whole-day optimum**.
- `max_results` is the only wrapper cap and it truncates after upstream ordering;
  `truncated` is defined as `upstream_count > returned_count`, never inferred from
  a count equality. Leaving it unset returns every itinerary the upstream supplied.
- `direct` holds the upstream's street-only alternatives when `include_direct` is
  true; they are full itineraries, not a summary.
- `continuation` is `{previous, next}`, each `null` or `{cursor, query}`. The query
  is the complete frozen effective query (endpoint forms, control, the resolved
  instant, zones, every cap, ordered modes, `include_direct`). Replay by passing the
  whole object; omitted time is resolved once on the first call and frozen, so a
  paged call cannot shift its search time. A separately supplied argument that
  contradicts it is `invalid_input` / `cursor_without_identical_query` with
  `error_details.fields` naming each difference. There is no `page_cursor`
  argument, no server state and no signed-query claim: this is consistency checking,
  not tamper resistance.

## Limits

| Limit | Value |
| --- | --- |
| `transit_plan` `max_transfers` / `num_itineraries` / `max_itineraries` / `max_results` | 0..10 / 1..20 / 1..50 / 1..50 |
| `transit_plan` `search_window_seconds` | 60..86400 (default 900) |
| `transit_plan` walk and transfer caps | 0..120 minutes each |
| `transit_geocode` / `transit_reverse_geocode` `num_results` | 1..40 (geocode default 10) |
| `transit_nearby_stops` `radius` | 1..5000 m (default 1000); 20 results |
| `transit_departures` `count` / `window` | 1..50 (default 10) / 1..86400 s |
| `transit_reachable` `max_time_min` | 1..1440 (default 60) |
| `transit_one_to_many*` `destinations` | 1..50 pairs |
| `transit_map_trips` `zoom` / results | 1..19 (default 12) / 50 segments |
| Multiplexed arrays | one comma-joined value per parameter, no repeated keys |
| HTTP timeout | 25 s, one request, no retry |

## Protocol

This release targets the stateless `2026-07-28` MCP era. Clients should send
`server/discover`; the server intentionally rejects legacy `initialize` requests
with JSON-RPC `-32601` and keeps the same stdio process available for discovery.
`server/discover`, `tools/list` and `tools/call` results include the `resultType`,
`ttlMs` and `cacheScope` fields and `_meta` server info. Tool text results
intentionally do not declare `outputSchema` or emit `structuredContent`.

Protocol faults stay separate from tool failures: a missing `params` object or a
non-string `params.name` is JSON-RPC `-32602`, an absent method is `-32601`, and an
unexpected internal fault is `-32603`. A tool failure is a normal result whose text
is the canonical error object and whose `isError` is `true`.

## Application boundary

`transit_core.backend` is the protocol-independent operation boundary. It exposes
the 13 canonical operations (`plan`, `geocode`, `departures`, `nearby_stops`,
`reverse_geocode`, `trip`, `health`, `reachable`, `refresh`, `rentals`,
`one_to_many`, `one_to_many_transit`, `map_trips`) through
`backend.run(operation, arguments)`, and it returns the same result and error
objects the MCP surface returns. Only a classified `ToolError` is converted to the
canonical error result; an unexpected programming failure propagates instead of
being reported as a user-input or upstream fault. `server.py` translates the
`transit_<operation>` tool names onto that inventory and keeps the JSON-RPC
messages, the era envelope, `isError` and the tool registry; neither the backend
nor the core imports `server`. `health`, `refresh` and `rentals` still return the
decoded provider payload unchanged on success, with the canonical error object on
failure. Importing `server` or `transit_core` has no side effect: stdin is
reconfigured only when the stdio loop starts.

## Usage

Use `/usr/bin/python3` for the deterministic production invocation (Python 3.10+).
That explicit system interpreter is the configured command:

```yaml
mcp_servers:
  transitous:
    command: /usr/bin/python3
    args: [/path/to/transitous-mcp/server.py]
    protocol: stateless
```

The server also works with other newline-delimited stdio MCP clients. No package
installation or MCP framework is required. An installed environment exposes the
same adapter as `transitous-mcp` or `python -m transitous_mcp`; switching an
existing Hermes configuration to that executable is a separate operator decision,
and the root-script configuration above keeps working unchanged.

## Version and rollback

The server reports application version `3.0.0`, a breaking application contract:
tool names and the MCP protocol are unchanged, the result, time and continuation
contracts are not. That version has one source, `transit_core.APPLICATION_VERSION`;
`server.py` (and `transitous_mcp.server`) reads it into `serverInfo`, the CLI and
installed distribution metadata read the same literal, and
`transit_core.__version__` is an alias. The previously internal core version
`0.1.0` is reconciled to `3.0.0` in this unreleased tree — the application
contract itself is unchanged and not bumped.
The rollback anchor is commit
`4a247f2871e23c013b20e6421d8ff834b82bba11` (version 2.1.0 with its committed
`golden/transitous.tools.json`); restoring that commit's `server.py`, `golden/`,
`README.md` and removing `transit_core/` and the added `tests/` files returns the
previous surface. No state is migrated and no runtime configuration changes. The
lightweight tag `pre-migration/20260913` is preserved but predates this work and is
not the rollback anchor.

## Requirements

Python 3.10+. Standard library only — no pip installs, no runtime dependency.

Tests use pytest in a development environment only. From a fresh checkout:

```sh
python3 -m venv .venv-dev
.venv-dev/bin/pip install -r tools/dev-requirements.txt
.venv-dev/bin/python -m pytest -q -p no:cacheprovider
```

`tools/dev-requirements.txt` is a reference shim for `-e .[dev]` and must be run
from the project root; the one authoritative development dependency declaration is
`[project.optional-dependencies].dev = ["pytest>=8,<10"]` in `pyproject.toml`.
`server.py`, `transitous_mcp/`, `transitous_cli/` and `transit_core/` never import
pytest. Creating the virtualenv and installing pytest may need the package index;
running the suite itself needs no network.

The regression suite is self-contained: every API-shaped body it reads is a
small constructed JSON file under `tests/fixtures/`, labelled `synthetic` in that
directory's manifest. No external capture corpus, environment variable or
data-generation step is required, and the suite does not require `PYTHONPATH` or
`PYTHONHOME` (the interpreter still honours them if they are set).

The suite exercises the request and mapping contracts against those constructed
bodies; it is **not** a live-service check. The wire-level tests use a fake
transport and never open a socket.

## License

0-clause BSD.
