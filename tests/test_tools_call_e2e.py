"""End-to-end ``tools/call`` acceptance over the real stdio JSON-RPC loop (§9.1).

Every case starts a real child process on the production interpreter
(``/usr/bin/python3``). The child is a **test-owned entry** written into pytest's
temporary directory for that case: it patches ``urllib.request.urlopen`` with a
fixture transport *before* the production modules are loaded, then loads this
checkout's ``server.py`` with ``runpy`` and calls its real ``main``. So the loop,
the era envelope and the tool wiring under test are the production ones.

There is no shadow package and no production test-mode switch. The child imports
the checkout because ``PYTHONPATH`` is the checkout, and it records the resolved
module origins afterwards so the test can prove which bytes ran. Every upstream
URL the tools actually assembled is logged by the fake transport; an unmatched URL
is recorded and raised, never handed to a socket. The socket module is sealed in
the child as a second guarantee.

Example bodies come from the checked-in constructed fixtures through
``fixture_support``. Bodies built inside a test are inline and labelled
``synthetic``.
"""

import json
import os
import select
import subprocess
import sys
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import fixture_support
import pytest

REPO = fixture_support.REPO_ROOT
SERVER = os.path.join(REPO, "server.py")
PROD_PY = "/usr/bin/python3"
era = "2026-07-28"
READ_TIMEOUT = 60
CALL_TIMEOUT = 120

TOOL_NAMES = (
    "transit_plan",
    "transit_geocode",
    "transit_departures",
    "transit_nearby_stops",
    "transit_reverse_geocode",
    "transit_trip",
    "transit_health",
    "transit_reachable",
    "transit_refresh",
    "transit_rentals",
    "transit_one_to_many",
    "transit_one_to_many_transit",
    "transit_map_trips",
)

#: Substrings each description must contain (§4.14, §4.4–§4.13). They are the
#: contract statements a caller needs, not a prose style check.
DESCRIPTION_TOKENS = {
    "transit_plan": (
        "depart_after",
        "arrive_by",
        "destination zone",
        "offset-bearing instant",
        "ambiguous_local_time",
        "nonexistent_local_time",
        "continuation",
        "max_results",
        "truncated",
        "direct",
        "no_routes",
        "constraints_active",
        "whole-day optimum",
        "RAIL is a parent mode",
    ),
    "transit_geocode": (
        "biases",
        "filters nothing",
        "comma-joined",
        "match_id",
        "parent_id",
        "1..40",
    ),
    "transit_nearby_stops": (
        "nearest 20",
        "1..5000",
        "requested window",
        "not a guaranteed",
    ),
    "transit_reverse_geocode": ("match_id", "stop_id", "num_results", "parent_id"),
    "transit_departures": (
        "scheduled_departure",
        "expected_departure",
        "realtime",
        "delay_seconds",
        "tri-state",
        "stop's own zone",
    ),
    "transit_trip": ("no time input", "intermediate", "cancellation"),
    "transit_map_trips": (
        "bounding box",
        "bbox_window_requires_explicit_zone",
        "vehicle_position",
        "realTime",
    ),
    "transit_reachable": ("travel_time_seconds", "transfers_k", "max_time_min"),
    "transit_one_to_many": ("one_role", "fractional", "1..50", "arrive_by"),
    "transit_one_to_many_transit": (
        "Pareto",
        "whole minutes",
        "applied_modes",
        "one_role",
    ),
    "transit_refresh": ("raw provider payload",),
    "transit_rentals": ("raw provider payload",),
    "transit_health": ("raw provider payload",),
}

#: The child entry. It receives its routes, repo, server path and log path through
#: the environment, patches the transport before importing anything production and
#: writes one JSON log when the server's ``main`` returns.
CHILD_SOURCE = '''\
import json
import os
import runpy
import socket
import sys
import urllib.request

REPO = os.environ["E2E_REPO"]
SERVER = os.environ["E2E_SERVER"]
ROUTES_PATH = os.environ["E2E_ROUTES"]
LOG_PATH = os.environ["E2E_URL_LOG"]

with open(ROUTES_PATH, encoding="utf-8") as handle:
    ROUTES = json.load(handle)

STATE = {"urls": [], "unexpected": [], "socket_attempts": [], "routes_used": []}


class StubResponse:
    """A response shaped like what ``http.api_get`` reads."""

    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    def read(self):
        return self._payload

    def getcode(self):
        return self.status

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def transport(request, timeout):
    url = request.full_url
    STATE["urls"].append(url)
    for index, route in enumerate(ROUTES):
        if route["match"] in url:
            STATE["routes_used"].append(index)
            if "body" in route:
                payload = json.dumps(route["body"]).encode("utf-8")
            else:
                payload = route["raw"].encode("utf-8")
            return StubResponse(route.get("status", 200), payload)
    STATE["unexpected"].append(url)
    raise AssertionError("unexpected upstream URL: " + url)


urllib.request.urlopen = transport


def _sealed(*args, **kwargs):
    STATE["socket_attempts"].append(repr(args))
    raise AssertionError("a live socket was attempted")


socket.socket.connect = _sealed
socket.socket.connect_ex = _sealed
socket.create_connection = _sealed

sys.path.insert(0, REPO)

try:
    namespace = runpy.run_path(SERVER, run_name="__main__")
finally:
    import transit_core.boards
    import transit_core.errors
    import transit_core.geocode
    import transit_core.http
    import transit_core.onetomany
    import transit_core.plan
    import transit_core.rendering
    import transit_core.timeparse
    import transit_core.timezones

    modules = (
        transit_core.boards,
        transit_core.errors,
        transit_core.geocode,
        transit_core.http,
        transit_core.onetomany,
        transit_core.plan,
        transit_core.rendering,
        transit_core.timeparse,
        transit_core.timezones,
    )
    log = dict(STATE)
    log["origins"] = {module.__name__: os.path.realpath(module.__file__) for module in modules}
    log["server_file"] = os.path.realpath(namespace.get("__file__", ""))
    with open(LOG_PATH, "w", encoding="utf-8") as handle:
        json.dump(log, handle)
'''


# ── the session driver ──────────────────────────────────────────────


def _read_line(stream, timeout=READ_TIMEOUT):
    ready, _, _ = select.select([stream], [], [], timeout)
    assert ready, f"no JSON-RPC response within {timeout}s"
    line = stream.readline()
    assert line.strip(), "the server closed stdout without a response"
    return line


