"""The protocol-independent application boundary and its import-time contract.

``transit_core.backend`` is the one operation entry point shared by the stateless
MCP adapter and any future native CLI. These tests exercise it directly: the
ordered 13-operation inventory, the argument mappings, the single ``ToolError``
boundary, the extracted health/refresh/rentals request assembly and the one
shared request-mode vocabulary. They also prove, in a child interpreter, that
importing the backend or ``server`` neither touches a stream, opens a socket nor
starts the stdio loop.

Bodies built here are inline and labelled ``synthetic``; the transport is a
deterministic in-process fake and no socket is opened.
"""

import io
import json
import os
import subprocess
import inspect
import urllib.parse
import urllib.request

import fixture_support
import pytest

import server
import transit_core
from transit_core import backend, errors, geocode, http, modes, onetomany, plan

REPO = fixture_support.REPO_ROOT
PROD_PY = "/usr/bin/python3"


# ── deterministic transport ─────────────────────────────────────────


class StubResponse:
    """A response shaped like what ``http.api_get`` reads."""

    def __init__(self, payload, status=200):
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def getcode(self):
        return self.status

    def close(self):
        pass


class RecordingTransport:
    """Record every assembled URL and answer it with one synthetic body."""

    def __init__(self, payload=None, status=200):
        self.payload = {} if payload is None else payload
        self.status = status
        self.urls = []

    def __call__(self, request, timeout):
        self.urls.append(request.full_url)
        return StubResponse(self.payload, self.status)


def query_of(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)


def path_of(url):
    return urllib.parse.urlsplit(url).path


# ── one ordered inventory, translated by the adapter ────────────────


def test_operation_inventory_is_the_thirteen_tools_in_order():
    tool_names = [tool["name"] for tool in server.TOOLS]
    assert tool_names == ["transit_" + operation for operation in backend.OPERATIONS]
    assert len(backend.OPERATIONS) == 13
    assert len(set(backend.OPERATIONS)) == 13


@pytest.mark.parametrize("tool_name", [tool["name"] for tool in server.TOOLS])
def test_every_tool_translates_and_forwards_all_supplied_arguments(tool_name, monkeypatch):
    operation = tool_name[len("transit_"):]
    owner = {
        "plan": backend.plan,
        "geocode": backend.geocode,
        "departures": backend.boards,
        "nearby_stops": backend.geocode,
        "reverse_geocode": backend.geocode,
        "trip": backend.boards,
        "health": backend,
        "reachable": backend.onetomany,
        "refresh": backend,
        "rentals": backend,
        "one_to_many": backend.onetomany,
        "one_to_many_transit": backend.onetomany,
        "map_trips": backend.boards,
    }[operation]
    signature = inspect.signature(getattr(owner, operation))
    tool = next(tool for tool in server.TOOLS if tool["name"] == tool_name)
    # Opaque sentinels test the mapping, not domain validation: every schema field
    # must arrive unchanged at the real core callable's argument boundary.
    arguments = {name: object() for name in tool["inputSchema"]["properties"]}
    body = {"synthetic": True, "operation": operation}
    received = []

    def record(*args, **kwargs):
        received.append(signature.bind(*args, **kwargs).arguments)
        return body

    monkeypatch.setattr(owner, operation, record)
    transport = object()
    variants = [arguments]
    if operation == "one_to_many":
        variants += [
            {key: value for key, value in arguments.items() if key != "mode"},
            dict(arguments, mode=None),
        ]
    for supplied in variants:
        for via_adapter in (True, False):
            received.clear()
            if via_adapter:
                result = server.handle_call(tool_name, supplied)
            else:
                result = backend.run(operation, supplied, transport=transport)
            assert result is body
            assert len(received) == 1
            expected_transport = None if via_adapter else transport
            if operation == "plan":
                assert received[0] == {"args": supplied, "transport": expected_transport}
                assert received[0]["args"] is supplied
            else:
                assert received[0] == dict(supplied, transport=expected_transport)


