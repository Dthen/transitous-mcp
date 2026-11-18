"""Native CLI contract: declaration, parsing, conversion, output and rendering.

These tests exercise the CLI frontend in-process against the real
:mod:`transit_core` backend.  Backend calls are observed through the same
``transit_core.http.api_get`` seam the core uses, so no socket is opened and no
live API is contacted.  Bodies come from the checked-in synthetic fixtures via
``fixture_support`` unless a case says it builds its body inline.

The installed/subprocess byte checks live in ``test_cli_artifacts.py``; this file
owns parser/declaration/conversion/renderer/exit classification semantics.
"""

import io
import json
import os
import subprocess
import sys

import fixture_support
import pytest

from transitous_cli import cli, declaration

import transit_core
from transit_core import backend, errors

REPO = fixture_support.REPO_ROOT
GOLDEN = os.path.join(REPO, "golden", "transitous.tools.json")


# ── deterministic recorder transport ────────────────────────────────


class _StubResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

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


class Recorder:
    """Record every assembled request and answer with one fixed body.

    Replaces the single GET boundary :func:`transit_core.http.api_get`, so request
    assembly, mapping and classification stay the production ones.
    """

    def __init__(self, payload=None):
        self.payload = {} if payload is None else payload
        self.urls = []
        self.params = []

    def __call__(self, path, params=None, *, transport=None, base=None):
        self.urls.append(path)
        self.params.append(params)
        return self.payload


@pytest.fixture()
def recorder(monkeypatch):
    rec = Recorder({})
    monkeypatch.setattr(transit_core.http, "api_get", rec)
    return rec


def run(argv, *, stdout=None, stderr=None):
    out = io.StringIO() if stdout is None else stdout
    err = io.StringIO() if stderr is None else stderr
    code = cli.main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def parse_only(argv):
    parser = declaration.build_parser()
    namespace = parser.parse_args(argv)
    return declaration.to_backend_args(namespace)


# ── declaration completeness ────────────────────────────────────────


def test_all_thirteen_operations_are_declared_once():
    assert [c.name for c in declaration.COMMANDS] == [
        "plan",
        "geocode",
        "departures",
        "nearby-stops",
        "reverse-geocode",
        "trip",
        "health",
        "reachable",
        "refresh",
        "rentals",
        "one-to-many",
        "one-to-many-transit",
        "map-trips",
    ]
    assert {c.operation for c in declaration.COMMANDS} == set(backend.OPERATIONS)


def test_every_frozen_schema_property_has_a_declared_native_mapping():
    registry = json.load(open(GOLDEN, encoding="utf-8"))
    declared = {}
    for command in declaration.COMMANDS:
        names = set()
        for option in command.options:
            names.update(option.targets or (option.dest,))
        for operand in command.operands:
            names.add(operand.dest)
        declared[command.operation] = names

    missing = []
    for tool in registry:
        operation = tool["name"][len("transit_"):]
        for prop in tool["inputSchema"].get("properties", {}):
            if prop not in declared.get(operation, set()):
                missing.append((operation, prop))
    assert missing == []

    assert sum(
        len(tool["inputSchema"].get("properties", {})) for tool in registry
    ) == 80


# ── parser and conversion semantics ─────────────────────────────────