class Session:
    """One child server process, one JSON-RPC conversation, one transport log."""

    def __init__(self, tmp_path, routes, name):
        self.routes_path = tmp_path / f"{name}.routes.json"
        self.log_path = tmp_path / f"{name}.log.json"
        self.child_path = tmp_path / f"{name}.child.py"
        self.routes_path.write_text(json.dumps(routes), encoding="utf-8")
        self.child_path.write_text(CHILD_SOURCE, encoding="utf-8")
        self.process = None
        self.log = None

    def __enter__(self):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": REPO,
            "PYTHONDONTWRITEBYTECODE": "1",
            "E2E_REPO": REPO,
            "E2E_SERVER": SERVER,
            "E2E_ROUTES": str(self.routes_path),
            "E2E_URL_LOG": str(self.log_path),
        }
        self.process = subprocess.Popen(
            [PROD_PY, str(self.child_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(self.child_path.parent),
            env=env,
        )
        return self

    def __exit__(self, exc_type, exc_info, traceback):
        if self.process is None:
            return False
        try:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            self.process.wait(timeout=CALL_TIMEOUT)
            trailing = self.process.stdout.read()
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
        if exc_type is not None:
            # The body already failed; a cleanup assertion here would mask it.
            return False
        assert self.process.returncode == 0, self.process.stderr.read()
        assert trailing == "", f"unexpected trailing stdout: {trailing!r}"
        self.log = json.loads(self.log_path.read_text(encoding="utf-8"))
        return False

    def send(self, payload):
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def rpc(self, payload):
        self.send(payload)
        return json.loads(_read_line(self.process.stdout))

    def call(self, name, arguments=None, rid=2):
        return self.rpc(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "tools/call",
                "params": {"name": name, "arguments": {} if arguments is None else arguments},
            }
        )


def assert_era(result):
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == 0
    assert result["cacheScope"] == "private"
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"] == {
        "name": "transitous-mcp",
        "version": "3.0.0",
    }
    assert "structuredContent" not in result


def tool_payload(response):
    """The parsed canonical payload of one successful ``tools/call`` result."""
    result = response["result"]
    assert_era(result)
    assert result.get("isError") is not True, result
    assert isinstance(result["content"], list) and len(result["content"]) == 1
    block = result["content"][0]
    assert block["type"] == "text"
    return json.loads(block["text"])


def tool_error(response):
    """The parsed canonical error payload of one failed ``tools/call`` result."""
    result = response["result"]
    assert_era(result)
    assert result["isError"] is True, result
    payload = json.loads(result["content"][0]["text"])
    assert set(payload) == {"error", "outcome", "error_type", "error_details"}, payload
    assert payload["outcome"] not in ("ok", "no_routes")
    return payload


def query_of(url):
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def one_value(url, name):
    values = query_of(url).get(name)
    assert values is not None, f"{name} absent from {url}"
    assert len(values) == 1, f"{name} repeated in {url}: {values}"
    return values[0]


def fixture_body(name):
    return fixture_support.load(name)


def route(match, body=None, status=200, raw=None):
    item = {"match": match, "status": status}
    if raw is not None:
        item["raw"] = raw
    else:
        item["body"] = body
    return item


def assert_no_unexpected(session):
    assert session.log["unexpected"] == []
    assert session.log["socket_attempts"] == []


# ── the child runs this checkout's bytes and no live transport ──────


def test_child_imports_this_checkout_and_never_opens_a_socket(tmp_path):
    with Session(tmp_path, [], "origins") as session:
        response = session.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert len(response["result"]["tools"]) == 13
        session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    assert session.log["server_file"] == os.path.realpath(SERVER)
    assert set(session.log["origins"]) == {
        "transit_core.boards",
        "transit_core.errors",
        "transit_core.geocode",
        "transit_core.http",
        "transit_core.onetomany",
        "transit_core.plan",
        "transit_core.rendering",
        "transit_core.timeparse",
        "transit_core.timezones",
    }
    package = os.path.join(REPO, "transit_core") + os.sep
    for module, origin in session.log["origins"].items():
        assert origin.startswith(package), f"{module} resolved outside the checkout: {origin}"
    assert session.log["socket_attempts"] == []
    assert session.log["urls"] == []


# ── the surface: 13 names, version, era shape, new schemas ──────────


def test_tools_list_keeps_the_thirteen_names_version_and_era_shape(tmp_path):
    with Session(tmp_path, [], "surface") as session:
        response = session.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    result = response["result"]
    assert_era(result)
    tools = result["tools"]
    assert tuple(tool["name"] for tool in tools) == TOOL_NAMES
    assert len(tools) == 13
    schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
    for name, schema in schemas.items():
        assert schema["type"] == "object", name
        assert "outputSchema" not in tool_by_name(tools, name)

    plan = schemas["transit_plan"]["properties"]
    for argument in (
        "from_stop_id",
        "to_stop_id",
        "from_lat",
        "to_lat",
        "time",
        "timezone",
        "origin_timezone",
        "destination_timezone",
        "max_transfers",
        "max_itineraries",
        "max_results",
        "search_window_seconds",
        "max_access_walk_minutes",
        "max_egress_walk_minutes",
        "min_transfer_minutes",
        "continuation",
        "include_direct",
    ):
        assert argument in plan, argument
    assert "max_walking_minutes" not in plan  # the breaking rename, not an alias
    assert plan["continuation"]["properties"] == {"cursor": {}, "query": {}} or (
        set(plan["continuation"]["properties"]) == {"cursor", "query"}
    )
    assert schemas["transit_plan"]["required"] == [] or not any(
        key.startswith(("from_", "to_")) for key in schemas["transit_plan"].get("required", [])
    )
    assert "stop_id" in schemas["transit_departures"]["properties"]
    assert "stop" not in schemas["transit_departures"]["properties"]
    assert schemas["transit_departures"]["required"] == ["stop_id"]
    assert "timezone" in schemas["transit_departures"]["properties"]
    assert "window" in schemas["transit_departures"]["properties"]
    assert "timezone" in schemas["transit_map_trips"]["properties"]
    assert "types" in schemas["transit_geocode"]["properties"]
    assert "near" in schemas["transit_geocode"]["properties"]
    assert "near_bias" in schemas["transit_geocode"]["properties"]
    assert "num_results" in schemas["transit_geocode"]["properties"]
    assert "types" in schemas["transit_reverse_geocode"]["properties"]
    assert "num_results" in schemas["transit_reverse_geocode"]["properties"]
    assert "timezone" in schemas["transit_reachable"]["properties"]
    assert "endpoint_timezone" in schemas["transit_reachable"]["properties"]
    assert "max_access_walk_minutes" in schemas["transit_one_to_many_transit"]["properties"]
    assert "endpoint_timezone" in schemas["transit_one_to_many_transit"]["properties"]