def test_an_unknown_operation_is_a_canonical_error_naming_the_inventory():
    result = backend.run("no_such_operation", {})
    assert set(result) == {"error", "outcome", "error_type", "error_details"}
    assert result["error_type"] == errors.VALUE_OUT_OF_RANGE
    assert result["outcome"] == errors.INVALID_INPUT
    assert result["error_details"]["argument"] == "name"
    assert result["error_details"]["accepted"] == list(backend.OPERATIONS)
    # An unhashable name is the same closed-set fault, not a TypeError.
    assert backend.run(["plan"], {})["error_details"]["argument"] == "name"


def test_non_mapping_arguments_are_rejected_by_the_shared_boundary():
    result = backend.run("plan", ["not", "a", "mapping"])
    assert result == backend.arguments_error(["not", "a", "mapping"])
    assert result["error_type"] == errors.VALUE_OUT_OF_RANGE
    assert result["error_details"]["expected"] == "object"


def test_the_adapter_keeps_its_own_argument_and_unknown_name_diagnostics():
    # Translation happens after the argument-shape check, exactly as before, so a
    # non-object argument bag outranks an unknown tool name; the unknown-name
    # diagnostic still lists the MCP tool names, not the canonical operations.
    arguments_error = server.handle_call("transit_plan", 5)
    assert arguments_error["error"] == "tool arguments must be an object"
    assert arguments_error["error_details"]["expected"] == "object"
    unknown = server.handle_call("no_such_tool", {})
    assert unknown["error"] == "Unknown tool: no_such_tool"
    assert unknown["error_details"]["accepted"] == [tool["name"] for tool in server.TOOLS]
    assert server.handle_call("no_such_tool", 5) == arguments_error


def test_only_classified_tool_errors_become_error_results(monkeypatch):
    # A classified core failure is converted at the boundary ...
    result = backend.run("refresh", {})
    assert result["error_type"] == errors.VALUE_OUT_OF_RANGE
    assert result["outcome"] == errors.INVALID_INPUT

    # ... while an unexpected programming failure is not re-labelled as a user or
    # upstream fault. It propagates unchanged for the caller to fix.
    def boom(*args, **kwargs):
        raise RuntimeError("programming fault, not a tool error")

    monkeypatch.setattr(backend.geocode, "geocode", boom)
    with pytest.raises(RuntimeError, match="programming fault"):
        backend.run("geocode", {"query": "synthetic place"})


# ── the extracted raw endpoints ─────────────────────────────────────


def test_health_is_one_unchanged_get():
    body = {"status": "ok", "synthetic": True}
    transport = RecordingTransport(body)
    assert backend.health(transport=transport) == body
    assert backend.run("health", {}, transport=transport) == body
    assert [path_of(url) for url in transport.urls] == ["/api/v1/health"] * 2
    assert [urllib.parse.urlsplit(url).query for url in transport.urls] == ["", ""]


def test_refresh_returns_the_raw_body_and_keeps_the_opaque_id():
    body = {"synthetic": True, "itinerary": {"id": "itinerary-1", "legs": []}}
    transport = RecordingTransport(body)
    assert backend.refresh("itinerary-1", transport=transport) == body
    (url,) = transport.urls
    assert path_of(url) == "/api/v6/refresh-itinerary"
    assert query_of(url)["itineraryId"] == ["itinerary-1"]


@pytest.mark.parametrize("value", [None, "", "   ", 42, ["itinerary-1"]])
def test_refresh_rejects_a_missing_or_non_string_id_before_any_request(value):
    transport = RecordingTransport({"synthetic": True})
    with pytest.raises(errors.ToolError) as caught:
        backend.refresh(value, transport=transport)
    assert caught.value.reason == errors.VALUE_OUT_OF_RANGE
    assert caught.value.outcome == errors.INVALID_INPUT
    assert caught.value.details["argument"] == "itinerary_id"
    assert transport.urls == []