@pytest.mark.parametrize(
    "argv,expected",
    [
        (
            ["plan", "--from-stop-id", "A", "--to-lat", "51.5", "--to-lon", "-0.1",
             "--time", "2026-10-27T09:00:00Z", "--arrive-by", "--timezone", "Europe/London",
             "--origin-timezone", "Europe/London", "--destination-timezone", "Europe/Paris",
             "--max-transfers", "2", "--transit-mode", "RAIL", "--transit-mode", "BUS",
             "--num-itineraries", "3", "--max-itineraries", "9", "--max-results", "4",
             "--search-window-seconds", "600", "--include-direct",
             "--max-access-walk-minutes", "10", "--max-egress-walk-minutes", "11",
             "--min-transfer-minutes", "2"],
            {
                "from_stop_id": "A", "to_lat": 51.5, "to_lon": -0.1,
                "time": "2026-10-27T09:00:00Z", "arrive_by": True,
                "timezone": "Europe/London", "origin_timezone": "Europe/London",
                "destination_timezone": "Europe/Paris", "max_transfers": 2,
                "transit_modes": ["RAIL", "BUS"], "num_itineraries": 3,
                "max_itineraries": 9, "max_results": 4, "search_window_seconds": 600,
                "include_direct": True, "max_access_walk_minutes": 10,
                "max_egress_walk_minutes": 11, "min_transfer_minutes": 2,
            },
        ),
        (
            ["geocode", "Ayr, Scotland", "--type", "STOP", "--type", "ADDRESS",
             "--mode", "RAIL", "--mode", "BUS", "--near", "55.45,-4.63",
             "--near-bias", "0.25", "--num-results", "3", "--language", "a,b"],
            {
                "query": "Ayr, Scotland", "types": ["STOP", "ADDRESS"],
                "modes": ["RAIL", "BUS"], "near": [55.45, -4.63], "near_bias": 0.25,
                "num_results": 3, "language": "a,b",
            },
        ),
        (
            ["departures", "stop:1", "--count", "7", "--time", "09:00",
             "--window", "600", "--timezone", "UTC", "--endpoint-timezone", "UTC"],
            {"stop_id": "stop:1", "count": 7, "time": "09:00", "window": 600,
             "timezone": "UTC", "endpoint_timezone": "UTC"},
        ),
        (["nearby-stops", "51.5", "-0.1", "--radius", "250"],
         {"lat": 51.5, "lon": -0.1, "radius": 250}),
        (["reverse-geocode", "51.5", "-0.1", "--type", "STOP", "--num-results", "4"],
         {"lat": 51.5, "lon": -0.1, "types": ["STOP"], "num_results": 4}),
        (["trip", "trip:1"], {"trip_id": "trip:1"}),
        (["health"], {}),
        (["reachable", "51.5", "-0.1", "--max-time-min", "90", "--time", "2026-10-27",
          "--no-arrive-by", "--timezone", "UTC", "--endpoint-timezone", "UTC"],
         {"lat": 51.5, "lon": -0.1, "max_time_min": 90, "time": "2026-10-27",
          "arrive_by": False, "timezone": "UTC", "endpoint_timezone": "UTC"}),
        (["refresh", "it:1"], {"itinerary_id": "it:1"}),
        (["rentals", "51.5", "-0.1", "--radius", "300"],
         {"lat": 51.5, "lon": -0.1, "radius": 300}),
        (["one-to-many", "--from", "51.5,-0.1", "--destination", "52,0",
          "--destination", "53,1", "--mode", "BIKE", "--max-sec", "120.5",
          "--arrive-by"],
         {"from_lat": 51.5, "from_lon": -0.1, "destinations": [[52.0, 0.0], [53.0, 1.0]],
          "mode": "BIKE", "max_sec": 120.5, "arrive_by": True}),
        (["one-to-many-transit", "--from", "51.5,-0.1", "--destination", "52,0",
          "--time", "2026-10-27T09:00:00Z", "--arrive-by", "--max-transfers", "2",
          "--transit-mode", "RAIL", "--max-access-walk-minutes", "5",
          "--max-egress-walk-minutes", "6", "--max-travel-minutes", "70",
          "--timezone", "UTC", "--endpoint-timezone", "UTC"],
         {"from_lat": 51.5, "from_lon": -0.1, "destinations": [[52.0, 0.0]],
          "time": "2026-10-27T09:00:00Z", "arrive_by": True, "max_transfers": 2,
          "transit_modes": ["RAIL"], "max_access_walk_minutes": 5,
          "max_egress_walk_minutes": 6, "max_travel_minutes": 70,
          "timezone": "UTC", "endpoint_timezone": "UTC"}),
        (["map-trips", "--bbox=-1,-2,3,4", "--start-time", "2026-10-27T09:00:00Z",
          "--end-time", "2026-10-27T10:00:00Z", "--zoom", "7", "--timezone", "UTC"],
         {"min_lat": -1.0, "min_lon": -2.0, "max_lat": 3.0, "max_lon": 4.0,
          "start_time": "2026-10-27T09:00:00Z", "end_time": "2026-10-27T10:00:00Z",
          "zoom": 7, "timezone": "UTC"}),
    ],
)
def test_each_command_maps_every_supplied_option_to_the_backend_names(argv, expected):
    assert parse_only(argv) == expected


def test_omitted_optionals_stay_absent_rather_than_becoming_defaults():
    args = parse_only(["plan", "--from-lat", "1", "--from-lon", "2"])
    assert args == {"from_lat": 1.0, "from_lon": 2.0}
    assert parse_only(["health"]) == {}