def test_discover_carries_the_era_fields_and_server_info(tmp_path):
    with Session(tmp_path, [], "discover") as session:
        response = session.rpc({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
    result = response["result"]
    assert result["supportedVersions"] == [era]
    assert result["capabilities"] == {"tools": {}}
    assert_era(result)
    assert isinstance(result["_meta"]["io.modelcontextprotocol/serverInfo"]["version"], str)


def test_missing_or_non_string_params_is_a_protocol_fault(tmp_path):
    with Session(tmp_path, [], "protocol_faults") as session:
        missing = session.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
        non_string = session.rpc(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": 42}}
        )
        legacy = session.rpc({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}})
        alive = session.rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    for response in (missing, non_string):
        assert response["error"]["code"] == -32602
        assert "result" not in response
    assert legacy["error"]["code"] == -32601
    assert len(alive["result"]["tools"]) == 13


def tool_by_name(tools, name):
    for tool in tools:
        if tool["name"] == name:
            return tool
    raise AssertionError(f"no such tool: {name}")


def test_descriptions_state_the_new_contracts(tmp_path):
    with Session(tmp_path, [], "descriptions") as session:
        tools = session.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    for name, tokens in DESCRIPTION_TOKENS.items():
        description = by_name[name]["description"]
        for token in tokens:
            assert token in description, f"{name} description lacks {token!r}"
    # The raw three are the only tools documented as raw, so say it there and
    # nowhere else.
    for name in ("transit_refresh", "transit_rentals", "transit_health"):
        assert "not the canonical envelope" in by_name[name]["description"], name
    for name in TOOL_NAMES:
        if name in ("transit_refresh", "transit_rentals", "transit_health"):
            continue
        assert "raw provider payload" not in by_name[name]["description"], name


# ── a successful plan: one canonical envelope, one real URL ─────────


def test_plan_success_is_one_canonical_envelope_and_one_real_url(tmp_path):
    body = fixture_body("synthetic_plan_ayr_ncl_stopids_n3.json")
    with Session(tmp_path, [route("/api/v6/plan", body)], "plan") as session:
        response = session.call(
            "transit_plan",
            {
                "from_stop_id": "gb-great-britain_910GAYRR",
                "to_stop_id": "gb-great-britain_910GNWCSTLE",
                "time": "2026-10-27T09:00:00Z",
                "num_itineraries": 3,
                "transit_modes": ["RAIL"],
            },
        )
        payload = tool_payload(response)

    assert payload["outcome"] == "ok"
    assert "result" not in payload and "error" not in payload and "transport_error" not in payload
    assert payload["time_frame"]["form"] == "instant"
    assert payload["time_frame"]["control"] == "depart_after"
    assert payload["time_frame"]["frame_source"] == "not_applicable"
    assert len(payload["itineraries"]) == 3
    fastest = min(payload["itineraries"], key=lambda item: item["elapsed_seconds"])
    assert fastest["elapsed_seconds"] == 12540
    assert fastest["changes"] == 2
    assert fastest["access_seconds"] == 0 and fastest["egress_seconds"] == 0
    assert fastest["station_to_station_seconds"] == 12540
    assert fastest["transit_leg_count"] == 3
    assert fastest["departure"]["zone"] == "Europe/London"
    assert "iso" in fastest["departure"] and "display" in fastest["departure"]
    # one canonical timestamp shape: no second ISO member, no bare clock field
    assert set(fastest["departure"]) == {
        "iso",
        "epoch_seconds",
        "local",
        "local_date",
        "utc_offset_seconds",
        "zone",
        "zone_source",
        "display",
    }
    assert payload["query"]["control"] == "depart_after"
    assert payload["query"]["upstream_count"] == 3
    assert payload["query"]["returned_count"] == 3
    assert payload["query"]["minimum_requested"] == 3
    assert payload["query"]["max_results"] is None
    assert payload["query"]["truncated"] is False
    assert payload["query"]["truncation_reason"] == "none"
    assert payload["query"]["requested_modes"] == ["RAIL"]
    assert payload["query"]["applied_modes"] == "RAIL"
    assert payload["query"]["search_window_seconds"] == 900
    assert payload["continuation"]["previous"]["cursor"] == "EARLIER|1793091600"
    assert payload["continuation"]["next"]["cursor"] == "LATER|1793095560"
    for direction in ("previous", "next"):
        frozen = payload["continuation"][direction]["query"]
        assert frozen["from"] == {"form": "stop_id", "value": "gb-great-britain_910GAYRR"}
        assert frozen["to"] == {"form": "stop_id", "value": "gb-great-britain_910GNWCSTLE"}
        assert frozen["time"] == "2026-10-27T09:00:00Z"
        assert frozen["transit_modes"] == ["RAIL"]
    # RAIL is narrower than the default TRANSIT, so it is an active constraint -
    # echoed as supplied, never offered as the cause of an empty result.
    assert payload["constraints_active"] == [{"name": "transit_modes", "value": ["RAIL"]}]
    assert payload["warnings"] == [
        {
            "code": "realtime_unavailable",
            "message": "no returned leg carries realtime data; times are scheduled, not live",
            "scope": "query",
        }
    ]
    assert payload["from"]["stop_id"] == "gb-great-britain_910GAYRR"

    assert_no_unexpected(session)
    assert len(session.log["urls"]) == 1
    url = session.log["urls"][0]
    assert urlsplit(url).path == "/api/v6/plan"
    assert one_value(url, "fromPlace") == "gb-great-britain_910GAYRR"
    assert one_value(url, "toPlace") == "gb-great-britain_910GNWCSTLE"
    assert one_value(url, "time") == "2026-10-27T09:00:00Z"
    assert one_value(url, "arriveBy") == "false"
    assert one_value(url, "numItineraries") == "3"
    assert one_value(url, "transitModes") == "RAIL"
    assert one_value(url, "searchWindow") == "900"
    assert "pageCursor" not in query_of(url)


def test_plan_max_results_truncation_is_reported_not_inferred(tmp_path):
    body = fixture_body("synthetic_plan_ayr_ncl_stopids_n3.json")
    with Session(tmp_path, [route("/api/v6/plan", body)], "truncation") as session:
        payload = tool_payload(
            session.call(
                "transit_plan",
                {
                    "from_stop_id": "gb-great-britain_910GAYRR",
                    "to_stop_id": "gb-great-britain_910GNWCSTLE",
                    "time": "2026-10-27T09:00:00Z",
                    "max_results": 1,
                },
            )
        )
    assert len(payload["itineraries"]) == 1
    assert payload["query"]["upstream_count"] == 3
    assert payload["query"]["returned_count"] == 1
    assert payload["query"]["truncated"] is True
    assert payload["query"]["truncation_reason"] == "max_results"
    assert payload["query"]["dropped_count"] == 2
    assert_one_request(session, "/api/v6/plan")


def assert_one_request(session, path):
    assert_no_unexpected(session)
    assert len(session.log["urls"]) == 1
    assert urlsplit(session.log["urls"][0]).path == path


# ── geocode, boards and the remaining mapped tools through the wire ─


def test_geocode_success_identity_filters_and_bounded_count(tmp_path):
    body = fixture_body("synthetic_geocode_ayr_station.json")
    with Session(tmp_path, [route("/api/v1/geocode", body)], "geocode") as session:
        payload = tool_payload(
            session.call(
                "transit_geocode",
                {
                    "query": "Ayr station",
                    "types": ["STOP", "ADDRESS"],
                    "num_results": 3,
                },
            )
        )
    assert payload["outcome"] == "ok"
    # count is the length of what the upstream returned; the cap is a request
    # parameter (numResults), not a client-side slice.
    assert payload["count"] == len(payload["results"]) == len(body)
    assert payload["effective_query"]["types"] == ["STOP", "ADDRESS"]
    assert payload["effective_query"]["num_results"] == 3
    first = payload["results"][0]
    assert first["match_type"] == "STOP"
    assert first["stop_id"] == first["match_id"] == "gb-great-britain_6190123"
    assert first["timezone"] == "Europe/London"
    assert "parent_id" not in first
    url = session.log["urls"][0]
    assert urlsplit(url).path == "/api/v1/geocode"
    assert one_value(url, "text") == "Ayr station"
    assert one_value(url, "type") == "STOP,ADDRESS"  # one comma value, caller order
    assert one_value(url, "numResults") == "3"


def test_departures_success_keeps_scheduled_and_realtime_apart(tmp_path):
    body = fixture_body("synthetic_stoptimes_ayr_p3.json")
    with Session(tmp_path, [route("/api/v6/stoptimes", body)], "departures") as session:
        payload = tool_payload(
            session.call(
                "transit_departures",
                {
                    "stop_id": "gb-great-britain_9100AYRR3",
                    "count": 3,
                    "time": "2026-10-27T09:00:00Z",
                },
            )
        )
    assert payload["outcome"] == "ok"
    assert payload["query"]["stop_id"] == "gb-great-britain_9100AYRR3"
    assert payload["query"]["count"] == 3
    assert payload["returned_count"] == 3
    assert payload["stop"]["stop_id"] == "gb-great-britain_9100AYRR3"
    assert payload["stop"]["parent_id"] == "gb-great-britain_910GAYRR"
    for departure in payload["departures"]:
        assert departure["realtime"] is False
        assert departure["expected_departure"] is None
        assert departure["scheduled"] is None
        assert departure["scheduled_departure"]["zone"] == "Europe/London"
        assert departure["delay_seconds"] is None
        assert set(departure) >= {"trip_cancelled", "stop_cancelled"}
    url = session.log["urls"][0]
    assert urlsplit(url).path == "/api/v6/stoptimes"
    assert one_value(url, "stopId") == "gb-great-britain_9100AYRR3"
    assert one_value(url, "n") == "3"
    assert one_value(url, "time") == "2026-10-27T09:00:00Z"


def test_trip_success_reports_intermediate_stops(tmp_path):
    body = fixture_body("synthetic_trip_intermediate_stops.json")
    with Session(tmp_path, [route("/api/v6/trip", body)], "trip") as session:
        payload = tool_payload(session.call("transit_trip", {"trip_id": "synthetic-trip-1"}))
    assert payload["outcome"] == "ok"
    assert payload["trip_id"] == "synthetic-trip-1"
    assert len(payload["legs"]) == 1
    leg = payload["legs"][0]
    assert leg["index"] == 0
    assert leg["intermediate_stop_count"] == 3
    assert len(leg["intermediate_stops"]) == 3
    for stop in leg["intermediate_stops"]:
        assert set(stop) >= {"stop_id", "name", "arrival", "departure", "stop_cancelled"}
    url = session.log["urls"][0]
    assert urlsplit(url).path == "/api/v6/trip"
    assert one_value(url, "tripId") == "synthetic-trip-1"


def test_map_trips_success_has_no_position_field(tmp_path):
    body = fixture_body("synthetic_map_trips_window.json")
    with Session(tmp_path, [route("/api/v6/map/trips", body)], "map_trips") as session:
        payload = tool_payload(
            session.call(
                "transit_map_trips",
                {
                    "min_lat": 55.4,
                    "min_lon": -4.7,
                    "max_lat": 55.9,
                    "max_lon": -4.2,
                    "start_time": "2026-10-27T09:00:00Z",
                    "end_time": "2026-10-27T09:30:00Z",
                    "zoom": 12,
                },
            )
        )
    assert payload["outcome"] == "ok"
    assert payload["returned_count"] == 2
    for trip in payload["trips"]:
        assert set(trip) >= {
            "trip_id",
            "route",
            "mode",
            "departure",
            "arrival",
            "scheduled_departure",
            "from_lat",
            "distance_meters",
            "polyline_present",
            "realtime",
        }
        assert "vehicle_position" not in trip
        assert isinstance(trip["realtime"], bool)
    url = session.log["urls"][0]
    assert urlsplit(url).path == "/api/v6/map/trips"
    assert one_value(url, "min") == "55.4,-4.7"
    assert one_value(url, "max") == "55.9,-4.2"
    assert one_value(url, "zoom") == "12"
    assert one_value(url, "startTime") == "2026-10-27T09:00:00Z"
    assert one_value(url, "endTime") == "2026-10-27T09:30:00Z"


def test_side_tools_return_the_canonical_envelope(tmp_path):
    routes = [
        route("/api/v6/one-to-all", fixture_body("synthetic_reachable_pareto.json")),
        route("/api/v1/one-to-many", fixture_body("synthetic_one_to_many_street.json")),
        route(
            "/api/experimental/one-to-many-intermodal",
            fixture_body("synthetic_one_to_many_transit.json")["valid"],
        ),
    ]
    destinations = [[55.858315, -4.258436], [55.9527965, -3.188463], [56.0, -3.5]]
    with Session(tmp_path, routes, "side_tools") as session:
        reachable = tool_payload(
            session.call(
                "transit_reachable",
                {"lat": 55.458267, "lon": -4.625827, "max_time_min": 60, "time": "2026-10-27T09:00:00Z"},
                rid=2,
            )
        )
        street = tool_payload(
            session.call(
                "transit_one_to_many",
                {
                    "from_lat": 55.458267,
                    "from_lon": -4.625827,
                    "destinations": destinations,
                    "mode": "WALK",
                },
                rid=3,
            )
        )
        intermodal = tool_payload(
            session.call(
                "transit_one_to_many_transit",
                {
                    "from_lat": 55.458267,
                    "from_lon": -4.625827,
                    "destinations": destinations,
                    "time": "2026-10-27T09:00:00Z",
                    "transit_modes": ["BUS", "RAIL"],
                },
                rid=4,
            )
        )

    assert reachable["outcome"] == "ok"
    assert reachable["query"]["one_role"] == "origin"
    assert reachable["query"]["max_time_min"] == 60
    entries = reachable["entries"]
    assert entries and entries[0]["travel_time_seconds"] == 3060
    assert entries[0]["transfers_k"] == 1
    assert "travelTime_min" not in entries[0]
    assert "duration_minutes" not in entries[0]

    assert street["outcome"] == "ok"
    assert street["query"]["one_role"] == "origin"
    assert street["query"]["destination_count"] == 3
    assert [row["duration_seconds"] for row in street["results"]] == [423.75, 0, None]
    assert [row["distance_meters"] for row in street["results"]] == [531.2, 0.0, None]

    assert intermodal["outcome"] == "ok"
    assert intermodal["query"]["one_role"] == "origin"
    assert intermodal["query"]["applied_modes"] == ["BUS", "RAIL"]
    assert [row["street_duration_seconds"] for row in intermodal["results"]] == [300, None, 120.5]
    assert intermodal["results"][0]["transit"] == [
        {"duration_seconds": 1800, "transfers": 0},
        {"duration_seconds": 1500, "transfers": 1},
    ]
    assert intermodal["results"][1]["transit"] == []

    assert_no_unexpected(session)
    paths = [urlsplit(url).path for url in session.log["urls"]]
    assert paths == [
        "/api/v6/one-to-all",
        "/api/v1/one-to-many",
        "/api/experimental/one-to-many-intermodal",
    ]
    many = one_value(session.log["urls"][1], "many")
    assert many == "55.858315;-4.258436,55.9527965;-3.188463,56.0;-3.5"
    modes = one_value(session.log["urls"][2], "transitModes")
    assert modes == "BUS,RAIL"


# ── the published street-mode default and its explicit values ───────
#
# §4.12.2 publishes `mode` as optional with default WALK. An omitted key must
# stay omitted so the accepted core applies that one default; a key that is
# present must be forwarded exactly, including the values the core rejects.

STREET_DESTINATIONS = [[55.858315, -4.258436], [55.9527965, -3.188463], [56.0, -3.5]]
_ABSENT = object()


def _street_arguments(mode):
    arguments = {
        "from_lat": 55.458267,
        "from_lon": -4.625827,
        "destinations": STREET_DESTINATIONS,
    }
    if mode is not _ABSENT:
        arguments["mode"] = mode
    return arguments


@pytest.mark.parametrize(
    "case, supplied, expected",
    [
        ("omitted", _ABSENT, "WALK"),
        ("walk", "WALK", "WALK"),
        ("bike", "BIKE", "BIKE"),
        ("car", "CAR", "CAR"),
    ],
)
def test_one_to_many_mode_defaults_to_walk_and_echoes_each_explicit_profile(
    tmp_path, case, supplied, expected
):
    routes = [route("/api/v1/one-to-many", fixture_body("synthetic_one_to_many_street.json"))]
    with Session(tmp_path, routes, f"street_mode_{case}") as session:
        response = session.call("transit_one_to_many", _street_arguments(supplied), rid=2)

    # Name the contract on the failing line: a rejected omission shows the
    # canonical payload the core produced, not a transport or import fault.
    assert response["result"].get("isError") is not True, response["result"]["content"][0]["text"]
    payload = tool_payload(response)
    assert payload["outcome"] == "ok"
    assert payload["query"]["mode"] == expected
    assert payload["query"]["max_sec"] == 3600
    assert payload["query"]["arrive_by"] is False
    assert [row["duration_seconds"] for row in payload["results"]] == [423.75, 0, None]

    assert_no_unexpected(session)
    assert len(session.log["urls"]) == 1
    url = session.log["urls"][0]
    assert urlsplit(url).path == "/api/v1/one-to-many"
    assert one_value(url, "mode") == expected
    assert one_value(url, "max") == "3600"
    assert one_value(url, "arriveBy") == "false"
    assert one_value(url, "maxMatchingDistance") == "25"


@pytest.mark.parametrize(
    "case, supplied",
    [
        ("null", None),
        ("false", False),
        ("empty", ""),
        ("lowercase", "walk"),
        ("padded", "WALK "),
        ("transit", "TRANSIT"),
    ],
)
def test_one_to_many_present_invalid_mode_is_rejected_before_any_request(
    tmp_path, case, supplied
):
    with Session(tmp_path, [], f"street_mode_bad_{case}") as session:
        response = session.call("transit_one_to_many", _street_arguments(supplied), rid=2)

    payload = tool_error(response)
    assert payload["outcome"] == "invalid_input"
    assert payload["error_type"] == "unknown_mode"
    assert payload["error_details"]["argument"] == "mode"
    assert session.log["urls"] == []
    assert_no_unexpected(session)


# ── continuation: one frozen query, both directions ─────────────────


def _page(name):
    return fixture_body("synthetic_continuation_pages.json")[name]


def _shift_itinerary(itinerary, seconds):
    payload = json.loads(json.dumps(itinerary))

    def bump(value):
        moment = datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(seconds=seconds)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    payload["startTime"] = bump(payload["startTime"])
    payload["endTime"] = bump(payload["endTime"])
    for leg in payload["legs"]:
        for key in ("startTime", "endTime", "scheduledStartTime", "scheduledEndTime"):
            if key in leg:
                leg[key] = bump(leg[key])
    return payload


def _page_two_with_three_itineraries():
    """page2 plus a shifted itinerary: adjacent pages then differ in count.

    Labelled synthetic and built in memory by this test; the fixtures give
    both pages two itineraries and the point is that the page reports its own
    count.
    """
    page = json.loads(json.dumps(_page("page2")))
    extra = _shift_itinerary(page["itineraries"][0], 3600)
    extra["id"] = "synthetic-return-itinerary-3"
    page["itineraries"].append(extra)
    return page


def _page_with_one_itinerary():
    """page2 cut to its first itinerary: the earlier page differs in count too.

    Labelled synthetic and built in memory by this test, like the page above: the
    retained pages both carry two itineraries, and the point is that each replayed
    direction reports the count of the page it actually received.
    """
    page = json.loads(json.dumps(_page("page2")))
    page["itineraries"] = page["itineraries"][:1]
    return page


def test_continuation_replay_preserves_the_frozen_query_and_page(tmp_path):
    routes = [
        route("pageCursor=EARLIER", _page_with_one_itinerary()),
        route("pageCursor=LATER", _page_two_with_three_itineraries()),
        route("/api/v6/plan", _page("page1")),
    ]
    arguments = {
        "from_lat": 55.858315,
        "from_lon": -4.258436,
        "to_lat": 55.458267,
        "to_lon": -4.625827,
        "num_itineraries": 2,
        "transit_modes": ["RAIL"],
    }
    with Session(tmp_path, routes, "continuation") as session:
        first = tool_payload(session.call("transit_plan", arguments, rid=2))
        assert first["time_frame"]["form"] == "omitted"
        previous = first["continuation"]["previous"]
        following = first["continuation"]["next"]
        assert previous is not None and following is not None
        assert previous["cursor"] != following["cursor"]
        # each direction is replayed on its own, through the same real loop
        earlier = tool_payload(
            session.call("transit_plan", {"continuation": previous}, rid=3)
        )
        later = tool_payload(
            session.call("transit_plan", {"continuation": following}, rid=4)
        )
        # a restated identical argument is accepted
        restated = tool_payload(
            session.call(
                "transit_plan",
                {"continuation": following, "num_itineraries": 2},
                rid=5,
            )
        )
        assert earlier["outcome"] == "ok"
        assert later["outcome"] == "ok"
        assert restated["outcome"] == "ok"

    assert_no_unexpected(session)
    assert len(session.log["urls"]) == 4
    first_url, previous_url, next_url, restated_url = session.log["urls"]
    frozen_time = one_value(first_url, "time")
    assert frozen_time.endswith("Z")

    # Both directions carry one frozen query: each sends its own cursor, the frozen
    # instant, window, caps and modes, and both frozen endpoint values in the form
    # the first call used (§4.6.1).
    for frozen, url in (
        (previous, previous_url),
        (following, next_url),
        (following, restated_url),
    ):
        query = frozen["query"]
        assert query["time"] == frozen_time
        assert query["from"] == {"form": "coordinate", "value": "55.858315,-4.258436"}
        assert query["to"] == {"form": "coordinate", "value": "55.458267,-4.625827"}
        assert one_value(url, "pageCursor") == frozen["cursor"]
        assert one_value(url, "fromPlace") == query["from"]["value"]
        assert one_value(url, "toPlace") == query["to"]["value"]
        assert one_value(url, "time") == query["time"]
        assert one_value(url, "searchWindow") == str(query["search_window_seconds"])
        assert one_value(url, "numItineraries") == str(query["num_itineraries"])
        assert one_value(url, "maxTransfers") == str(query["max_transfers"])
        assert one_value(url, "transitModes") == ",".join(query["transit_modes"])
        assert one_value(url, "maxPreTransitTime") == str(query["max_access_walk_minutes"] * 60)
        assert one_value(url, "maxPostTransitTime") == str(query["max_egress_walk_minutes"] * 60)
        assert one_value(url, "minTransferTime") == str(query["min_transfer_minutes"] * 60)
    # the earlier cursor is the earlier direction on the wire, not a replay of next
    assert previous_url != next_url
    assert one_value(previous_url, "pageCursor") == "EARLIER|1793170800"
    assert one_value(next_url, "pageCursor") == "LATER|1793098920"
    assert one_value(previous_url, "fromPlace") == "55.858315,-4.258436"
    assert one_value(previous_url, "toPlace") == "55.458267,-4.625827"
    # each page reports its own counts, not the first page's
    assert first["query"]["upstream_count"] == 2
    assert earlier["query"]["upstream_count"] == 1
    assert earlier["query"]["returned_count"] == 1
    assert earlier["itineraries"][0]["itinerary_id"] == "synthetic-return-itinerary-2"
    assert later["query"]["upstream_count"] == 3
    assert later["query"]["returned_count"] == 3
    assert later["itineraries"][-1]["itinerary_id"] == "synthetic-return-itinerary-3"
    assert restated["query"]["upstream_count"] == 3
    assert restated["query"]["returned_count"] == 3


def test_continuation_contradictions_are_typed_and_issue_no_request(tmp_path):
    routes = [
        route("pageCursor=", _page_two_with_three_itineraries()),
        route("/api/v6/plan", _page("page1")),
    ]
    with Session(tmp_path, routes, "continuation_mismatch") as session:
        first = tool_payload(
            session.call(
                "transit_plan",
                {
                    "from_lat": 55.858315,
                    "from_lon": -4.258436,
                    "to_lat": 55.458267,
                    "to_lon": -4.625827,
                    "num_itineraries": 2,
                    "transit_modes": ["RAIL"],
                },
                rid=2,
            )
        )
        frozen = first["continuation"]["next"]
        time_error = tool_error(
            session.call(
                "transit_plan",
                {"continuation": frozen, "time": "2026-10-28T07:00:00Z"},
                rid=3,
            )
        )
        transfers_error = tool_error(
            session.call(
                "transit_plan",
                {"continuation": frozen, "max_transfers": 1},
                rid=4,
            )
        )
        cursor_error = tool_error(
            session.call(
                "transit_plan",
                {"continuation": {"cursor": frozen["cursor"], "query": {"from": "x"}}},
                rid=5,
            )
        )

    assert time_error["error_type"] == "cursor_without_identical_query"
    assert time_error["outcome"] == "invalid_input"
    assert time_error["error_details"]["fields"] == ["time"]
    assert transfers_error["error_details"]["fields"] == ["max_transfers"]
    assert cursor_error["error_type"] == "value_out_of_range"
    # one request only: the successful first page. Both contradictions and the
    # malformed cursor are rejected before any I/O.
    assert len(session.log["urls"]) == 1
    assert_no_unexpected(session)


# ── typed errors through the wire ───────────────────────────────────


def test_dst_and_malformed_time_are_typed_and_spend_no_request(tmp_path):
    with Session(tmp_path, [], "time_errors") as session:
        ambiguous = tool_error(
            session.call(
                "transit_plan",
                {
                    "from_stop_id": "gb-great-britain_910GAYRR",
                    "to_stop_id": "gb-great-britain_910GNWCSTLE",
                    "time": "2026-10-25T01:30",
                    "timezone": "Europe/London",
                },
                rid=2,
            )
        )
        nonexistent = tool_error(
            session.call(
                "transit_plan",
                {
                    "from_stop_id": "gb-great-britain_910GAYRR",
                    "to_stop_id": "gb-great-britain_910GNWCSTLE",
                    "time": "2026-03-29T01:30",
                    "timezone": "Europe/London",
                },
                rid=3,
            )
        )
        malformed = tool_error(
            session.call(
                "transit_plan",
                {
                    "from_lat": 55.4,
                    "from_lon": -4.6,
                    "to_lat": 54.9,
                    "to_lon": -1.6,
                    "time": "2026-02-30T09:00:00Z",
                },
                rid=4,
            )
        )

    assert ambiguous["error_type"] == "ambiguous_local_time"
    assert ambiguous["outcome"] == "invalid_input"
    assert {pair["utc_offset_seconds"] for pair in ambiguous["error_details"]["candidates"]} == {0, 3600}
    assert "explicit offset" in ambiguous["error_details"]["remedy"]
    assert nonexistent["error_type"] == "nonexistent_local_time"
    assert nonexistent["error_details"]["gap"] == {
        "start": "2026-03-29T01:00:00",
        "end": "2026-03-29T02:00:00",
    }
    assert malformed["error_type"] == "malformed_time"
    assert session.log["urls"] == []
    assert_no_unexpected(session)


def test_upstream_status_and_window_failures_are_typed_results(tmp_path):
    with Session(
        tmp_path, [route("/api/v6/plan", {"error": "slow down"}, status=429)], "rate_limited"
    ) as session:
        limited = tool_error(
            session.call(
                "transit_plan",
                {
                    "from_lat": 55.4,
                    "from_lon": -4.6,
                    "to_lat": 54.9,
                    "to_lon": -1.6,
                    "time": "2026-10-27T09:00:00Z",
                },
            )
        )
    assert limited["outcome"] == "upstream_rejected"
    assert limited["error_type"] == "rate_limited"

    window = fixture_body("synthetic_window_error.json")["entries"][1]
    assert window["status"] == 400
    with Session(
        tmp_path,
        [route("/api/v6/plan", status=400, raw=window["body"])],
        "window",
    ) as session:
        out_of_range = tool_error(
            session.call(
                "transit_plan",
                {
                    "from_lat": 55.4,
                    "from_lon": -4.6,
                    "to_lat": 54.9,
                    "to_lon": -1.6,
                    "time": "2026-07-15T09:00:00Z",
                },
            )
        )
    assert out_of_range["outcome"] == "out_of_range"
    assert out_of_range["error_type"] == "timetable_window"
    assert out_of_range["error_details"]["window"] == {
        "start": "2026-09-20 00:00",
        "end": "2027-10-21 00:00",
    }


def test_malformed_upstream_body_is_a_typed_result(tmp_path):
    # A structurally valid empty body is no_routes; the same body missing a key the
    # no-routes predicate requires is malformed_upstream, never no_routes.
    empty = fixture_body("synthetic_plan_empty_no_service.json")
    body = json.loads(json.dumps(empty))
    body.pop("debugOutput")
    arguments = {
        "from_lat": 55.4,
        "from_lon": -4.6,
        "to_lat": 54.9,
        "to_lon": -1.6,
        "time": "2026-10-27T09:00:00Z",
    }
    with Session(tmp_path, [route("/api/v6/plan", empty)], "no_routes") as session:
        no_routes = tool_payload(session.call("transit_plan", arguments))
    assert no_routes["outcome"] == "no_routes"
    assert no_routes["no_routes_reason"] == "not_determined"
    assert no_routes["itineraries"] == []
    assert no_routes["query"]["previous_page_cursor"] is not None

    with Session(tmp_path, [route("/api/v6/plan", body)], "malformed") as session:
        payload = tool_error(session.call("transit_plan", arguments))
    assert payload["outcome"] == "malformed_upstream"
    assert payload["error_type"] == "unexpected_shape"


def test_unknown_tool_is_a_canonical_error_result(tmp_path):
    with Session(tmp_path, [], "unknown_tool") as session:
        payload = tool_error(session.call("no_such_tool_xyz"))
    assert payload["error_type"] == "value_out_of_range"
    assert payload["outcome"] == "invalid_input"
    assert "Unknown tool" in payload["error"]
    assert payload["error_details"]["argument"] == "name"


def test_removed_arguments_are_rejected_and_name_the_accepted_set(tmp_path):
    # The breaking rename is a closed set, not an alias: an old argument fails
    # loudly before any request instead of silently falling back to a default.
    with Session(tmp_path, [], "legacy_arguments") as session:
        plan_legacy = tool_error(
            session.call(
                "transit_plan",
                {
                    "from_lat": 55.4,
                    "from_lon": -4.6,
                    "to_lat": 54.9,
                    "to_lon": -1.6,
                    "time": "2026-10-27T09:00:00Z",
                    "max_walking_minutes": 30,
                },
                rid=2,
            )
        )
        departures_legacy = tool_error(
            session.call("transit_departures", {"stop": "gb-great-britain_9100AYRR3"}, rid=3)
        )
    assert plan_legacy["error_type"] == "value_out_of_range"
    assert plan_legacy["error_details"]["argument"] == "max_walking_minutes"
    assert "max_access_walk_minutes" in plan_legacy["error_details"]["accepted_arguments"]
    assert "max_egress_walk_minutes" in plan_legacy["error_details"]["accepted_arguments"]
    assert departures_legacy["error_type"] == "value_out_of_range"
    assert departures_legacy["error_details"]["argument"] == "stop_id"
    assert session.log["urls"] == []


def test_unexpected_upstream_url_fails_without_a_live_socket(tmp_path):
    # No route matches, so the fake transport records the URL and raises. The call
    # must come back as a protocol fault (-32603), never as a live request.
    with Session(tmp_path, [], "unexpected") as session:
        response = session.call(
            "transit_health",
        )
    assert response["error"]["code"] == -32603
    assert "result" not in response
    assert session.log["unexpected"] == ["https://api.transitous.org/api/v1/health"]
    assert session.log["socket_attempts"] == []


# ── the raw three ───────────────────────────────────────────────────


def test_raw_payload_tools_keep_their_provider_payloads(tmp_path):
    health_body = {"status": "ok", "synthetic": True}
    rentals_body = [
        {"lat": 55.45, "lon": -4.62, "synthetic": True, "vehicles": [{"type": "BIKE"}]}
    ]
    refresh_body = {"synthetic": True, "itinerary": {"id": "itinerary-1", "legs": []}}
    routes = [
        route("/api/v1/health", health_body),
        route("/api/v1/rentals", rentals_body),
        route("/api/v6/refresh-itinerary", refresh_body),
    ]
    with Session(tmp_path, routes, "raw") as session:
        health = tool_payload(session.call("transit_health", rid=2))
        rentals = tool_payload(
            session.call("transit_rentals", {"lat": 55.45, "lon": -4.62}, rid=3)
        )
        refresh = tool_payload(
            session.call("transit_refresh", {"itinerary_id": "itinerary-1"}, rid=4)
        )
    assert health == health_body
    assert rentals == rentals_body
    assert refresh == refresh_body
    assert no_envelope(health) and no_envelope(rentals) and no_envelope(refresh)
    assert_no_unexpected(session)
    assert [urlsplit(url).path for url in session.log["urls"]] == [
        "/api/v1/health",
        "/api/v1/rentals",
        "/api/v6/refresh-itinerary",
    ]
    assert one_value(session.log["urls"][1], "point") == "55.45,-4.62"
    assert one_value(session.log["urls"][1], "radius") == "1000"
    assert one_value(session.log["urls"][2], "itineraryId") == "itinerary-1"


def test_raw_payload_tools_still_reject_out_of_range_coordinates(tmp_path):
    # The raw payload is the success shape only: argument validation is unchanged
    # and still costs no request.
    with Session(tmp_path, [], "rentals_bounds") as session:
        latitude = tool_error(session.call("transit_rentals", {"lat": 91, "lon": -4.62}, rid=2))
        longitude = tool_error(session.call("transit_rentals", {"lat": 55.45, "lon": 181}, rid=3))
        missing_id = tool_error(session.call("transit_refresh", {}, rid=4))
    for payload, argument, limit in (
        (latitude, "lat", 90.0),
        (longitude, "lon", 180.0),
    ):
        assert payload["outcome"] == "invalid_input"
        assert payload["error_type"] == "value_out_of_range"
        assert payload["error_details"]["argument"] == argument
        assert payload["error_details"]["maximum"] == limit
    assert missing_id["error_type"] == "value_out_of_range"
    assert missing_id["error_details"]["argument"] == "itinerary_id"
    assert session.log["urls"] == []
    assert_no_unexpected(session)


@pytest.mark.parametrize(
    "radius",
    [
        pytest.param("not-a-number", id="string"),
        pytest.param("1000", id="numeric-string"),
        pytest.param(12.5, id="fractional"),
        pytest.param(12.0, id="whole-float"),
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param([1000], id="list"),
        pytest.param({"metres": 1000}, id="object"),
    ],
)
def test_rentals_rejects_a_non_integer_radius_before_any_request(tmp_path, radius):
    # The MCP schema already says integer; the adapter must not forward a value
    # the endpoint cannot mean. A rejected radius costs no request at all.
    with Session(tmp_path, [], "rentals_radius") as session:
        payload = tool_error(
            session.call(
                "transit_rentals",
                {"lat": 55.45, "lon": -4.62, "radius": radius},
                rid=2,
            )
        )
    assert payload["outcome"] == "invalid_input"
    assert payload["error_type"] == "value_out_of_range"
    assert payload["error_details"]["argument"] == "radius"
    assert session.log["urls"] == []
    assert_no_unexpected(session)


def test_rentals_forwards_an_integer_beyond_the_nearby_stops_window(tmp_path):
    # transit_rentals publishes no 1..5000 bound (that window belongs only to
    # transit_nearby_stops), so a valid integer is sent verbatim rather than
    # clamped or rejected by an invented limit.
    body = [{"synthetic": True, "lat": 55.45, "lon": -4.62}]
    with Session(tmp_path, [route("/api/v1/rentals", body)], "rentals_wide") as session:
        payload = tool_payload(
            session.call(
                "transit_rentals",
                {"lat": 55.45, "lon": -4.62, "radius": 40000},
                rid=2,
            )
        )
    assert payload == body
    assert one_value(session.log["urls"][0], "radius") == "40000"
    assert_no_unexpected(session)


def no_envelope(payload):
    if not isinstance(payload, dict):
        return True  # a raw array payload cannot be the canonical envelope
    return not {"outcome", "error", "result", "transport_error"} & set(payload)
