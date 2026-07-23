# transitous-mcp

MCP server for free public transit routing via [Transitous](https://transitous.org).

## Tools

- `transit_plan` — plan a transit route (bus, train, tram, subway, ferry)
- `transit_geocode` — geocode a place/stop name to coordinates

## Usage

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  transitous:
    command: python3
    args: [/path/to/transitous-mcp/server.py]
```

Also works with Claude Desktop, Cursor, or any MCP client.

```json
{
  "mcpServers": {
    "transitous": {
      "command": "python3",
      "args": ["/path/to/transitous-mcp/server.py"]
    }
  }
}
```

## Requirements

Python 3.8+. Standard library only — no pip installs.

## License

0-clause BSD.