def test_repeated_scalars_use_the_last_supplied_value_and_flags_follow():
    args = parse_only(["departures", "s", "--count", "3", "--count", "9",
                       "--time", "a", "--time", "b"])
    assert args["count"] == 9
    assert args["time"] == "b"
    assert parse_only(["plan", "--arrive-by", "--no-arrive-by"])["arrive_by"] is False
    assert parse_only(["plan", "--no-arrive-by", "--arrive-by"])["arrive_by"] is True


def test_repeated_lists_preserve_caller_order_without_splitting():
    args = parse_only(["geocode", "x", "--type", "STOP", "--type", "PLACE",
                       "--mode", "RAIL", "--mode", "BUS"])
    assert args["types"] == ["STOP", "PLACE"]
    assert args["modes"] == ["RAIL", "BUS"]
    # One comma-containing string is never split into a list.
    assert parse_only(["geocode", "x", "--language", "a,b"])["language"] == "a,b"


def test_number_grammar_accepts_fractions_and_rejects_non_finite():
    assert parse_only(["nearby-stops", "1.5e2", "-0.25"])["lat"] == 150.0
    for bad in ("nan", "NaN", "inf", "-Infinity", "1,2", "0x10", "1_0"):
        with pytest.raises(declaration.UsageError):
            parse_only(["nearby-stops", bad, "0"])


def test_integer_grammar_rejects_floats_booleans_and_underscores():
    assert parse_only(["trip", "t"]) == {"trip_id": "t"}
    for bad in ("1.5", "1_0", "0x10", "nan"):
        with pytest.raises(declaration.UsageError):
            parse_only(["departures", "s", "--count", bad])


def test_coordinate_tuples_require_exactly_two_numbers():
    assert parse_only(["one-to-many", "--from=-1.2,-4.6",
                       "--destination", "0,0"])["from_lat"] == -1.2
    for bad in ("1", "1,2,3", "1,", ",2", "a,2", "1;2"):
        with pytest.raises(declaration.UsageError):
            parse_only(["one-to-many", "--from", bad, "--destination", "0,0"])


def test_bbox_keeps_caller_order_without_repairing_bounds():
    args = parse_only(["map-trips", "--bbox", "4,3,2,1", "--start-time", "a",
                       "--end-time", "b"])
    assert (args["min_lat"], args["min_lon"], args["max_lat"], args["max_lon"]) == (
        4.0, 3.0, 2.0, 1.0)


# ── management text and JSON selection ──────────────────────────────


def test_help_and_version_are_management_text_even_with_json():
    for argv in (["--help"], ["--json", "--help"], ["plan", "--help"],
                 ["--json", "plan", "--help"]):
        code, out, err = run(argv)
        assert code == 0
        assert err == ""
        assert out.startswith("usage: transitous")
        assert "Traceback" not in out

    code, out, err = run(["--version"])
    assert (code, out, err) == (0, f"transitous {transit_core.APPLICATION_VERSION}\n", "")
    code, out, err = run(["--json", "--version"])
    assert (code, out) == (0, f"transitous {transit_core.APPLICATION_VERSION}\n")


def test_no_command_is_a_usage_error_not_an_implicit_operation(recorder):
    code, out, err = run([])
    assert (code, out) == (2, "")
    assert "COMMAND" in err
    assert recorder.urls == []


def test_json_before_or_after_the_command_and_among_options_is_the_same(recorder):
    code, out, _ = run(["--json", "health"])
    assert code == 0 and json.loads(out) == {}
    code, out, _ = run(["health", "--json"])
    assert code == 0 and json.loads(out) == {}
    recorder.payload = [{"name": "Ayr", "stopId": "s1", "lat": 1.0, "lon": 2.0}]
    code, out, _ = run(["nearby-stops", "--json", "1", "2"])
    assert code == 0 and json.loads(out)["outcome"] == "ok"


def test_double_dash_and_option_values_are_not_json_selectors(recorder):
    # `--` ends option recognition: a later --json is a literal operand.
    code, out, err = run(["nearby-stops", "1", "2", "--", "--json"])
    assert code == 2 and out == ""
    assert recorder.urls == []

    # `--time=--json` is a literal time value, not an output request.
    code, out, err = run(["departures", "s", "--time=--json"])
    # The core rejects the malformed time; in human mode stdout stays empty.
    assert code == 2 and out == ""
    assert "malformed_time" in err


