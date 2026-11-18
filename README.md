# Transitous

Public-transport routing via a native `transitous` CLI and MCP server.
Python 3.10+; no runtime dependencies or API key. Queries need internet access.

## Install

Linux/POSIX shell:

```sh
git clone https://github.com/Dthen/transitous-mcp.git
cd transitous-mcp
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps .
. .venv/bin/activate
transitous geocode 'Ayr station' --json
```

Installation may download build tools. Without activation, use
`.venv/bin/transitous`. `python -m transitous_cli` is the module equivalent.

## Commands

Run `transitous COMMAND --help` for all options. MCP tools use the corresponding
`transit_` name with underscores, e.g. `nearby-stops` → `transit_nearby_stops`.

| Command | Purpose / required arguments |
|---|---|
| `plan` | Route between endpoints; use stop IDs or coordinates |
| `geocode QUERY` | Find places, addresses and stops |
| `departures STOP_ID` | Departure board |
| `nearby-stops LAT LON` | Nearby stops |
| `reverse-geocode LAT LON` | Places at a coordinate |
| `trip TRIP_ID` | Trip stop sequence |
| `health` | Provider status |
| `reachable LAT LON` | Places within a travel-time budget |
| `refresh ITINERARY_ID` | Refresh an itinerary |
| `rentals LAT LON` | Nearby shared vehicles |
| `one-to-many` | `--from LAT,LON --destination LAT,LON` (repeat destinations) |
| `one-to-many-transit` | Same endpoints, with public transport |
| `map-trips` | `--bbox MIN_LAT,MIN_LON,MAX_LAT,MAX_LON --start-time T --end-time T` |

Use stop IDs returned by geocoding:

```sh
transitous plan --from-stop-id "$FROM_STOP" --to-stop-id "$TO_STOP" --json
transitous departures "$FROM_STOP" --count 5
```

Each plan endpoint accepts a stop ID **or** its `--from-lat`/`--from-lon` or
`--to-lat`/`--to-lon` pair, not both. Mixing endpoint types is allowed.
Use `--option=value` for leading-dash values, e.g. `--from=-1.2,-4.6`.
Repeat list flags to add values; repeated scalar flags use the last value.

## Results and time

- Human output is the default. `--json` returns one backend JSON object;
  warnings go to stderr. `--help` and `--version` always print plain text.
- Most results have an `outcome`. Errors contain `error`, `outcome`,
  `error_type` and `error_details`. `health`, `refresh` and `rentals` return raw
  provider payloads. Valid empty/no-route answers are not failures.
- Journeys retain provider order. This is a **bounded search, not a whole-day
  optimum**. `max_results` caps results; truncation and active constraints are
  reported. No-route answers do not prove which constraint caused them.
- Prefer offset-bearing times, e.g. `2026-11-18T09:00:00Z`. Local times use the
  origin zone for departures and destination zone for arrive-by searches;
  `--timezone` overrides that frame. Ambiguous/nonexistent DST clocks are
  rejected, never silently shifted. Naive `map-trips` windows require a zone.
- Replay one returned `continuation.previous` or `continuation.next` object with
  `plan --continuation FILE` (`-` reads stdin). Use its whole `{cursor, query}`
  object, not the full result, and do not change the frozen query.
- `--` ends option parsing. Numbers must be finite; command help lists units
  and options. HTTP timeout: 25 seconds per request, no automatic retry.

Exit codes: **0** success/help/version; **2** invalid input;
**1** other errors/output failure; **130** Ctrl-C. CLI-local JSON errors use
`outcome: "cli_error"`; backend errors keep their own shape. An interrupted or
failed write can leave partial output; no replacement JSON is appended.

## MCP setup

After installation:

```yaml
mcp_servers:
  transitous:
    command: /absolute/path/to/transitous-mcp/.venv/bin/transitous-mcp
    protocol: stateless
```

Requires stateless MCP `2026-07-28` (`server/discover`), not the legacy
`initialize` handshake. `python -m transitous_mcp` is equivalent. The existing
source launcher, `python3 /absolute/path/to/transitous-mcp/server.py`, still
works without installation. The CLI never starts an MCP session.

## Development

See [tests/README.md](tests/README.md) for setup, tests and wheel builds.

## Data and license

Routing: [Transitous](https://transitous.org) ([feeds and licenses](https://transitous.org/sources/)).
Map/address/stop data: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL.
Code: [0BSD](LICENSE).

Keep the request contact intact when forking:
`HermesAgent/1.0 (transitous-mcp; +https://github.com/Dthen/transitous-mcp)`.