@pytest.mark.parametrize(
    ("radius", "expected"),
    [(None, "1000"), (1000, "1000"), (1, "1"), (0, "0"), (2500, "2500"), (40000, "40000")],
)
def test_rentals_preserves_the_radius_default_and_never_invents_a_bound(radius, expected):
    # `None` (omitted or JSON null) keeps the endpoint default 1000. Any integer is
    # forwarded verbatim -- including 0 and values beyond transit_nearby_stops'
    # unrelated 1..5000 window, because this endpoint publishes no such bound.
    body = [{"synthetic": True, "lat": 55.45, "lon": -4.62}]
    transport = RecordingTransport(body)
    assert backend.rentals(55.45, -4.62, radius, transport=transport) == body
    (url,) = transport.urls
    assert path_of(url) == "/api/v1/rentals"
    assert query_of(url)["point"] == ["55.45,-4.62"]
    assert query_of(url)["radius"] == [expected]


@pytest.mark.parametrize(
    "radius",
    ["not-a-number", "1000", 12.5, 12.0, True, False, [1000], {"metres": 1000}],
)
def test_rentals_rejects_a_non_integer_radius_before_any_request(radius):
    transport = RecordingTransport([{"synthetic": True}])
    with pytest.raises(errors.ToolError) as caught:
        backend.rentals(55.45, -4.62, radius, transport=transport)
    assert caught.value.reason == errors.VALUE_OUT_OF_RANGE
    assert caught.value.outcome == errors.INVALID_INPUT
    assert caught.value.details["argument"] == "radius"
    expected_value = radius if isinstance(radius, str) else repr(radius)
    assert caught.value.details["value"] == expected_value
    assert transport.urls == []


def test_rentals_rejection_through_the_shared_boundary_is_a_result_not_a_raise():
    transport = RecordingTransport([{"synthetic": True}])
    result = backend.run(
        "rentals", {"lat": 55.45, "lon": -4.62, "radius": "not-a-number"}, transport=transport
    )
    assert result["error_type"] == errors.VALUE_OUT_OF_RANGE
    assert result["error_details"]["argument"] == "radius"
    assert result["outcome"] == errors.INVALID_INPUT
    assert transport.urls == []


def test_rentals_still_validates_coordinates_before_the_radius():
    transport = RecordingTransport([{"synthetic": True}])
    for arguments, argument in (
        ({"lat": 91, "lon": -4.62}, "lat"),
        ({"lat": 55.45, "lon": 181}, "lon"),
    ):
        result = backend.run("rentals", dict(arguments, radius="not-a-number"), transport=transport)
        assert result["error_type"] == errors.VALUE_OUT_OF_RANGE
        assert result["error_details"]["argument"] == argument
    assert transport.urls == []


def test_raw_payloads_keep_no_canonical_envelope():
    body = {"synthetic": True, "items": [1, 2]}
    transport = RecordingTransport(body)
    for result in (
        backend.health(transport=transport),
        backend.refresh("itinerary-1", transport=transport),
        backend.rentals(55.45, -4.62, transport=transport),
    ):
        assert result == body
        assert "outcome" not in result


# ── one shared request-mode vocabulary ──────────────────────────────


def test_one_owner_holds_the_request_mode_vocabulary():
    # Exact identity: the three modules import the shared owner's objects instead
    # of carrying literal copies, so a member cannot be added to one and missed in
    # another. Historical public names remain aliases.
    assert plan.ACCEPTED_MODES is modes.TRANSIT_REQUEST_MODES
    assert onetomany.ACCEPTED_TRANSIT_MODES is modes.TRANSIT_REQUEST_MODES
    assert geocode.GEOCODE_MODES is modes.TRANSIT_REQUEST_MODE_ORDER
    assert plan.REJECTED_STREET_MODES is modes.REJECTED_STREET_MODES
    assert onetomany.STREET_REQUEST_MODES is modes.REJECTED_STREET_MODES
    assert geocode.STREET_MODES is modes.REJECTED_STREET_MODE_ORDER
    assert plan.DEBUG_MODE_PREFIX == modes.DEBUG_MODE_PREFIX
    assert onetomany.modes.DEBUG_MODE_PREFIX == modes.DEBUG_MODE_PREFIX
    assert geocode.DEBUG_MODE_PREFIX == modes.DEBUG_MODE_PREFIX