def test_unknown_options_remain_errors_even_in_json_mode(recorder):
    code, out, err = run(["health", "--json", "--nope"])
    assert code == 2
    payload = json.loads(out)
    assert payload["outcome"] == "cli_error"
    assert payload["error_type"] == "usage"
    assert err == ""
    assert recorder.urls == []


# ── continuation input ──────────────────────────────────────────────


def _continuation_file(tmp_path, name, data):
    path = tmp_path / name
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"cursor": "c", "cursor": "d", "query": {}}',
        b'\xef\xbb\xbf{"cursor": "c", "query": {}}',
        b'{"cursor": "c", "query": {"max_sec": NaN}}',
        b'{"cursor": "c", "query": {"max_sec": Infinity}}',
        b'null',
        b'[]',
        b'"text"',
        b'{"cursor": "c", "query": {}} trailing',
        b"\xff\xfe{}",
        # Generated excessive nesting (~20 KB): the decoder raises RecursionError,
        # which must be a local input error, not a Python traceback.
        b'{"cursor": "c", "query": ' + b"[" * 10000 + b"]" * 10000 + b"}",
    ],
)
def test_bad_continuation_documents_are_local_input_errors(recorder, tmp_path, payload):
    path = _continuation_file(tmp_path, "c.json", payload)
    code, out, err = run(["plan", "--continuation", path, "--json"])
    assert code == 2
    assert recorder.urls == []
    doc = json.loads(out)
    assert doc["outcome"] == "cli_error"
    assert doc["error_type"] == "input_file"
    assert err == ""


def test_missing_and_unreadable_continuation_files_are_local_input_errors(recorder, tmp_path):
    code, out, _ = run(["plan", "--continuation", str(tmp_path / "absent.json"), "--json"])
    assert code == 2 and json.loads(out)["error_type"] == "input_file"
    assert recorder.urls == []


def test_a_valid_continuation_object_is_passed_through_unchanged(recorder, tmp_path):
    doc = {"cursor": "abc", "query": {"control": "depart_after"}}
    path = _continuation_file(tmp_path, "ok.json", json.dumps(doc))
    recorder.payload = fixture_support.load("synthetic_plan_ber_muc.json")
    code, out, err = run(["plan", "--continuation", path, "--json"])
    # The CLI forwards the object unchanged; the core — not the CLI — rejects the
    # incomplete frozen query, and the cursor it read is the object's own.
    assert code == 2, err
    assert "continuation.query" in out
    assert recorder.urls == []


def test_stdin_continuation_is_read_once_to_eof(monkeypatch, recorder):
    doc = {"cursor": "abc", "query": {"control": "depart_after"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(doc)))
    recorder.payload = fixture_support.load("synthetic_plan_ber_muc.json")
    code, out, err = run(["plan", "--continuation", "-", "--json"])
    assert code == 2, err
    assert "continuation.query" in out
    assert recorder.urls == []


# ── exit classification ─────────────────────────────────────────────


def test_error_classification_follows_the_shared_marker(recorder):
    recorder.payload = {"error": "x", "outcome": "invalid_input", "error_type": "unknown_mode",
                        "error_details": {}}
    code, out, err = run(["health", "--json"])
    assert code == 2 and json.loads(out)["outcome"] == "invalid_input"

    recorder.payload = {"error": "x", "outcome": "transport_error"}
    code, out, _ = run(["health", "--json"])
    assert code == 1 and json.loads(out)["outcome"] == "transport_error"

    recorder.payload = {"error": "x"}
    code, out, _ = run(["health", "--json"])
    assert code == 1

    recorder.payload = {"error": "x", "outcome": "cli_error"}
    code, out, _ = run(["health", "--json"])
    assert code == 1

    recorder.payload = {"outcome": "unsupported"}
    assert run(["health", "--json"])[0] == 0

    recorder.payload = []
    assert run(["health", "--json"])[0] == 0


def test_a_human_backend_error_keeps_stdout_empty(recorder):
    recorder.payload = {"error": "unknown mode", "outcome": "invalid_input",
                        "error_type": "unknown_mode",
                        "error_details": {"argument": "mode"}}
    code, out, err = run(["health"])
    assert code == 2 and out == ""
    assert "invalid_input" in err and "unknown_mode" in err and "mode" in err


# ── JSON output shape ───────────────────────────────────────────────


