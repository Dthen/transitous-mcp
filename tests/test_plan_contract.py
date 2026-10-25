"""T2 contract tests: request assembly, echo, caps, continuation, no-routes (§4.4–4.6).

Everything here runs offline through ``plan.plan(args, transport=...)`` — the
callable boundary T5 will wire to the ``transit_plan`` tool. The stub transport
records every URL, so a test can assert both *what* was sent and *how many*
requests were issued (the plan request itself, or a single zone-discovery
request).

Recorded corpus data is scratch data outside the checkout; the loader enforces
``TRANSITOUS_TEST_FIXTURES``. Small in-memory bodies are labelled as such in the
test that builds them.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixture_support

from transit_core import errors, plan

UTC = timezone.utc

NOW = datetime(2026, 10, 28, 6, 30, tzinfo=UTC)
LATER_NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

AYR_NCL_COORD = {
    "from_lat": 55.458267,
    "from_lon": -4.625827,
    "to_lat": 54.968338,
    "to_lon": -1.616046,
    "time": "2026-10-27T09:00:00Z",
    "transit_modes": ["RAIL"],
}

AYR_NCL_STOPIDS = {
    "from_stop_id": "gb-great-britain_910GAYRR",
    "to_stop_id": "gb-great-britain_910GNWCSTLE",
    "time": "2026-10-27T09:00:00Z",
    "num_itineraries": 3,
    "transit_modes": ["RAIL"],
}

FROZEN_FIELDS = {
    "from",
    "to",
    "control",
    "time",
    "timezone",
    "origin_timezone",
    "destination_timezone",
    "max_transfers",
    "num_itineraries",
    "max_itineraries",
    "max_results",
    "search_window_seconds",
    "max_access_walk_minutes",
    "max_egress_walk_minutes",
    "min_transfer_minutes",
    "transit_modes",
    "include_direct",
}

QUERY_FIELDS = {
    "control",
    "requested_instant",
    "effective_instant",
    "search_window_seconds",
    "minimum_requested",
    "upstream_count",
    "returned_count",
    "max_results",
    "truncated",
    "truncation_reason",
    "dropped_count",
    "previous_page_cursor",
    "next_page_cursor",
    "direct_count",
    "requested_modes",
    "applied_modes",
    "realtime_present",
}

#: The 23 accepted request modes of §4.4.1, in the specification's order.
ACCEPTED_MODES = [
    "TRANSIT",
    "TRAM",
    "SUBWAY",
    "METRO",
    "BUS",
    "COACH",
    "RAIL",
    "HIGHSPEED_RAIL",
    "LONG_DISTANCE",
    "NIGHT_RAIL",
    "REGIONAL_FAST_RAIL",
    "REGIONAL_RAIL",
    "SUBURBAN",
    "FUNICULAR",
    "AERIAL_LIFT",
    "AREAL_LIFT",
    "CABLE_CAR",
    "FERRY",
    "AIRPLANE",
    "OTHER",
    "ODM",
    "RIDE_SHARING",
    "FLEX",
]


# ── stub transport ──────────────────────────────────────────────────


class StubResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


class Transport:
    """Answers by URL substring, in registration order, and records the URLs."""

    def __init__(self, routes=None):
        self.routes = list(routes or ())
        self.urls = []

    def add(self, substring, payload, status=200):
        if isinstance(payload, bytes):
            body = payload
        elif isinstance(payload, str):
            body = payload.encode()
        else:
            body = json.dumps(payload).encode()
        self.routes.append((substring, status, body))
        return self

    def __call__(self, request, timeout):
        url = request.full_url
        self.urls.append(url)
        for substring, status, body in self.routes:
            if substring in url:
                return StubResponse(status, body)
        raise AssertionError(f"unexpected upstream request: {url}")

    @property
    def plan_urls(self):
        return [url for url in self.urls if "/api/v6/plan" in url]

    @property
    def discovery_urls(self):
        return [url for url in self.urls if "/api/v6/plan" not in url]


def fixture_bytes(name):
    return fixture_support.read_bytes(name)


def plan_transport(name, status=200):
    return Transport().add("/api/v6/plan", fixture_bytes(name), status)


def query_of(url):
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def one_param(url, name):
    values = query_of(url).get(name)
    assert values is not None, f"{name} absent from {url}"
    assert len(values) == 1, f"{name} repeated in {url}"
    return values[0]


def call(args, transport=None, now=None, body=None, status=200):
    """Run the plan callable with one upstream body registered."""
    if transport is None:
        transport = Transport()
        if body is not None:
            transport.add("/api/v6/plan", body, status)
    return plan.plan(args, transport=transport, now=now), transport


def minimal_itinerary(**overrides):
    itinerary = {
        "duration": 1800,
        "startTime": "2026-10-27T09:00:00Z",
        "endTime": "2026-10-27T09:30:00Z",
        "transfers": 0,
        "id": "itin-a",
        "legs": [
            {
                "mode": "RAIL",
                "startTime": "2026-10-27T09:00:00Z",
                "endTime": "2026-10-27T09:30:00Z",
                "scheduledStartTime": "2026-10-27T09:00:00Z",
                "scheduledEndTime": "2026-10-27T09:30:00Z",
                "realTime": False,
                "scheduled": True,
                "duration": 1800,
                "from": {
                    "name": "A",
                    "lat": 1.0,
                    "lon": 2.0,
                    "tz": "Europe/London",
                    "cancelled": False,
                },
                "to": {
                    "name": "B",
                    "lat": 3.0,
                    "lon": 4.0,
                    "tz": "Europe/London",
                    "cancelled": False,
                },
                "legGeometry": {"points": "abc", "precision": 6, "length": 100},
            }
        ],
    }
    itinerary.update(overrides)
    return itinerary


def minimal_body(itineraries=None, direct=None, **overrides):
    body = {
        "requestParameters": {},
        "debugOutput": {},
        "from": {"name": "A", "lat": 1.0, "lon": 2.0, "tz": "Europe/London"},
        "to": {"name": "B", "lat": 3.0, "lon": 4.0, "tz": "Europe/London"},
        "direct": [] if direct is None else direct,
        "itineraries": [minimal_itinerary()] if itineraries is None else itineraries,
        "previousPageCursor": None,
        "nextPageCursor": None,
    }
    body.update(overrides)
    return body


def assert_error(result, reason):
    assert errors.is_error_result(result), result
    assert result["error_type"] == reason, result
    assert result["outcome"] == errors.REASON_OUTCOME[reason]
    assert set(result) == {"error", "outcome", "error_type", "error_details"}
    return result["error_details"]


def _put(mapping, key, value):
    """Set a frozen-query member whose name is a Python keyword."""
    mapping[key] = value
    return mapping


# ── the exercised module is this checkout's ─────────────────────────


def test_plan_module_resolves_inside_this_checkout():
    path = os.path.realpath(plan.__file__)
    assert path == os.path.join(fixture_support.REPO_ROOT, "transit_core", "plan.py")


# ── §4.4 endpoint forms ─────────────────────────────────────────────


def test_origin_without_any_endpoint_form_is_rejected_before_any_request():
    transport = Transport()
    result = plan.plan({"to_lat": 54.9, "to_lon": -1.6}, transport=transport)
    details = assert_error(result, errors.MIXED_ENDPOINT_FORM)
    assert details["endpoint"] == "origin"
    assert details["forms_supplied"] == []
    assert transport.urls == []


def test_destination_without_any_endpoint_form_is_rejected():
    result = plan.plan(
        {"from_lat": 55.4, "from_lon": -4.6}, transport=Transport()
    )
    details = assert_error(result, errors.MIXED_ENDPOINT_FORM)
    assert details["endpoint"] == "destination"


def test_both_forms_for_one_endpoint_are_mixed_endpoint_form():
    args = dict(AYR_NCL_COORD, from_stop_id="gb-great-britain_910GAYRR")
    transport = Transport()
    result = plan.plan(args, transport=transport)
    details = assert_error(result, errors.MIXED_ENDPOINT_FORM)
    assert details["endpoint"] == "origin"
    assert sorted(details["forms_supplied"]) == ["coordinate", "stop_id"]
    assert transport.urls == []


def test_partial_coordinate_is_not_a_coordinate_form():
    args = dict(AYR_NCL_COORD)
    args.pop("from_lon")
    details = assert_error(plan.plan(args, transport=Transport()), errors.MIXED_ENDPOINT_FORM)
    assert details["endpoint"] == "origin"
    assert "from_lon" in json.dumps(details)


@pytest.mark.parametrize(
    "name,value",
    [
        ("from_lat", 90.001),
        ("from_lat", -90.001),
        ("from_lon", 180.001),
        ("from_lon", -180.001),
        ("from_lat", "55.4"),
        ("from_lat", True),
        ("to_lon", float("nan")),
        ("to_lat", float("inf")),
    ],
)
def test_coordinate_values_are_typed_and_bounded(name, value):
    args = dict(AYR_NCL_COORD)
    args[name] = value
    transport = Transport()
    details = assert_error(plan.plan(args, transport=transport), errors.VALUE_OUT_OF_RANGE)
    assert details["field"] == name
    assert transport.urls == []


def test_coordinate_bounds_are_inclusive():
    args = dict(AYR_NCL_COORD, from_lat=90, from_lon=-180, to_lat=-90, to_lon=180)
    transport = Transport().add("/api/v6/plan", minimal_body())
    result, _ = call(args, transport=transport)
    assert result["outcome"] == "ok"
    assert one_param(transport.plan_urls[0], "fromPlace") == "90,-180"


@pytest.mark.parametrize("stop_id", ["", "   "])
def test_empty_stop_id_is_rejected(stop_id):
    transport = Transport()
    result = plan.plan(
        {"from_stop_id": stop_id, "to_lat": 54.9, "to_lon": -1.6}, transport=transport
    )
    details = assert_error(result, errors.VALUE_OUT_OF_RANGE)
    assert details["field"] == "from_stop_id"
    assert transport.urls == []


def test_stop_id_and_coordinate_may_mix_across_endpoints():
    args = {
        "from_stop_id": "gb-great-britain_910GAYRR",
        "to_lat": 54.968338,
        "to_lon": -1.616046,
        "time": "2026-10-27T09:00:00Z",
    }
    transport = Transport().add("/api/v6/plan", minimal_body())
    result, _ = call(args, transport=transport)
    assert result["outcome"] == "ok"
    url = transport.plan_urls[0]
    assert one_param(url, "fromPlace") == "gb-great-britain_910GAYRR"
    assert one_param(url, "toPlace") == "54.968338,-1.616046"


# ── §4.4 numeric bounds ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,value",
    [
        ("max_transfers", -1),
        ("max_transfers", 11),
        ("max_transfers", 5.0),
        ("max_transfers", True),
        ("max_transfers", "5"),
        ("num_itineraries", 0),
        ("num_itineraries", 21),
        ("num_itineraries", False),
        ("max_itineraries", 0),
        ("max_itineraries", 51),
        ("max_results", 0),
        ("max_results", 51),
        ("search_window_seconds", 59),
        ("search_window_seconds", 86401),
        ("max_access_walk_minutes", -1),
        ("max_access_walk_minutes", 121),
        ("max_egress_walk_minutes", -1),
        ("max_egress_walk_minutes", 121),
        ("min_transfer_minutes", -1),
        ("min_transfer_minutes", 121),
        ("include_direct", 1),
        ("arrive_by", "true"),
    ],
)
def test_numeric_and_boolean_bounds(name, value):
    args = dict(AYR_NCL_COORD)
    args[name] = value
    transport = Transport()
    details = assert_error(plan.plan(args, transport=transport), errors.VALUE_OUT_OF_RANGE)
    assert details["field"] == name
    assert transport.urls == []


@pytest.mark.parametrize("name,value", [
    ("max_transfers", 0),
    ("max_transfers", 10),
    ("num_itineraries", 1),
    ("num_itineraries", 20),
    ("max_itineraries", 1),
    ("max_itineraries", 50),
    ("max_results", 1),
    ("max_results", 50),
    ("search_window_seconds", 60),
    ("search_window_seconds", 86400),
    ("max_access_walk_minutes", 0),
    ("max_access_walk_minutes", 120),
    ("max_egress_walk_minutes", 0),
    ("max_egress_walk_minutes", 120),
    ("min_transfer_minutes", 0),
    ("min_transfer_minutes", 120),
])
def test_numeric_bounds_accept_their_edges(name, value):
    args = dict(AYR_NCL_COORD)
    args[name] = value
    result, transport = call(args, body=minimal_body())
    assert result["outcome"] == "ok", result
    assert len(transport.plan_urls) == 1


# ── §4.4.1 modes ───────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ACCEPTED_MODES)
def test_every_accepted_mode_reaches_the_wire(mode):
    args = dict(AYR_NCL_COORD, transit_modes=[mode])
    result, transport = call(args, body=minimal_body())
    assert result["outcome"] == "ok", result
    assert one_param(transport.plan_urls[0], "transitModes") == mode


@pytest.mark.parametrize(
    "mode",
    ["WALK", "BIKE", "RENTAL", "CAR", "HGV", "CAR_PARKING", "CAR_DROPOFF", "DEBUG_FOO", "DEBUG_"],
)
def test_street_and_debug_modes_are_not_transit(mode):
    transport = Transport()
    result = plan.plan(dict(AYR_NCL_COORD, transit_modes=[mode]), transport=transport)
    details = assert_error(result, errors.STREET_MODE_NOT_TRANSIT)
    assert details["mode"] == mode
    assert transport.urls == []


@pytest.mark.parametrize("mode", ["RAILX", "rail", " RAIL", "FOOT", ""])
def test_unknown_mode_is_not_alias_resolved(mode):
    transport = Transport()
    result = plan.plan(dict(AYR_NCL_COORD, transit_modes=[mode]), transport=transport)
    details = assert_error(result, errors.UNKNOWN_MODE)
    assert details["mode"] == mode
    assert transport.urls == []


@pytest.mark.parametrize("value", ["RAIL", [], [1], {"RAIL": True}, 7])
def test_transit_modes_must_be_a_non_empty_array_of_strings(value):
    transport = Transport()
    result = plan.plan(dict(AYR_NCL_COORD, transit_modes=value), transport=transport)
    details = assert_error(result, errors.VALUE_OUT_OF_RANGE)
    assert details["field"] == "transit_modes"
    assert transport.urls == []


def test_a_tuple_of_modes_is_the_same_ordered_array():
    result, transport = call(dict(AYR_NCL_COORD, transit_modes=("TRAM", "RAIL")), body=minimal_body())
    assert result["outcome"] == "ok", result
    assert one_param(transport.plan_urls[0], "transitModes") == "TRAM,RAIL"


def test_caller_mode_order_is_preserved_as_one_value():
    args = dict(AYR_NCL_COORD, transit_modes=["SUBURBAN", "RAIL", "TRAM"])
    result, transport = call(args, body=minimal_body())
    url = transport.plan_urls[0]
    assert one_param(url, "transitModes") == "SUBURBAN,RAIL,TRAM"
    assert len(query_of(url)["transitModes"]) == 1
    assert result["query"]["requested_modes"] == ["SUBURBAN", "RAIL", "TRAM"]
    assert result["query"]["applied_modes"] == "SUBURBAN,RAIL,TRAM"


# ── §4.4 / §2.1 time forms ─────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["2026-02-30T09:00:00Z", "09:00:00Z", "2026-10-27T", "2026-10-27 09:00:00Z", "2026-10-27T25:00:00Z", 42],
)
def test_malformed_time_is_rejected_before_any_request(value):
    transport = Transport()
    result = plan.plan(dict(AYR_NCL_COORD, time=value), transport=transport)
    assert_error(result, errors.MALFORMED_TIME)
    assert transport.urls == []


@pytest.mark.parametrize(
    "name", ["timezone", "origin_timezone", "destination_timezone"]
)
def test_unknown_timezone_arguments_are_caller_errors(name):
    transport = Transport()
    result = plan.plan(dict(AYR_NCL_COORD, **{name: "Mars/Olympus"}), transport=transport)
    details = assert_error(result, errors.UNKNOWN_TIMEZONE)
    assert details["zone"] == "Mars/Olympus"
    assert transport.urls == []


def test_explicit_instant_is_preserved_and_needs_no_discovery():
    transport = plan_transport("plan_ayr_ncl_coord.json")
    result, _ = call(AYR_NCL_COORD, transport=transport)
    frame = result["time_frame"]
    assert frame["form"] == "instant"
    assert frame["as_given"] == "2026-10-27T09:00:00Z"
    assert frame["control"] == "depart_after"
    assert frame["frame_zone"] is None
    assert frame["frame_source"] == "not_applicable"
    assert frame["dst_flags"] == []
    # §2.5: the ISO form always carries a numeric offset, never a Z
    assert frame["interpreted_instant"]["iso"] == "2026-10-27T09:00:00+00:00"
    assert frame["interpreted_instant"]["epoch_seconds"] == 1793091600
    assert transport.discovery_urls == []
    assert one_param(transport.plan_urls[0], "time") == "2026-10-27T09:00:00Z"


def test_offset_instant_is_sent_as_the_same_moment():
    args = dict(AYR_NCL_COORD, time="2026-10-27T10:00:00+01:00")
    result, transport = call(args, body=minimal_body())
    assert one_param(transport.plan_urls[0], "time") == "2026-10-27T09:00:00Z"
    assert result["time_frame"]["as_given"] == "2026-10-27T10:00:00+01:00"
    assert result["time_frame"]["interpreted_instant"]["epoch_seconds"] == 1793091600


@pytest.mark.parametrize(
    "wall_clock,expected_iso,expected_offset,expected_wire",
    [
        # Europe/London is BST (+01:00) until 2026-10-25 and GMT (+00:00) after it,
        # so the same wall clock must be read at a different offset either side.
        ("2026-10-20T09:00:00", "2026-10-20T09:00:00+01:00", 3600, "2026-10-20T08:00:00Z"),
        ("2026-10-27T09:00:00", "2026-10-27T09:00:00+00:00", 0, "2026-10-27T09:00:00Z"),
    ],
    ids=["bst", "gmt"],
)
def test_naive_time_read_in_the_caller_timezone_issues_no_discovery(
    wall_clock, expected_iso, expected_offset, expected_wire
):
    args = dict(AYR_NCL_COORD, time=wall_clock, timezone="Europe/London")
    result, transport = call(args, body=minimal_body())
    frame = result["time_frame"]
    assert frame["form"] == "naive_local"
    assert frame["frame_zone"] == "Europe/London"
    assert frame["frame_source"] == "caller_timezone"
    # the wall clock is read in Europe/London's offset for that date, sent as its instant
    assert frame["interpreted_instant"]["iso"] == expected_iso
    assert frame["interpreted_instant"]["local"] == wall_clock
    assert frame["interpreted_instant"]["utc_offset_seconds"] == expected_offset
    assert transport.discovery_urls == []
    assert one_param(transport.plan_urls[0], "time") == expected_wire


def test_naive_time_at_a_stop_id_endpoint_uses_stop_metadata():
    stop = {
        "place": {
            "name": "Ayr",
            "stopId": "gb-great-britain_910GAYRR",
            "lat": 55.458267,
            "lon": -4.625827,
            "tz": "Europe/London",
        }
    }
    transport = Transport().add("/api/v6/stop", stop).add("/api/v6/plan", minimal_body())
    args = dict(AYR_NCL_STOPIDS, time="2026-10-27T09:00:00")
    result, _ = call(args, transport=transport)
    frame = result["time_frame"]
    assert frame["form"] == "naive_local"
    assert frame["frame_zone"] == "Europe/London"
    assert frame["frame_source"] == "endpoint_stop_info"
    assert len(transport.discovery_urls) == 1
    assert "stopId=gb-great-britain_910GAYRR" in transport.discovery_urls[0]
    assert len(transport.plan_urls) == 1


@pytest.mark.parametrize(
    "date,expected_iso,expected_offset,expected_wire",
    [
        # local midnight is 00:00 BST in summer and 00:00 GMT in winter; the
        # instant it denotes differs by one hour even though the clock face does not.
        ("2026-10-20", "2026-10-20T00:00:00+01:00", 3600, "2026-10-19T23:00:00Z"),
        ("2026-10-27", "2026-10-27T00:00:00+00:00", 0, "2026-10-27T00:00:00Z"),
    ],
    ids=["bst", "gmt"],
)
def test_date_only_time_is_local_midnight_in_the_frame(
    date, expected_iso, expected_offset, expected_wire
):
    args = dict(AYR_NCL_COORD, time=date, timezone="Europe/London")
    result, transport = call(args, body=minimal_body())
    frame = result["time_frame"]
    assert frame["form"] == "date_only"
    assert frame["interpreted_instant"]["iso"] == expected_iso
    assert frame["interpreted_instant"]["local"] == f"{date}T00:00:00"
    assert frame["interpreted_instant"]["utc_offset_seconds"] == expected_offset
    assert one_param(transport.plan_urls[0], "time") == expected_wire


@pytest.mark.parametrize("spelling", ["absent", "null"])
def test_omitted_time_resolves_exactly_once(spelling):
    calls = []

    def clock():
        calls.append(1)
        return NOW

    # AYR_NCL_COORD carries an explicit instant, so it is not an omitted-time case:
    # both accepted omitted spellings (argument absent, argument null) must drop it.
    args = dict(AYR_NCL_COORD)
    args.pop("time")
    if spelling == "null":
        args["time"] = None
    result, transport = call(
        args,
        body=minimal_body(previousPageCursor="EARLIER|1793091600", nextPageCursor="LATER|1793098920"),
        now=clock,
    )
    frame = result["time_frame"]
    assert calls == [1]
    assert frame["form"] == "omitted"
    assert frame["as_given"] is None
    assert frame["interpreted_instant"]["iso"] == "2026-10-28T06:30:00+00:00"
    assert frame["interpreted_instant"]["zone"] is None
    assert frame["interpreted_instant"]["zone_source"] == "unavailable"
    assert one_param(transport.plan_urls[0], "time") == "2026-10-28T06:30:00Z"
    # the instant resolved once is the one frozen into both continuation directions
    continuation = result["continuation"]
    assert continuation["previous"] is not None and continuation["next"] is not None
    for direction in continuation.values():
        assert direction["query"]["time"] == "2026-10-28T06:30:00Z"


def test_arrive_by_reads_the_destination_zone_for_a_naive_deadline():
    args = dict(
        AYR_NCL_STOPIDS,
        time="2026-10-27T22:00:00",
        arrive_by=True,
        num_itineraries=1,
    )
    # An arrive-by naive deadline is read in the *destination* zone, so the one
    # budgeted discovery request is the destination stop's metadata, not the origin's.
    destination_stop = {
        "place": {
            "name": "Newcastle",
            "stopId": "gb-great-britain_910GNWCSTLE",
            "lat": 54.968338,
            "lon": -1.616046,
            "tz": "Europe/London",
        }
    }
    transport = (
        Transport()
        .add("/api/v6/stop", destination_stop)
        .add("/api/v6/plan", fixture_bytes("plan_ayr_ncl_arriveby.json"))
    )
    result, _ = call(args, transport=transport)
    frame = result["time_frame"]
    assert frame["control"] == "arrive_by"
    assert frame["form"] == "naive_local"
    assert frame["frame_zone"] == "Europe/London"
    assert frame["frame_source"] == "endpoint_stop_info"
    assert frame["interpreted_instant"]["iso"] == "2026-10-27T22:00:00+00:00"
    # destination authority: exactly one discovery request, for the destination stop
    assert len(transport.discovery_urls) == 1
    assert "stopId=gb-great-britain_910GNWCSTLE" in transport.discovery_urls[0]
    assert "910GAYRR" not in transport.discovery_urls[0]
    assert one_param(transport.plan_urls[0], "arriveBy") == "true"
    deadline = frame["interpreted_instant"]["epoch_seconds"]
    assert result["itineraries"], result
    for itinerary in result["itineraries"]:
        assert itinerary["arrival"]["epoch_seconds"] <= deadline


def test_ambiguous_local_clock_is_rejected_with_no_plan_request():
    args = dict(
        AYR_NCL_COORD, time="2026-10-25T01:30:00", timezone="Europe/London"
    )
    transport = Transport()
    result = plan.plan(args, transport=transport)
    details = assert_error(result, errors.AMBIGUOUS_LOCAL_TIME)
    assert [candidate["utc_offset_seconds"] for candidate in details["candidates"]] == [3600, 0]
    assert details["zone"] == "Europe/London"
    assert transport.urls == []


def test_nonexistent_local_clock_is_rejected_before_the_plan_request():
    args = dict(AYR_NCL_COORD, time="2026-03-29T01:30:00", timezone="Europe/London")
    transport = Transport()
    result = plan.plan(args, transport=transport)
    details = assert_error(result, errors.NONEXISTENT_LOCAL_TIME)
    assert details["gap"]["start"] == "2026-03-29T01:00:00"
    assert details["gap"]["end"] == "2026-03-29T02:00:00"
    assert transport.urls == []


# ── §4.3 zone authority and disagreement ───────────────────────────


def _reverse_geocode(tz, lat, lon):
    return [{"name": "Nearby", "id": "gb-test-1", "lat": lat, "lon": lon, "tz": tz}]


def test_coordinate_frame_comes_from_bounded_inference():
    transport = (
        Transport()
        .add(
            "/api/v1/reverse-geocode",
            _reverse_geocode("Europe/London", 55.4583, -4.6258),
        )
        .add("/api/v6/plan", minimal_body())
    )
    args = dict(AYR_NCL_COORD, time="2026-10-27T09:00:00")
    result, _ = call(args, transport=transport)
    frame = result["time_frame"]
    assert frame["frame_source"] == "endpoint_reverse_geocode"
    assert frame["frame_zone"] == "Europe/London"
    assert len(transport.discovery_urls) == 1


def test_unresolvable_coordinate_frame_names_the_endpoint_and_the_remedy():
    transport = Transport().add(
        "/api/v1/reverse-geocode", _reverse_geocode("Europe/London", 48.85, 2.35)
    )
    args = dict(AYR_NCL_COORD, time="2026-10-27T09:00:00")
    result = plan.plan(args, transport=transport)
    details = assert_error(result, errors.TIMEZONE_UNRESOLVED)
    assert details["endpoint"] == "origin"
    assert details["distance_m"] > 500
    assert "remedy" in details
    assert transport.plan_urls == []


def test_zone_disagreement_is_unsupported_and_names_both_values():
    body = minimal_body()
    body["from"]["tz"] = "Europe/Berlin"
    transport = (
        Transport()
        .add("/api/v1/reverse-geocode", _reverse_geocode("Europe/London", 55.4583, -4.6258))
        .add("/api/v6/plan", body)
    )
    args = dict(AYR_NCL_COORD, time="2026-10-27T09:00:00")
    result = plan.plan(args, transport=transport)
    details = assert_error(result, errors.TIMEZONE_DISAGREEMENT)
    assert details["request_zone"] == "Europe/London"
    assert details["response_zone"] == "Europe/Berlin"
    assert details["request_source"] == "endpoint_reverse_geocode"
    assert details["response_source"] == "response_place"
    assert result["outcome"] == errors.UNSUPPORTED


def test_caller_timezone_is_never_a_disagreement():
    body = minimal_body()
    body["from"]["tz"] = "Europe/Berlin"
    transport = Transport().add("/api/v6/plan", body)
    args = dict(AYR_NCL_COORD, time="2026-10-27T09:00:00", timezone="Europe/London")
    result, _ = call(args, transport=transport)
    assert result["outcome"] == "ok"
    assert result["time_frame"]["frame_source"] == "caller_timezone"


# ── §4.5.1 effective-query echo ────────────────────────────────────


def test_echo_has_exactly_the_documented_fields():
    transport = plan_transport("plan_ayr_ncl_coord.json")
    result, _ = call(AYR_NCL_COORD, transport=transport)
    assert set(result) == {
        "outcome",
        "constraints_active",
        "time_frame",
        "query",
        "continuation",
        "from",
        "to",
        "itineraries",
        "warnings",
    }
    assert set(result["query"]) == QUERY_FIELDS


def test_echo_reports_counts_window_and_cursors():
    transport = plan_transport("plan_ayr_ncl_coord.json")
    result, _ = call(dict(AYR_NCL_COORD, search_window_seconds=1200), transport=transport)
    query = result["query"]
    assert query["control"] == "depart_after"
    assert query["search_window_seconds"] == 1200
    assert one_param(transport.plan_urls[0], "searchWindow") == "1200"
    assert query["minimum_requested"] == 5
    assert query["upstream_count"] == 5
    assert query["returned_count"] == 5
    assert query["max_results"] is None
    assert query["truncated"] is False
    assert query["truncation_reason"] == "none"
    assert query["dropped_count"] == 0
    assert query["previous_page_cursor"] == "EARLIER|1793091600"
    assert query["next_page_cursor"] == "LATER|1793098920"
    assert query["direct_count"] == 0
    assert query["realtime_present"] is False


def test_requested_and_effective_instant_are_both_timestamp_objects():
    result, _ = call(dict(AYR_NCL_COORD, time="2026-10-27T09:00:00Z"), body=minimal_body())
    for key in ("requested_instant", "effective_instant"):
        instant = result["query"][key]
        assert set(instant) == {
            "iso",
            "epoch_seconds",
            "local",
            "local_date",
            "utc_offset_seconds",
            "zone",
            "zone_source",
            "display",
        }
    assert result["query"]["requested_instant"] == result["query"]["effective_instant"]
    assert result["query"]["effective_instant"] == result["time_frame"]["interpreted_instant"]


# ── §4.6 caps, ordering, truncation ────────────────────────────────


def test_no_default_wrapper_slice_returns_every_upstream_itinerary():
    transport = plan_transport("plan_ayr_ncl_coord.json")
    result, _ = call(AYR_NCL_COORD, transport=transport)
    assert len(result["itineraries"]) == 5
    assert result["query"]["returned_count"] == 5
    assert [item["option"] for item in result["itineraries"]] == [1, 2, 3, 4, 5]


def test_max_results_is_the_only_wrapper_cap():
    transport = plan_transport("plan_ayr_ncl_coord.json")
    args = dict(AYR_NCL_COORD, max_results=2)
    result, _ = call(args, transport=transport)
    query = result["query"]
    assert len(result["itineraries"]) == 2
    assert query["upstream_count"] == 5
    assert query["returned_count"] == 2
    assert query["max_results"] == 2
    assert query["truncated"] is True
    assert query["truncation_reason"] == "max_results"
    assert query["dropped_count"] == 3
    # max_results is a wrapper cap and is never sent upstream
    assert "max_results" not in query_of(transport.plan_urls[0])
    assert "maxResults" not in query_of(transport.plan_urls[0])


def test_truncated_is_not_inferred_from_equality():
    transport = plan_transport("plan_ayr_ncl_coord.json")
    result, _ = call(dict(AYR_NCL_COORD, max_results=5), transport=transport)
    assert result["query"]["truncated"] is False
    assert result["query"]["truncation_reason"] == "none"
    assert result["query"]["dropped_count"] == 0
    assert len(result["itineraries"]) == 5


def test_truncation_keeps_upstream_order():
    upstream = fixture_support.load("plan_ayr_ncl_coord.json")
    transport = plan_transport("plan_ayr_ncl_coord.json")
    result, _ = call(dict(AYR_NCL_COORD, max_results=3), transport=transport)
    assert [item["itinerary_id"] for item in result["itineraries"]] == [
        itinerary["id"] for itinerary in upstream["itineraries"][:3]
    ]


def test_num_itineraries_maps_to_the_upstream_minimum():
    transport = plan_transport("plan_ayr_man_stopids_n8.json.gz")
    args = {
        "from_stop_id": "gb-great-britain_910GAYRR",
        "to_stop_id": "gb-great-britain_910GMNCRPIC",
        "time": "2026-10-27T09:00:00Z",
        "num_itineraries": 8,
        "transit_modes": ["RAIL"],
    }
    result, _ = call(args, transport=transport)
    query = result["query"]
    assert one_param(transport.plan_urls[0], "numItineraries") == "8"
    assert query["minimum_requested"] == 8
    assert query["upstream_count"] == 8
    assert query["returned_count"] == 8
    assert query["truncated"] is False
    assert 15480 in [item["elapsed_seconds"] for item in result["itineraries"]]


def test_max_itineraries_maps_to_the_computation_cap_and_is_not_a_result_limit():
    transport = plan_transport("plan_ayr_ncl_coord.json")
    result, _ = call(dict(AYR_NCL_COORD, max_itineraries=7), transport=transport)
    assert one_param(transport.plan_urls[0], "maxItineraries") == "7"
    assert result["query"]["returned_count"] == 5
    assert result["query"]["truncated"] is False


def test_walk_caps_are_converted_to_seconds():
    transport = plan_transport("plan_glw_ncl_walk60.json")
    args = {
        "from_stop_id": "gb-great-britain_910GGLGC",
        "to_stop_id": "gb-great-britain_910GNWCSTLE",
        "time": "2026-10-27T09:00:00Z",
        "max_transfers": 3,
        "transit_modes": ["RAIL"],
        "max_access_walk_minutes": 60,
        "max_egress_walk_minutes": 60,
        "min_transfer_minutes": 2,
    }
    result, _ = call(args, transport=transport)
    url = transport.plan_urls[0]
    assert one_param(url, "maxPreTransitTime") == "3600"
    assert one_param(url, "maxPostTransitTime") == "3600"
    assert one_param(url, "minTransferTime") == "120"
    assert result["query"]["search_window_seconds"] == 900


# ── §4.6.1 frozen query ────────────────────────────────────────────


def test_frozen_query_has_the_closed_canonical_field_set():
    transport = Transport().add("/api/v6/plan", _page("page1"))
    args = {
        "from_lat": 55.858315,
        "from_lon": -4.258436,
        "to_lat": 55.458267,
        "to_lon": -4.625827,
        "num_itineraries": 2,
        "transit_modes": ["RAIL"],
        "max_access_walk_minutes": 30,
    }
    result, _ = call(args, transport=transport, now=NOW)
    frozen = result["continuation"]["next"]["query"]
    assert set(frozen) == FROZEN_FIELDS
    assert frozen["from"] == {"form": "coordinate", "value": "55.858315,-4.258436"}
    assert frozen["to"] == {"form": "coordinate", "value": "55.458267,-4.625827"}
    assert frozen["control"] == "depart_after"
    assert frozen["time"] == "2026-10-28T06:30:00Z"
    assert frozen["timezone"] is None
    assert frozen["max_transfers"] == 5
    assert frozen["num_itineraries"] == 2
    assert frozen["max_itineraries"] is None
    assert frozen["max_results"] is None
    assert frozen["search_window_seconds"] == 900
    assert frozen["max_access_walk_minutes"] == 30
    assert frozen["max_egress_walk_minutes"] == 15
    assert frozen["min_transfer_minutes"] == 0
    assert frozen["transit_modes"] == ["RAIL"]
    assert frozen["include_direct"] is True
    # presentation-only values are excluded from the frozen query
    for absent in ("warnings", "no_routes_reason", "constraints_active", "time_frame", "query"):
        assert absent not in frozen


def test_continuation_carries_both_directions_with_their_own_query():
    transport = plan_transport("plan_ayr_ncl_coord.json")
    result, _ = call(AYR_NCL_COORD, transport=transport)
    continuation = result["continuation"]
    assert set(continuation) == {"previous", "next"}
    assert continuation["previous"]["cursor"] == "EARLIER|1793091600"
    assert continuation["next"]["cursor"] == "LATER|1793098920"
    assert continuation["previous"]["query"] == continuation["next"]["query"]
    request, plan_time = plan_request_and_time()
    assert continuation["next"]["query"] == plan.frozen_query(request, plan_time)


def plan_request_and_time():
    request = plan.parse_request(dict(AYR_NCL_COORD))
    plan_time = plan.resolve_time(request)
    return request, plan_time


def test_missing_cursors_report_null_directions():
    result, _ = call(AYR_NCL_COORD, body=minimal_body())
    assert result["continuation"] == {"previous": None, "next": None}


def _page(name):
    return fixture_support.load("synthetic_continuation_pages.json")[name]


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


def _page2_with_three_itineraries():
    """page2 of the synthetic pair plus one shifted itinerary (3 vs page1's 2).

    Labelled in-memory extension: the retained corpus gives adjacent pages equal
    counts, and the acceptance requirement is that a page reports *its own*
    count, so this makes the two pages genuinely different.
    """
    page = json.loads(json.dumps(_page("page2")))
    page["itineraries"].append(_shift_itinerary(page["itineraries"][0], 1800))
    page["itineraries"][-1]["id"] = "synthetic-return-itinerary-3"
    return page


def _first_page_call():
    transport = Transport().add("/api/v6/plan", _page("page1"))
    args = {
        "from_lat": 55.858315,
        "from_lon": -4.258436,
        "to_lat": 55.458267,
        "to_lon": -4.625827,
        "num_itineraries": 2,
        "transit_modes": ["RAIL"],
    }
    result, _ = call(args, transport=transport, now=NOW)
    assert result["outcome"] == "ok", result
    return result, transport


def test_continuation_only_replay_preserves_the_frozen_instant_and_window():
    first, first_transport = _first_page_call()
    assert first["time_frame"]["form"] == "omitted"
    assert one_param(first_transport.plan_urls[0], "time") == "2026-10-28T06:30:00Z"
    frozen = first["continuation"]["next"]["query"]

    second_transport = Transport().add("/api/v6/plan", _page2_with_three_itineraries())
    second = plan.plan(
        {"continuation": first["continuation"]["next"]},
        transport=second_transport,
        now=LATER_NOW,
    )
    assert second["outcome"] == "ok", second
    url = second_transport.plan_urls[0]
    # the frozen instant, not the replay call's clock
    assert one_param(url, "time") == frozen["time"]
    assert one_param(url, "searchWindow") == str(frozen["search_window_seconds"])
    assert one_param(url, "numItineraries") == str(frozen["num_itineraries"])
    assert one_param(url, "maxTransfers") == str(frozen["max_transfers"])
    assert one_param(url, "transitModes") == "RAIL"
    assert one_param(url, "fromPlace") == "55.858315,-4.258436"
    # the next page's own cursor is what was sent
    assert one_param(url, "pageCursor") == "LATER|1793098920"


def test_continuation_page_reports_its_own_counts():
    first, _ = _first_page_call()
    assert first["query"]["upstream_count"] == 2
    second_transport = Transport().add("/api/v6/plan", _page2_with_three_itineraries())
    second = plan.plan({"continuation": first["continuation"]["next"]}, transport=second_transport)
    assert second["query"]["upstream_count"] == 3
    assert second["query"]["returned_count"] == 3
    assert len(second["itineraries"]) == 3
    assert second["query"]["minimum_requested"] == 2
    assert [item["itinerary_id"] for item in second["itineraries"]] == [
        "synthetic-return-itinerary-2",
        "synthetic-return-itinerary-0",
        "synthetic-return-itinerary-3",
    ]


def test_continuation_replay_is_idempotent_in_its_frozen_query():
    first, _ = _first_page_call()
    second_transport = Transport().add("/api/v6/plan", _page2_with_three_itineraries())
    second = plan.plan({"continuation": first["continuation"]["next"]}, transport=second_transport)
    assert second["continuation"]["next"]["query"] == first["continuation"]["next"]["query"]
    assert second["continuation"]["previous"]["query"] == first["continuation"]["next"]["query"]
    assert second["continuation"]["next"]["cursor"] == "LATER|1793106000"
    assert second["continuation"]["previous"]["cursor"] == "EARLIER|1793098920"


def test_a_previous_cursor_follows_the_other_direction():
    first, _ = _first_page_call()
    transport = Transport().add("/api/v6/plan", _page("page2"))
    result = plan.plan({"continuation": first["continuation"]["previous"]}, transport=transport)
    assert result["outcome"] == "ok", result
    assert one_param(transport.plan_urls[0], "pageCursor") == first["continuation"]["previous"]["cursor"]


def test_a_replay_chain_keeps_the_original_window():
    first, _ = _first_page_call()
    second_transport = Transport().add("/api/v6/plan", _page2_with_three_itineraries())
    second = plan.plan({"continuation": first["continuation"]["next"]}, transport=second_transport)
    third_transport = Transport().add("/api/v6/plan", _page2_with_three_itineraries())
    third = plan.plan({"continuation": second["continuation"]["next"]}, transport=third_transport)
    assert one_param(third_transport.plan_urls[0], "pageCursor") == "LATER|1793106000"
    assert one_param(third_transport.plan_urls[0], "time") == "2026-10-28T06:30:00Z"


@pytest.mark.parametrize(
    "override,field",
    [
        ({"time": "2026-10-28T07:30:00Z"}, "time"),
        ({"max_transfers": 2}, "max_transfers"),
        ({"num_itineraries": 5}, "num_itineraries"),
        ({"search_window_seconds": 1800}, "search_window_seconds"),
        ({"transit_modes": ["RAIL", "BUS"]}, "transit_modes"),
        ({"transit_modes": ["BUS", "RAIL"]}, "transit_modes"),
        ({"include_direct": False}, "include_direct"),
        ({"max_access_walk_minutes": 5}, "max_access_walk_minutes"),
        ({"max_results": 1}, "max_results"),
        ({"arrive_by": True}, "control"),
        ({"timezone": "Europe/Berlin"}, "timezone"),
        ({"origin_timezone": "Europe/Berlin"}, "origin_timezone"),
        ({"to_lat": 55.4, "to_lon": -4.6}, "to"),
    ],
)
def test_a_contradicting_argument_names_its_field(override, field):
    first, _ = _first_page_call()
    transport = Transport().add("/api/v6/plan", _page("page2"))
    args = dict(override)
    args["continuation"] = first["continuation"]["next"]
    result = plan.plan(args, transport=transport)
    details = assert_error(result, errors.CURSOR_WITHOUT_IDENTICAL_QUERY)
    assert details["fields"] == [field]
    assert details["mismatches"][0]["field"] == field
    assert transport.urls == []


def test_every_differing_field_is_named():
    first, _ = _first_page_call()
    transport = Transport().add("/api/v6/plan", _page("page2"))
    result = plan.plan(
        {
            "continuation": first["continuation"]["next"],
            "max_transfers": 1,
            "search_window_seconds": 3600,
        },
        transport=transport,
    )
    details = assert_error(result, errors.CURSOR_WITHOUT_IDENTICAL_QUERY)
    assert details["fields"] == ["max_transfers", "search_window_seconds"]
    assert transport.urls == []


def test_a_naive_time_override_is_rejected_with_the_remedy():
    first, _ = _first_page_call()
    transport = Transport().add("/api/v6/plan", _page("page2"))
    result = plan.plan(
        {
            "continuation": first["continuation"]["next"],
            "time": "2026-10-28T07:30:00",
        },
        transport=transport,
    )
    details = assert_error(result, errors.CURSOR_WITHOUT_IDENTICAL_QUERY)
    assert details["fields"] == ["time"]
    assert "explicit instant" in details["mismatches"][0]["remedy"]
    assert transport.urls == []


def test_a_date_only_time_override_is_rejected():
    first, _ = _first_page_call()
    transport = Transport().add("/api/v6/plan", _page("page2"))
    result = plan.plan(
        {"continuation": first["continuation"]["next"], "time": "2026-10-28"},
        transport=transport,
    )
    details = assert_error(result, errors.CURSOR_WITHOUT_IDENTICAL_QUERY)
    assert details["fields"] == ["time"]
    assert transport.urls == []


def test_an_equivalent_instant_with_a_different_offset_is_accepted():
    first, _ = _first_page_call()
    transport = Transport().add("/api/v6/plan", _page2_with_three_itineraries())
    # 2026-10-28T06:30:00Z == 2026-10-28T07:30:00+01:00
    result = plan.plan(
        {
            "continuation": first["continuation"]["next"],
            "time": "2026-10-28T07:30:00+01:00",
        },
        transport=transport,
    )
    assert result["outcome"] == "ok", result
    assert one_param(transport.plan_urls[0], "time") == "2026-10-28T06:30:00Z"


def test_an_identical_restatement_is_accepted():
    first, _ = _first_page_call()
    frozen = first["continuation"]["next"]["query"]
    transport = Transport().add("/api/v6/plan", _page2_with_three_itineraries())
    result = plan.plan(
        {
            "continuation": first["continuation"]["next"],
            "from_lat": 55.858315,
            "from_lon": -4.258436,
            "to_lat": 55.458267,
            "to_lon": -4.625827,
            "time": frozen["time"],
            "num_itineraries": frozen["num_itineraries"],
            "max_transfers": frozen["max_transfers"],
            "transit_modes": ["RAIL"],
        },
        transport=transport,
    )
    assert result["outcome"] == "ok", result
    assert one_param(transport.plan_urls[0], "fromPlace") == "55.858315,-4.258436"


def test_null_arguments_do_not_override_the_frozen_query():
    first, _ = _first_page_call()
    transport = Transport().add("/api/v6/plan", _page2_with_three_itineraries())
    result = plan.plan(
        {
            "continuation": first["continuation"]["next"],
            "time": None,
            "max_transfers": None,
            "timezone": None,
        },
        transport=transport,
    )
    assert result["outcome"] == "ok", result


def test_a_stop_id_endpoint_restated_as_coordinates_names_from():
    first = plan.plan(
        dict(AYR_NCL_STOPIDS, time="2026-10-27T09:00:00Z"),
        transport=plan_transport("plan_ayr_ncl_stopids_n3.json"),
    )
    transport = Transport().add("/api/v6/plan", minimal_body())
    result = plan.plan(
        {
            "continuation": first["continuation"]["next"],
            "from_lat": 55.458267,
            "from_lon": -4.625827,
        },
        transport=transport,
    )
    details = assert_error(result, errors.CURSOR_WITHOUT_IDENTICAL_QUERY)
    assert details["fields"] == ["from"]
    assert transport.urls == []


# ── continuation bounds ────────────────────────────────────────────


def test_continuation_must_be_an_object():
    details = assert_error(
        plan.plan({"continuation": "LATER|1"}, transport=Transport()), errors.VALUE_OUT_OF_RANGE
    )
    assert details["field"] == "continuation"


def test_continuation_has_a_closed_member_set():
    transport = Transport()
    result = plan.plan(
        {"continuation": {"cursor": "LATER|1", "query": {}, "page_cursor": "x"}},
        transport=transport,
    )
    details = assert_error(result, errors.VALUE_OUT_OF_RANGE)
    assert details["field"] == "continuation"
    assert details["unknown_members"] == ["page_cursor"]
    assert transport.urls == []


def test_a_cursor_over_16384_bytes_is_rejected_before_io():
    transport = Transport()
    result = plan.plan(
        {"continuation": {"cursor": "L" * 16385, "query": {"from": {}}}}, transport=transport
    )
    details = assert_error(result, errors.VALUE_OUT_OF_RANGE)
    assert details["field"] == "continuation.cursor"
    assert details["max_bytes"] == plan.MAX_CURSOR_BYTES
    assert transport.urls == []


def test_a_cursor_at_the_limit_is_not_rejected_for_size():
    transport = Transport().add("/api/v6/plan", minimal_body())
    cursor = "L" * plan.MAX_CURSOR_BYTES
    result = plan.plan(
        {
            "continuation": {
                "cursor": cursor,
                "query": plan.frozen_query(*plan_request_and_time()),
            }
        },
        transport=transport,
    )
    # the query is valid, so this call reaches the upstream rather than the size gate
    assert result["outcome"] == "ok", result
    assert one_param(transport.plan_urls[0], "pageCursor") == cursor


def test_a_continuation_over_32768_bytes_is_rejected_before_io():
    transport = Transport()
    continuation = {
        "cursor": "L" * 16000,
        "query": plan.frozen_query(*plan_request_and_time()),
    }
    continuation["query"]["timezone"] = "Europe/" + "x" * 18000
    result = plan.plan({"continuation": continuation}, transport=transport)
    details = assert_error(result, errors.VALUE_OUT_OF_RANGE)
    assert details["field"] == "continuation"
    assert details["max_bytes"] == plan.MAX_CONTINUATION_BYTES
    assert transport.urls == []


@pytest.mark.parametrize(
    "mutate,reason,field",
    [
        (lambda q: q.pop("max_results"), errors.VALUE_OUT_OF_RANGE, "continuation.query"),
        (lambda q: q.update(extra=1), errors.VALUE_OUT_OF_RANGE, "continuation.query"),
        (lambda q: q.update(control="depart"), errors.VALUE_OUT_OF_RANGE, "continuation.query.control"),
        (lambda q: q.update(max_transfers=None), errors.VALUE_OUT_OF_RANGE, "continuation.query.max_transfers"),
        (lambda q: q.update(time="2026-10-27T09:00:00"), errors.VALUE_OUT_OF_RANGE, "continuation.query.time"),
        (lambda q: q.update(time="not-a-time"), errors.VALUE_OUT_OF_RANGE, "continuation.query.time"),
        (lambda q: q.update(max_transfers=99), errors.VALUE_OUT_OF_RANGE, "continuation.query.max_transfers"),
        (lambda q: q.update(max_transfers="5"), errors.VALUE_OUT_OF_RANGE, "continuation.query.max_transfers"),
        (lambda q: q.update(transit_modes=["WALK"]), errors.STREET_MODE_NOT_TRANSIT, "continuation.query.transit_modes"),
        (lambda q: q.update(transit_modes=["NOPE"]), errors.UNKNOWN_MODE, "continuation.query.transit_modes"),
        (lambda q: q.update(transit_modes=[]), errors.VALUE_OUT_OF_RANGE, "continuation.query.transit_modes"),
        (lambda q: q.update(include_direct="yes"), errors.VALUE_OUT_OF_RANGE, "continuation.query.include_direct"),
        (lambda q: _put(q, "from", {"form": "coordinate", "value": "999,0"}), errors.VALUE_OUT_OF_RANGE, "continuation.query.from"),
        (lambda q: _put(q, "from", {"form": "stop_id", "value": ""}), errors.VALUE_OUT_OF_RANGE, "continuation.query.from"),
        (lambda q: _put(q, "from", {"form": "place", "value": "x"}), errors.VALUE_OUT_OF_RANGE, "continuation.query.from"),
        (lambda q: _put(q, "to", {"form": "coordinate"}), errors.VALUE_OUT_OF_RANGE, "continuation.query.to"),
        (lambda q: q.update(timezone="Nowhere/Land"), errors.VALUE_OUT_OF_RANGE, "continuation.query.timezone"),
    ],
)
def test_the_embedded_query_is_validated_like_an_initial_call(mutate, reason, field):
    query = plan.frozen_query(*plan_request_and_time())
    mutate(query)
    transport = Transport()
    result = plan.plan({"continuation": {"cursor": "LATER|1", "query": query}}, transport=transport)
    details = assert_error(result, reason)
    assert details["field"] == field
    assert transport.urls == []


def test_page_cursor_is_not_an_acceptable_argument():
    transport = Transport()
    result = plan.plan(dict(AYR_NCL_COORD, page_cursor="LATER|1"), transport=transport)
    details = assert_error(result, errors.VALUE_OUT_OF_RANGE)
    assert details["argument"] == "page_cursor"
    assert "continuation" in details["remedy"]
    assert transport.urls == []


# ── §4.2 no-routes predicate ───────────────────────────────────────


EMPTY_ARGS = {
    "from_stop_id": "gb-great-britain_910GAYRR",
    "to_stop_id": "gb-great-britain_910GNWCSTLE",
    "time": "2027-07-15T09:00:00Z",
    "num_itineraries": 3,
    "transit_modes": ["RAIL"],
}


def test_a_structurally_valid_empty_result_is_no_routes():
    transport = plan_transport("plan_empty_no_service.json")
    result, _ = call(EMPTY_ARGS, transport=transport)
    assert result["outcome"] == "no_routes"
    assert result["no_routes_reason"] == "not_determined"
    assert result["itineraries"] == []
    # RAIL is narrower than the default TRANSIT, so it is an active condition
    assert result["constraints_active"] == [{"name": "transit_modes", "value": ["RAIL"]}]
    assert result["continuation"]["next"]["cursor"] == "LATER|1815642000"
    assert result["query"]["upstream_count"] == 0
    assert result["query"]["returned_count"] == 0
    assert result["query"]["truncated"] is False
    assert result["from"]["name"] == "Ayr"
    assert result["to"]["name"] == "Newcastle"


def test_no_constraints_supplied_means_an_empty_constraints_array():
    args = {
        "from_stop_id": "gb-great-britain_910GAYRR",
        "to_stop_id": "gb-great-britain_910GNWCSTLE",
        "time": "2026-10-27T09:00:00Z",
    }
    result, _ = call(args, body=fixture_support.load("plan_empty_no_service.json"))
    assert result["outcome"] == "no_routes"
    assert result["constraints_active"] == []


def test_no_routes_without_a_cause_never_claims_the_constraint_caused_it():
    args = dict(EMPTY_ARGS, max_transfers=0)
    transport = plan_transport("plan_empty_no_service.json")
    result, _ = call(args, transport=transport)
    assert result["outcome"] == "no_routes"
    assert result["no_routes_reason"] == "not_determined"
    assert {"name": "max_transfers", "value": 0} in result["constraints_active"]
    # no relaxed control request is ever issued to manufacture a cause
    assert len(transport.plan_urls) == 1


def test_zero_endpoint_walk_caps_are_echoed_as_active_constraints():
    args = {
        "from_lat": 55.458267,
        "from_lon": -4.625827,
        "to_lat": 54.968338,
        "to_lon": -1.616046,
        "time": "2026-10-27T09:00:00Z",
        "num_itineraries": 3,
        "transit_modes": ["RAIL"],
        "max_access_walk_minutes": 0,
        "max_egress_walk_minutes": 0,
    }
    transport = plan_transport("plan_empty_constraints.json")
    result, _ = call(args, transport=transport)
    assert result["outcome"] == "no_routes"
    assert result["no_routes_reason"] == "not_determined"
    assert result["constraints_active"] == [
        {"name": "max_access_walk_minutes", "value": 0},
        {"name": "max_egress_walk_minutes", "value": 0},
        {"name": "transit_modes", "value": ["RAIL"]},
    ]


def test_street_only_direct_alternatives_are_reported_not_hidden():
    args = {
        "from_lat": 55.458267,
        "from_lon": -4.625827,
        "to_lat": 54.968338,
        "to_lon": -1.616046,
        "time": "2026-10-27T09:00:00Z",
        "num_itineraries": 3,
        "max_transfers": 0,
        "transit_modes": ["RAIL"],
    }
    transport = plan_transport("synthetic_plan_direct_alternatives.json")
    result, _ = call(args, transport=transport)
    assert result["outcome"] == "no_routes"
    assert result["no_routes_reason"] == "street_only_alternatives_returned"
    assert len(result["direct"]) == 1
    direct = result["direct"][0]
    assert direct["duration_basis"] == "street_only"
    assert direct["elapsed_seconds"] == 12600
    assert direct["transit_leg_count"] == 0
    assert direct["legs"][0]["duration_seconds"] == 12600
    assert direct["legs"][0]["mode"] == "WALK"
    assert result["query"]["direct_count"] == 1
    assert "direct_alternatives_only" in [w["code"] for w in result["warnings"]]


def test_include_direct_false_withholds_the_direct_array():
    args = {
        "from_lat": 55.458267,
        "from_lon": -4.625827,
        "to_lat": 54.968338,
        "to_lon": -1.616046,
        "time": "2026-10-27T09:00:00Z",
        "max_transfers": 0,
        "transit_modes": ["RAIL"],
        "include_direct": False,
    }
    transport = plan_transport("synthetic_plan_direct_alternatives.json")
    result, _ = call(args, transport=transport)
    assert "direct" not in result
    assert result["query"]["direct_count"] == 0
    assert result["no_routes_reason"] == "street_only_alternatives_returned"


@pytest.mark.parametrize(
    "mutation,element",
    [
        (lambda body: body.pop("requestParameters"), "requestParameters"),
        (lambda body: body.pop("debugOutput"), "debugOutput"),
        (lambda body: body.pop("nextPageCursor"), "nextPageCursor"),
        (lambda body: body.pop("previousPageCursor"), "previousPageCursor"),
        (lambda body: body.update(direct={}), "direct"),
        (lambda body: body.update(itineraries={}), "itineraries"),
        (lambda body: body.update(**{"from": {"lat": 1.0, "lon": 2.0}}), "from"),
        (lambda body: body.update(**{"from": {"name": "", "lat": 1.0, "lon": 2.0}}), "from"),
        (lambda body: body.update(to=[]), "to"),
    ],
)
def test_an_empty_result_that_fails_the_predicate_is_malformed(mutation, element):
    body = fixture_support.load("plan_empty_no_service.json")
    mutation(body)
    transport = Transport().add("/api/v6/plan", body)
    result = plan.plan(EMPTY_ARGS, transport=transport)
    details = assert_error(result, errors.UNEXPECTED_SHAPE)
    assert element in details["failed"]
    assert result["outcome"] == errors.MALFORMED_UPSTREAM


def test_the_empty_result_requires_http_200_not_any_2xx():
    body = fixture_support.load("plan_empty_no_service.json")
    transport = Transport().add("/api/v6/plan", body, 201)
    result = plan.plan(EMPTY_ARGS, transport=transport)
    details = assert_error(result, errors.UNEXPECTED_SHAPE)
    assert details["status"] == 201
    assert details["failed"] == ["status_200"]


def test_a_non_empty_result_is_ok_not_no_routes():
    transport = plan_transport("plan_ayr_ncl_coord.json")
    result, _ = call(AYR_NCL_COORD, transport=transport)
    assert result["outcome"] == "ok"
    assert "no_routes_reason" not in result


# ── §4.2 transport and status classification at the plan boundary ──


def test_window_error_is_out_of_range_with_the_window():
    recorded = fixture_support.load("window_error.json")
    entry = recorded["entries"][0]
    assert entry["status"] == 400
    transport = Transport().add("/api/v6/plan", entry["body"].encode(), 400)
    result = plan.plan(EMPTY_ARGS, transport=transport)
    details = assert_error(result, errors.TIMETABLE_WINDOW)
    assert details["window"] == {"start": "2026-09-20 00:00", "end": "2027-10-21 00:00"}
    assert result["outcome"] == errors.OUT_OF_RANGE


def test_unmatched_400_is_upstream_rejected():
    transport = Transport().add("/api/v6/plan", b"bad request text", 400)
    result = plan.plan(EMPTY_ARGS, transport=transport)
    assert_error(result, errors.BAD_REQUEST)


def test_server_error_is_upstream_rejected():
    transport = Transport().add("/api/v6/plan", b"oops", 500)
    result = plan.plan(EMPTY_ARGS, transport=transport)
    assert_error(result, errors.SERVER_ERROR)


def test_transport_failure_is_a_transport_error():
    def broken(request, timeout):
        raise OSError("connection reset")

    result = plan.plan(EMPTY_ARGS, transport=broken)
    assert_error(result, errors.CONNECTION_FAILURE)


def test_non_json_body_is_malformed_upstream():
    transport = Transport().add("/api/v6/plan", b"<html>nope</html>")
    result = plan.plan(EMPTY_ARGS, transport=transport)
    assert_error(result, errors.NON_JSON_BODY)


def test_a_json_array_is_not_a_plan_body():
    transport = Transport().add("/api/v6/plan", b"[]")
    result = plan.plan(EMPTY_ARGS, transport=transport)
    details = assert_error(result, errors.UNEXPECTED_SHAPE)
    assert details["received"] == "list"


def test_non_mapping_arguments_are_rejected():
    details = assert_error(plan.plan(["from_lat"]), errors.VALUE_OUT_OF_RANGE)
    assert details["argument"] == "args"


# ── §2.4 diagnostic locators: scope, option and leg (R1–R3) ─────────
#
# `error_details` is the caller's only pointer into a large returned body, so a
# leg-level `malformed_upstream` rejection must name the object that failed with
# the same locator the accepted model uses: `itinerary` for `itineraries[n]`
# (§4.5.3 numbers `option` from 1) or `direct` for `direct[n]`, plus the 0-based
# `leg`. Every case below uses a *second* option and, except where the fault is a
# leg container itself, a *second* leg, so a hardcoded option zero, an
# `itinerary` key on a `direct` object, or a detail with no leg at all fails.
#
# R1: `scheduledStartTime`/`scheduledEndTime` are read by the leg mapper and not
# by leg validation, so this is the site that used to report `itinerary: 0`.
# R2: leg validation and the itinerary-level instant parser used to write the
# scope key `itinerary` even when mapping a `direct` alternative.
# R3: an `intermediateStops` fault carried neither scope nor option nor leg.

DIAG_START = "2026-10-27T09:00:00Z"
DIAG_END = "2026-10-27T10:00:00Z"


def diag_leg(**fields):
    leg = {
        "mode": "RAIL",
        "from": {"name": "A", "tz": "Europe/London"},
        "to": {"name": "B", "tz": "Europe/London"},
        "startTime": DIAG_START,
        "endTime": "2026-10-27T09:30:00Z",
        "duration": 1800,
        "legGeometry": {"points": "abc"},
    }
    leg.update(fields)
    return leg


def diag_second_leg(**fields):
    return diag_leg(
        **{
            "from": {"name": "B", "tz": "Europe/London"},
            "to": {"name": "C", "tz": "Europe/London"},
            "startTime": "2026-10-27T09:30:00Z",
            "endTime": "2026-10-27T09:50:00Z",
            "duration": 1200,
            **fields,
        }
    )


def diag_itinerary(legs, *, ident="diag", **overrides):
    itinerary = {
        "duration": 3600,
        "startTime": DIAG_START,
        "endTime": DIAG_END,
        "transfers": 0,
        "id": ident,
        "legs": legs,
    }
    itinerary.update(overrides)
    return itinerary


def diag_body(bad, *, scope_key):
    """Two alternatives in ``scope_key``; the second is the malformed one."""
    good = diag_itinerary([diag_leg()], ident="good")
    if scope_key == "itinerary":
        return minimal_body(itineraries=[good, bad])
    return minimal_body(itineraries=[], direct=[good, bad])


def diag_result(body):
    return plan.plan(dict(AYR_NCL_COORD), transport=Transport().add("/api/v6/plan", body))


def assert_locator(result, scope_key, option, leg):
    """One rejection, located on the same object the details name."""
    details = assert_error(result, errors.UNEXPECTED_SHAPE)
    assert result["outcome"] == errors.MALFORMED_UPSTREAM
    assert details[scope_key] == option, details
    assert details["leg"] == leg, details
    # exactly one scope vocabulary, never both
    assert ({"itinerary", "direct"} - {scope_key}).isdisjoint(details), details
    # the message names the object the details describe
    assert f"{scope_key} {option} leg {leg}" in result["error"], result["error"]
    return details


@pytest.mark.parametrize("element", ["scheduledStartTime", "scheduledEndTime"])
@pytest.mark.parametrize("scope_key", ["itinerary", "direct"])
def test_a_malformed_scheduled_instant_names_its_scope_option_and_leg(scope_key, element):
    """R1: the leg mapper's own scheduled instants are located, not option zero."""
    body = diag_body(diag_itinerary([diag_leg(), diag_second_leg(**{element: "12345"})]),
                     scope_key=scope_key)
    result = diag_result(body)
    details = assert_locator(result, scope_key, 2, 1)
    assert details["element"] == element
    assert details["value"] == "12345"
    assert element in result["error"], result["error"]


@pytest.mark.parametrize("scope_key", ["itinerary", "direct"])
def test_a_negative_declared_duration_names_the_scope_it_was_found_in(scope_key):
    """R2: leg validation names the alternative it validated."""
    body = diag_body(diag_itinerary([diag_leg(), diag_second_leg(duration=-600)]),
                     scope_key=scope_key)
    details = assert_locator(diag_result(body), scope_key, 2, 1)
    assert details["element"] == "duration"
    assert details["value"] == "-600"


# Every `_validate_legs` failure site (validated field, container, ordering) and
# the shared instant parser it calls, in one table: each must carry the caller's
# scope key. The `element` column is None where the rejection has no field.
def _leg_not_an_object():
    return "not-a-leg"


def _leg_mode_missing():
    return diag_leg(mode="")


def _leg_from_missing():
    leg = diag_leg()
    leg["from"] = "nope"
    return leg


def _leg_ends_before_it_starts():
    return diag_second_leg(startTime="2026-10-27T09:50:00Z", endTime="2026-10-27T09:30:00Z")


def _leg_duration_not_a_number():
    return diag_second_leg(duration="1200")


def _leg_duration_negative():
    return diag_second_leg(duration=-0.5)


def _leg_start_unparseable():
    return diag_second_leg(startTime="not-a-time")


def _leg_starts_before_the_previous():
    return diag_second_leg(startTime="2026-10-27T08:50:00Z",
                           endTime="2026-10-27T09:10:00Z")


@pytest.mark.parametrize(
    "second_leg,element",
    [
        (_leg_not_an_object, None),
        (_leg_mode_missing, "mode"),
        (_leg_from_missing, "from"),
        (_leg_ends_before_it_starts, "endTime"),
        (_leg_duration_not_a_number, "duration"),
        (_leg_duration_negative, "duration"),
        (_leg_start_unparseable, "startTime"),
        (_leg_starts_before_the_previous, "startTime"),
    ],
    ids=lambda value: getattr(value, "__name__", value),
)
def test_every_leg_validation_failure_carries_the_caller_scope(second_leg, element):
    """R2: the same fault class is located consistently in a `direct` alternative."""
    body = diag_body(diag_itinerary([diag_leg(), second_leg()]), scope_key="direct")
    details = assert_locator(diag_result(body), "direct", 2, 1)
    if element is None:
        assert "element" not in details, details
    else:
        assert details["element"] == element, details


@pytest.mark.parametrize("element", ["startTime", "endTime"])
def test_a_malformed_itinerary_instant_uses_the_direct_locator(element):
    """R2, sibling parser: the itinerary-level instant parser is scope-aware too."""
    bad = diag_itinerary([diag_leg()], **{element: "nope"})
    result = diag_result(diag_body(bad, scope_key="direct"))
    details = assert_error(result, errors.UNEXPECTED_SHAPE)
    assert details["direct"] == 2, details
    assert "itinerary" not in details, details
    assert details["element"] == element
    assert details["value"] == "nope"
    assert f"direct 2 {element}" in result["error"], result["error"]


@pytest.mark.parametrize(
    "stops,received,stop_index",
    [({"a": 1}, "dict", None), ([{"name": "ok"}, 7], "int", 1)],
    ids=["container", "entry"],
)
@pytest.mark.parametrize("scope_key", ["itinerary", "direct"])
def test_malformed_intermediate_stops_names_its_scope_option_and_leg(
    scope_key, stops, received, stop_index
):
    """R3: both malformed shapes are located on the leg that carries them."""
    body = diag_body(
        diag_itinerary([diag_leg(), diag_second_leg(intermediateStops=stops)]),
        scope_key=scope_key,
    )
    result = diag_result(body)
    details = assert_locator(result, scope_key, 2, 1)
    assert details["element"] == "intermediateStops"
    assert details["received"] == received
    if stop_index is None:
        assert "stop" not in details, details
    else:
        assert details["stop"] == stop_index, details
        assert f"intermediateStops entry {stop_index}" in result["error"], result["error"]