def test_the_shared_vocabularies_keep_their_membership_and_order():
    assert frozenset(geocode.GEOCODE_MODES) == modes.TRANSIT_REQUEST_MODES
    assert len(modes.TRANSIT_REQUEST_MODE_ORDER) == len(modes.TRANSIT_REQUEST_MODES)
    assert geocode.GEOCODE_MODES[0] == "TRANSIT"
    assert geocode.GEOCODE_MODES[-1] == "FLEX"
    assert set(geocode.STREET_MODES) == modes.REJECTED_STREET_MODES
    assert tuple(geocode.STREET_MODES) == (
        "WALK", "BIKE", "RENTAL", "CAR", "HGV", "CAR_PARKING", "CAR_DROPOFF",
    )
    assert not modes.TRANSIT_REQUEST_MODES & modes.REJECTED_STREET_MODES


# ── one application version source ──────────────────────────────────


def test_one_public_application_version_source():
    assert transit_core.APPLICATION_VERSION == "3.0.0"
    assert transit_core.__version__ == transit_core.APPLICATION_VERSION
    assert server.SERVER_INFO["version"] == transit_core.APPLICATION_VERSION
    assert "0.1.0" not in {
        transit_core.APPLICATION_VERSION,
        transit_core.__version__,
        server.SERVER_INFO["version"],
    }


# ── import safety in a child interpreter ────────────────────────────

IMPORT_PROBE = r'''
import io
import json
import os
import socket
import sys
import urllib.request

REPO = os.environ["IMPORT_REPO"]
KIND = os.environ["IMPORT_STREAM_KIND"]
real_out = sys.stdout
attempts = []


def trap(label):
    def fail(*args, **kwargs):
        attempts.append(label)
        raise AssertionError("side effect during import: " + label)
    return fail


urllib.request.urlopen = trap("urllib.request.urlopen")
socket.socket.connect = trap("socket.socket.connect")
socket.create_connection = trap("socket.create_connection")


class SpyText:
    def __init__(self, name):
        self.name = name

    def __getattr__(self, attribute):
        attempts.append(self.name + "." + attribute)
        return trap(self.name + "." + attribute)


if KIND == "spy":
    sys.stdin, sys.stdout, sys.stderr = SpyText("stdin"), SpyText("stdout"), SpyText("stderr")
elif KIND == "text":
    sys.stdin, sys.stdout, sys.stderr = io.StringIO(), io.StringIO(), io.StringIO()
elif KIND == "none":
    sys.stdin = sys.stdout = sys.stderr = None
else:
    raise AssertionError(KIND)

sys.path.insert(0, REPO)
import transit_core.backend as backend

after_backend = list(attempts)
backend_imported_server = "server" in sys.modules

import server

result = {
    "backend": os.path.realpath(backend.__file__),
    "server": os.path.realpath(server.__file__),
    "attempts": attempts,
    "after_backend": after_backend,
    "backend_imported_server": backend_imported_server,
    "captured": "" if KIND != "text" else sys.stdout.getvalue(),
}
real_out.write(json.dumps(result) + "\n")
'''


@pytest.mark.parametrize("kind", ["spy", "text", "none"])
def test_importing_the_backend_and_server_has_no_side_effects(tmp_path, kind):
    # §contract 1: importing server or the application layer must not read or
    # reconfigure stdin, write to stdio, open a socket or start the loop. The spy
    # stream records every attribute access, including a `hasattr` probe.
    child = tmp_path / "import_probe.py"
    child.write_text(IMPORT_PROBE, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "IMPORT_REPO": REPO,
        "IMPORT_STREAM_KIND": kind,
    }
    completed = subprocess.run(
        [PROD_PY, str(child)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    log = json.loads(completed.stdout.strip().splitlines()[-1])
    assert log["attempts"] == []
    assert log["after_backend"] == []
    assert log["backend_imported_server"] is False
    assert log["captured"] == ""
    assert os.path.realpath(log["backend"]) == os.path.join(REPO, "transit_core", "backend.py")
    assert os.path.realpath(log["server"]) == os.path.join(REPO, "server.py")