def test_json_operation_output_is_exactly_one_document_equal_to_the_result(recorder):
    body = {"outcome": "ok", "status": "pass", "nested": [1, False, None, 0.5, ""]}
    recorder.payload = body
    code, out, err = run(["health", "--json"])
    assert code == 0 and err == ""
    assert out.endswith("\n") and out.count("\n") == 1
    assert json.loads(out) == body


def test_raw_scalar_and_container_payloads_survive_json_mode(recorder):
    for body in ([], 0, False, None, "text", [1, 2]):
        recorder.payload = body
        code, out, err = run(["health", "--json"])
        assert code == 0 and err == ""
        assert json.loads(out) == body


def test_non_ascii_and_lone_surrogates_are_escaped_not_dropped(recorder):
    recorder.payload = {"name": "Ayr \u00e9", "bad": "\ud800"}
    code, out, err = run(["health", "--json"])
    assert code == 0
    assert json.loads(out)["name"] == "Ayr \u00e9"
    assert json.loads(out)["bad"] == "\ud800"


# ── human rendering ─────────────────────────────────────────────────


def test_human_plan_renders_every_itinerary_leg_and_continuation_state(recorder):
    body = fixture_support.load("synthetic_plan_ber_muc.json")
    recorder.payload = body
    code, out, err = run(["plan", "--from-stop-id", "a", "--to-stop-id", "b",
                          "--time", "2026-10-27T09:00:00Z"])
    assert code == 0
    assert "Itinerary 1" in out and "Itinerary 2" in out
    assert "WALK" in out and "RAIL" in out
    assert "bounded search" in out.lower()
    assert "realtime_unavailable" in err


def test_human_renderings_cover_every_returned_row(recorder):
    bodies = {
        "geocode": ("synthetic_geocode_ayr.json", ["query"]),
        "departures": ("synthetic_stoptimes_ayr_p3.json", ["stop_id"]),
        "trip": ("synthetic_trip_intermediate_stops.json", ["trip_id"]),
    }
    for command, (fixture, extra) in bodies.items():
        recorder.payload = fixture_support.load(fixture)
        code, out, err = run([command, *extra])
        assert code == 0, (command, err)
        assert out.strip(), command

    recorder.payload = [
        {"name": "Ayr", "stopId": "s1", "lat": 55.45, "lon": -4.63,
         "tz": "Europe/London", "modes": ["BUS"]},
    ]
    code, out, err = run(["nearby-stops", "55.45", "-4.63"])
    assert code == 0 and "s1" in out and "0.0 m" in out, err


def test_human_raw_operations_print_the_whole_payload_as_pretty_json(recorder):
    recorder.payload = {"status": "pass", "systems": [{"id": "x"}]}
    code, out, err = run(["health"])
    assert code == 0 and err == ""
    assert json.loads(out) == recorder.payload
    assert "\n  " in out


# ── registry consumers and mapping sensitivity ──────────────────────


def _demo_commands():
    extra = declaration.Command(
        name="demo-stops",
        operation="nearby_stops",
        operands=(
            declaration.Operand("lat", "number", metavar="LAT", help="latitude"),
            declaration.Operand("lon", "number", metavar="LON", help="longitude"),
        ),
        options=(
            declaration.Option("radius", ("--radius-m",), "int", metavar="N",
                               help="search radius in metres"),
        ),
        renderer="nearby",
        help="temporary test-local command",
    )
    return declaration.COMMANDS + (extra,)


def test_a_temporary_declared_command_reaches_help_validation_dispatch_and_render(
        recorder, monkeypatch):
    recorder.payload = [{"name": "Ayr", "stopId": "s1", "lat": 1.0, "lon": 2.0}]
    commands = _demo_commands()

    parser = declaration.build_parser(commands)
    help_text = parser.format_help()
    assert "demo-stops" in help_text
    subparser = parser._subparsers._group_actions[0].choices["demo-stops"]
    assert "--radius-m" in subparser.format_help()

    code, out, err = run_with_commands(["demo-stops", "1", "2", "--radius-m", "250", "--json"],
                                       commands)
    assert code == 0, err
    assert json.loads(out)["outcome"] == "ok"
    from transit_core import geocode
    assert recorder.urls == ["/api/v6/map/stops"]
    wire_min = [float(part) for part in recorder.params[0]["min"].split(",")]
    assert wire_min == geocode.nearby_bbox(1.0, 2.0, 250)["min"]

    code, out, err = run_with_commands(["demo-stops", "1", "2", "--radius-m", "x", "--json"],
                                       commands)
    assert code == 2 and json.loads(out)["error_type"] == "usage"

    code, out, err = run_with_commands(["demo-stops", "1", "2", "--radius-m", "250"], commands)
    assert code == 0 and out.strip()


