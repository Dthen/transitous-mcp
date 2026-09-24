# transitous-mcp

MCP server for free public transit routing via [Transitous](https://transitous.org).

## Tools

The stateless server exposes the frozen 13-tool surface:

- `transit_plan` — plan a transit route
- `transit_geocode` — geocode a place or stop name
- `transit_departures` — fetch a live departure board
- `transit_nearby_stops` — find nearby transit stops
- `transit_reverse_geocode` — resolve coordinates to a stop or address
- `transit_trip` — fetch full trip details
- `transit_health` — check the Transitous API
- `transit_reachable` — find destinations within a travel-time budget
- `transit_refresh` — refresh an itinerary with real-time updates
- `transit_rentals` — find nearby shared bikes, scooters, and cars
- `transit_one_to_many` — route from one origin to many destinations
- `transit_one_to_many_transit` — transit route from one origin to many destinations
- `transit_map_trips` — find active trips in a bounding box

## Protocol

This release targets the stateless `2026-07-28` MCP era. Clients should send
`server/discover`; the server intentionally rejects legacy `initialize` requests
with JSON-RPC `-32601` and keeps the same stdio process available for discovery.
`tools/list` and `tools/call` results include the `resultType`, `ttlMs`, and
`cacheScope` fields. Tool text results intentionally do not declare
`outputSchema` or emit `structuredContent`.

## Usage

Use `/usr/bin/python3` for the deterministic production invocation (Python
3.10+). The pre-cutover live gateway entry currently uses bare `python3`; the
owner-controlled cutover pins the explicit system interpreter:

```yaml
mcp_servers:
  transitous:
    command: /usr/bin/python3
    args: [/path/to/transitous-mcp/server.py]
    protocol: stateless
```

The server also works with other newline-delimited stdio MCP clients. No
package installation or MCP framework is required.

## Version and rollback

The server reports version `2.1.0`; the local lightweight release tag is
`v2.1.0`. The rollback anchor is the preserved lightweight tag
`pre-migration/20260913`. Keep that tag and the pre-migration configuration
path available until the owner completes the migration soak and release gate.

## Requirements

Python 3.10+. Standard library only — no pip installs.

## License

0-clause BSD.