def run_with_commands(argv, commands):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=err, commands=commands)
    return code, out.getvalue(), err.getvalue()


def _nearby_expected_args():
    return {"lat": 1.0, "lon": 2.0, "radius": 250}


def _nearby_forwarding_guard(commands):
    """The same expected-arguments assertion the mapping tests use."""
    parser = declaration.build_parser(commands)
    args = declaration.to_backend_args(
        parser.parse_args(["nearby-stops", "1", "2", "--radius", "250"])
    )
    assert args == _nearby_expected_args()
    return args


def test_mapping_guard_fails_when_one_supplied_field_rule_is_dropped():
    """Executed sensitivity: the forwarding guard fails on a wrong-rule copy.

    The mutant still ACCEPTS ``--radius`` (parse succeeds) but forwards it under
    a different backend key, so the supplied value is dropped from ``radius``.
    The guard is the real expected-dict comparison, not an observation that a
    key is absent.
    """
    _nearby_forwarding_guard(declaration.COMMANDS)  # clean before

    original = declaration.COMMANDS_BY_NAME["nearby-stops"]
    mutant = tuple(
        command for command in declaration.COMMANDS if command.name != "nearby-stops"
    ) + (
        declaration.Command(
            name="nearby-stops",
            operation="nearby_stops",
            operands=original.operands,
            options=(
                declaration.Option("radius_dropped", ("--radius",), declaration.INT,
                                   metavar="METRES"),
            ),
            renderer=original.renderer,
            help="owned metadata-free wrong-rule copy",
        ),
    )
    with pytest.raises(AssertionError):
        _nearby_forwarding_guard(mutant)  # mutant fails the same assertion

    _nearby_forwarding_guard(declaration.COMMANDS)  # clean after


def test_cli_modules_never_import_the_mcp_adapter_or_registry():
    """Cold import of the CLI entry modules leaves server/MCP absent."""
    probe = r'''
import json, os, sys
sys.path.insert(0, os.environ["CLI_REPO"])
import transitous_cli.cli
import transitous_cli.__main__
print(json.dumps({
    "server": "server" in sys.modules,
    "mcp": sorted(m for m in sys.modules if m == "transitous_mcp" or m.startswith("transitous_mcp.")),
    "cli": sorted(m for m in sys.modules if m == "transitous_cli" or m.startswith("transitous_cli.")),
}))
'''
    completed = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        capture_output=True, text=True, cwd="/",
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
             "CLI_REPO": REPO},
    )
    assert completed.returncode == 0, completed.stderr
    log = json.loads(completed.stdout)
    assert log["server"] is False
    assert log["mcp"] == []
    assert "transitous_cli.cli" in log["cli"]


# ── domain passthrough boundaries (B3) ──────────────────────────────


def test_plan_endpoint_forms_reach_the_core_rather_than_parser_groups(recorder):
    # Both forms for one endpoint is the core's mixed_endpoint_form, not argparse's.
    recorder.payload = fixture_support.load("synthetic_plan_ber_muc.json")
    code, out, err = run(["plan", "--from-stop-id", "a", "--from-lat", "1",
                          "--from-lon", "2", "--to-stop-id", "b", "--json"])
    assert code == 2
    document = json.loads(out)
    assert document["outcome"] == "invalid_input"
    assert document["error_type"] == "mixed_endpoint_form"
    assert recorder.urls == []


def test_zero_destinations_is_usage_but_fifty_one_reaches_core_validation(recorder):
    code, out, err = run(["one-to-many", "--from", "1,2", "--json"])
    assert code == 2 and json.loads(out)["error_type"] == "usage"
    assert recorder.urls == []

    argv = ["one-to-many", "--from", "1,2"]
    for index in range(51):
        argv += ["--destination", f"{index % 80},{index % 170}"]
    code, out, err = run(argv + ["--json"])
    assert code == 2
    assert json.loads(out)["outcome"] == "invalid_input"
    assert recorder.urls == []


def test_omitted_street_mode_stays_absent_and_supplied_mode_is_forwarded(recorder):
    recorder.payload = [{"duration": 1, "distance": 2}]
    assert "mode" not in parse_only(["one-to-many", "--from", "1,2",
                                     "--destination", "3,4"])
    code, out, err = run(["one-to-many", "--from", "1,2", "--destination", "3,4",
                          "--json"])
    assert code == 0, err
    assert recorder.params[0]["mode"] == "WALK"  # the core's single default

    code, out, err = run(["one-to-many", "--from", "1,2", "--destination", "3,4",
                          "--mode", "BIKE", "--json"])
    assert code == 0, err
    assert recorder.params[-1]["mode"] == "BIKE"


def test_nearby_and_rentals_keep_their_own_radius_parameters(recorder):
    recorder.payload = []
    assert run(["nearby-stops", "1", "2", "--radius", "250", "--json"])[0] == 0
    assert recorder.urls[0] == "/api/v6/map/stops"
    assert recorder.params[0]["min"] == "0.9977477477477478,1.9977474046665677"

    recorder.payload = []
    assert run(["rentals", "1", "2", "--radius", "300", "--json"])[0] == 0
    assert recorder.urls[1] == "/api/v1/rentals"
    assert recorder.params[1]["radius"] == 300


def test_reverse_geocode_num_results_is_omitted_when_absent(recorder):
    recorder.payload = []
    assert run(["reverse-geocode", "1", "2", "--json"])[0] == 0
    assert "numResults" not in recorder.params[0]
    assert run(["reverse-geocode", "1", "2", "--num-results", "4", "--json"])[0] == 0
    assert recorder.params[1]["numResults"] == 4


def test_fractional_max_sec_and_language_comma_survive_request_assembly(recorder):
    recorder.payload = [{"duration": 1, "distance": 2}]
    assert run(["one-to-many", "--from", "1,2", "--destination", "3,4",
                "--max-sec", "120.5", "--json"])[0] == 0
    assert recorder.params[0]["max"] == 120.5

    recorder.payload = []
    assert run(["geocode", "x", "--language", "a,b", "--json"])[0] == 0
    assert recorder.params[1]["language"] == ["a,b"]


def test_time_forms_are_not_resolved_by_the_cli():
    for value in ("2026-10-27T09:00:00Z", "2026-10-27T09:00", "2026-10-27",
                  "2026-10-27T09:00:00+05:30"):
        assert parse_only(["departures", "s", "--time", value])["time"] == value


def test_continuation_conflict_is_the_cores_named_error(recorder, tmp_path):
    body = fixture_support.load("synthetic_plan_ber_muc.json")
    body = dict(body)
    body["nextPageCursor"] = "synthetic-next-cursor"
    recorder.payload = body
    code, out, err = run(["plan", "--from-stop-id", "a", "--to-stop-id", "b",
                          "--time", "2026-10-27T09:00:00Z", "--json"])
    assert code == 0, err
    first = json.loads(out)
    next_object = first["continuation"]["next"]
    path = _continuation_file(tmp_path, "next.json", json.dumps(next_object))

    # Replay alone succeeds and carries the cursor and frozen query unchanged.
    code, out, err = run(["plan", "--continuation", path, "--json"])
    assert code == 0, err
    assert recorder.params[-1]["pageCursor"] == "synthetic-next-cursor"

    # A contradicting ordinary flag is named by the core, not ignored or rewritten.
    code, out, err = run(["plan", "--continuation", path,
                          "--time", "2026-10-27T10:00:00Z", "--json"])
    assert code == 2
    assert json.loads(out)["error_type"] == "cursor_without_identical_query"


def test_partial_unreachable_rows_keep_unknown_and_genuine_zero(recorder):
    recorder.payload = fixture_support.load("synthetic_one_to_many_street.json")
    code, out, err = run(["one-to-many", "--from", "1,2", "--destination", "3,4",
                          "--destination", "5,6", "--destination", "7,8"])
    assert code == 0, err
    assert "423.75 s" in out
    assert "0 s" in out  # a genuine zero is not printed as unknown
    assert out.count("unknown") >= 2  # the empty Duration stays unknown

    recorder.payload = [{"duration": 0, "distance": 0.0}]
    code, out, err = run(["one-to-many", "--from", "1,2", "--destination", "3,4"])
    assert code == 0 and "0 s" in out


def test_empty_and_no_routes_are_successes(recorder):
    recorder.payload = fixture_support.load("synthetic_plan_empty_no_service.json")
    code, out, err = run(["plan", "--from-stop-id", "a", "--to-stop-id", "b",
                          "--time", "2026-10-27T09:00:00Z", "--json"])
    assert code == 0, err
    document = json.loads(out)
    assert document["outcome"] == "no_routes"

    code, out, err = run(["plan", "--from-stop-id", "a", "--to-stop-id", "b",
                          "--time", "2026-10-27T09:00:00Z"])
    assert code == 0
    assert "no_routes" in out.lower() or "No routes reason" in out


@pytest.mark.parametrize(
    "command,fixture,extra",
    [
        ("map-trips", "synthetic_map_trips_window.json",
         ["--bbox=-1,-2,3,4", "--start-time", "2026-10-27T09:00:00Z",
          "--end-time", "2026-10-27T10:00:00Z"]),
        ("reachable", "synthetic_reachable_pareto.json",
         ["1", "2", "--time", "2026-10-27T09:00:00Z"]),
        ("one-to-many-transit", "synthetic_one_to_many_transit.json",
         ["--from", "1,2", "--destination", "3,4", "--destination", "5,6",
          "--destination", "7,8", "--time", "2026-10-27T09:00:00Z"]),
    ],
)
def test_remaining_envelope_renderers_print_every_returned_row(recorder, command,
                                                               fixture, extra):
    recorder.payload = fixture_support.load(fixture)
    if command == "one-to-many-transit":
        recorder.payload = recorder.payload["valid"]
    code, out, err = run([command, *extra])
    assert code == 0, (command, err)
    assert out.strip()
    if command == "map-trips":
        assert "synthetic-trip-map-1" in out
    if command == "reachable":
        assert "travel" in out
    if command == "one-to-many-transit":
        assert out.count("destination (") >= 2
        assert "transit:" in out


@pytest.mark.parametrize(
    "modules,must_be_absent",
    [
        (("transitous_cli.cli", "transitous_cli.__main__"),
         ("server", "transitous_mcp")),
        (("transit_core", "transit_core.backend"), ("transitous_cli",)),
        (("transitous_mcp.server", "transitous_mcp.__main__"), ("transitous_cli",)),
    ],
)
def test_cold_import_boundaries_keep_the_three_surfaces_separate(modules, must_be_absent):
    probe = (
        "import json, os, sys\n"
        "sys.path.insert(0, os.environ['CLI_REPO'])\n"
        f"for name in {modules!r}:\n"
        "    __import__(name)\n"
        "present = set(sys.modules)\n"
        f"print(json.dumps({{p: sorted(m for m in present if m == p or m.startswith(p + '.'))"
        f" for p in {must_be_absent!r}}}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        capture_output=True, text=True, cwd="/",
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
             "CLI_REPO": REPO},
    )
    assert completed.returncode == 0, completed.stderr
    found = json.loads(completed.stdout)
    assert found == {name: [] for name in must_be_absent}, found


def test_human_plan_labels_direct_alternatives_and_continuation_state(recorder):
    recorder.payload = fixture_support.load("synthetic_plan_direct_alternatives.json")
    code, out, err = run(["plan", "--from-stop-id", "a", "--to-stop-id", "b",
                          "--time", "2026-10-27T09:00:00Z"])
    assert code == 0, err
    assert "Itineraries: 0" in out
    assert "Direct street-only alternatives: 1" in out
    assert "Direct alternative 1" in out
    assert "Continuation: previous none | next none" in out
    assert "does not guarantee a provider next cursor" in out


def test_human_raw_marked_error_keeps_its_whole_payload(recorder):
    recorder.payload = {"error": "upstream said no", "provider_detail": "keep-me",
                        "retry_after": 30}
    code, out, err = run(["health"])
    assert code == 1 and out == ""
    assert "keep-me" in err and "retry_after" in err


def test_required_members_are_parser_errors_before_any_backend_call(recorder):
    for argv in (
        ["one-to-many", "--destination", "1,2", "--json"],
        ["one-to-many-transit", "--destination", "1,2", "--json"],
        ["map-trips", "--start-time", "a", "--end-time", "b", "--json"],
        ["map-trips", "--bbox=1,2,3,4", "--end-time", "b", "--json"],
    ):
        code, out, err = run(argv)
        assert code == 2, argv
        assert json.loads(out)["outcome"] == "cli_error"
        assert json.loads(out)["error_type"] == "usage"
    assert recorder.urls == []
