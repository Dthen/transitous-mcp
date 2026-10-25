"""Reachable and both one-to-many paths (§4.12, §4.3, §4.13).

The oracles this file exists for:

* zero and fractional street durations are real numbers, and a missing array is
  malformed — not ``no_routes``;
* a many-to-one call labels ``one_role: "destination"`` and reads a naive clock in
  that endpoint's zone;
* a Pareto entry missing ``duration`` or ``transfers`` is malformed, never a
  fabricated zero;
* street array lengths match the requested destination count exactly, with every
  index preserved and no padding or dropping;
* a street call has no clock and therefore no ``time_frame``.
"""

import copy
import json
import math
import os
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest

import fixture_support as fs  # noqa: F401  (puts the repository root on sys.path)

from transit_core import errors, onetomany

DESTINATIONS = [[55.858315, -4.258436], [55.9527965, -3.1877235], [55.5016, -4.6142]]
FROM = (55.458267, -4.625827)


# ── transport double ────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, body, status=200):
        self.status = status
        self._body = body

    def read(self):
        if isinstance(self._body, (dict, list)):
            return json.dumps(self._body).encode("utf-8")
        if isinstance(self._body, bytes):
            return self._body
        return str(self._body).encode("utf-8")

    def close(self):
        pass


class FixtureTransport:
    def __init__(self, routes):
        self.routes = dict(routes)
        self.urls = []

    def __call__(self, request, timeout):
        url = request.full_url
        self.urls.append(url)
        path = urlsplit(url).path
        if path not in self.routes:
            raise AssertionError(f"unexpected request: {url}")
        body = self.routes[path]
        if callable(body):
            body = body(url)
        return FakeResponse(body)

    def query(self, index=-1):
        return parse_qs(urlsplit(self.urls[index]).query)

    def paths(self):
        return [urlsplit(url).path for url in self.urls]


def june(body):
    return copy.deepcopy(body)


def assert_error(result, error_type, outcome=None):
    assert isinstance(result, dict), result
    assert result.get("error"), result
    assert result["error_type"] == error_type, result
    assert result["outcome"] == (outcome or errors.REASON_OUTCOME[error_type]), result
    assert isinstance(result["error_details"], dict)


def street_transport(body=None):
    return FixtureTransport({"/api/v1/one-to-many": body or fs.load("synthetic_one_to_many_street.json")})


def intermodal_body():
    return fs.load("synthetic_one_to_many_transit.json")


def intermodal_transport(body=None):
    return FixtureTransport(
        {"/api/experimental/one-to-many-intermodal": body if body is not None else intermodal_body()["valid"]}
    )


DISCOVERY = [{
    "name": "Ayr", "id": "gb-great-britain_910GAYRR",
    "lat": 55.458267, "lon": -4.625827, "tz": "Europe/London",
}]


# ── module identity ─────────────────────────────────────────────────


def test_onetomany_module_lives_in_the_checkout():
    path = os.path.realpath(onetomany.__file__)
    assert path.startswith(os.path.realpath(fs.REPO_ROOT) + os.sep)


# ── transit_one_to_many (street) ────────────────────────────────────


def test_street_preserves_fractional_zero_and_unavailable_entries_by_index():
    transport = street_transport()

    result = onetomany.one_to_many(*FROM, DESTINATIONS, transport=transport)

    assert result["outcome"] == errors.OK
    results = result["results"]
    assert len(results) == 3
    assert results[0] == {
        "lat": 55.858315, "lon": -4.258436,
        "duration_seconds": 423.75, "distance_meters": 531.2,
    }
    assert results[1]["duration_seconds"] == 0        # zero is available, not missing
    assert results[1]["distance_meters"] == 0.0
    assert results[2]["duration_seconds"] is None     # empty Duration, never zero
    assert results[2]["distance_meters"] is None
    assert result["query"]["one_role"] == "origin"
    assert "time_frame" not in result


def test_street_request_keeps_one_semicolon_and_many_comma_form():
    transport = street_transport()

    onetomany.one_to_many(*FROM, DESTINATIONS[:2], mode="BIKE", max_sec=1800, transport=transport)

    query = transport.query()
    assert query["one"] == ["55.458267;-4.625827"]
    assert query["many"] == ["55.858315;-4.258436,55.9527965;-3.1877235"]
    assert query["mode"] == ["BIKE"]
    assert query["max"] == ["1800"]
    assert query["maxMatchingDistance"] == ["25"]
    assert query["arriveBy"] == ["false"]
    assert transport.paths() == ["/api/v1/one-to-many"]
    assert len([key for key in query if key == "many"]) == 1


def test_street_fractional_max_seconds_is_preserved():
    transport = street_transport()

    result = onetomany.one_to_many(*FROM, DESTINATIONS, max_sec=1800.5, transport=transport)

    assert result["outcome"] == errors.OK
    assert transport.query()["max"] == ["1800.5"]
    assert result["query"]["max_sec"] == 1800.5


def test_street_many_to_one_labels_the_one_endpoint_as_destination():
    transport = street_transport()

    result = onetomany.one_to_many(*FROM, DESTINATIONS, arrive_by=True, transport=transport)

    assert transport.query()["arriveBy"] == ["true"]
    assert result["query"]["one_role"] == "destination"
    assert result["query"]["arrive_by"] is True


def test_street_missing_array_is_malformed_not_no_routes():
    transport = street_transport({"duration": 120})

    result = onetomany.one_to_many(*FROM, DESTINATIONS, transport=transport)

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


def test_street_length_mismatch_is_malformed_and_names_the_counts():
    transport = street_transport([{"duration": 120}, {"duration": 240}])

    result = onetomany.one_to_many(*FROM, DESTINATIONS, transport=transport)

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)
    assert result["error_details"]["expected"] == 3
    assert result["error_details"]["actual"] == 2


def test_street_all_unavailable_is_no_routes_with_every_index_kept():
    transport = street_transport([{}, {"distance": 12.0}, {"duration": None}])

    result = onetomany.one_to_many(*FROM, DESTINATIONS, transport=transport)

    assert result["outcome"] == errors.NO_ROUTES
    assert len(result["results"]) == 3
    assert [row["duration_seconds"] for row in result["results"]] == [None, None, None]
    assert "error" not in result


def test_street_mixed_availability_stays_ok():
    transport = street_transport([{}, {"duration": 12.5}, {}])

    result = onetomany.one_to_many(*FROM, DESTINATIONS, transport=transport)

    assert result["outcome"] == errors.OK
    assert result["results"][1]["duration_seconds"] == 12.5


@pytest.mark.parametrize(
    "entry",
    [
        {"duration": True},
        {"duration": "120"},
        {"distance": math.inf},
        {"duration": -1},
    ],
)
def test_street_malformed_duration_members_are_shape_failures(entry):
    transport = street_transport([entry, {}, {}])

    result = onetomany.one_to_many(*FROM, DESTINATIONS, transport=transport)

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


def test_street_unknown_mode_is_rejected_before_any_request():
    transport = FixtureTransport({})
    for bad in ("TRANSIT", "walk", 3, None):
        result = onetomany.one_to_many(*FROM, DESTINATIONS, mode=bad, transport=transport)
        assert_error(result, errors.UNKNOWN_MODE, errors.INVALID_INPUT)
    assert transport.urls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_sec": -1},
        {"max_sec": True},
        {"max_sec": math.inf},
        {"arrive_by": "false"},
        {"destinations": []},
        {"destinations": [[55.8, -4.2]] * 51},
        {"destinations": [[55.8]]},
        {"destinations": [[55.8, -4.2, 1]]},
        {"destinations": [["55.8", -4.2]]},
        {"destinations": [[91, -4.2]]},
        {"destinations": [True]},
        {"from_lat": 91},
        {"from_lon": -181},
    ],
)
def test_street_argument_validation_happens_before_any_request(kwargs):
    transport = FixtureTransport({})
    call = {"from_lat": FROM[0], "from_lon": FROM[1], "destinations": DESTINATIONS}
    call.update(kwargs)

    result = onetomany.one_to_many(transport=transport, **call)

    assert_error(result, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert transport.urls == []


# ── transit_one_to_many_transit (intermodal) ────────────────────────


def test_intermodal_preserves_indexed_street_and_pareto_entries():
    transport = intermodal_transport()

    result = onetomany.one_to_many_transit(
        *FROM, DESTINATIONS, time="2026-10-27T09:00:00Z", transport=transport,
    )

    assert result["outcome"] == errors.OK
    results = result["results"]
    assert len(results) == 3
    assert results[0]["street_duration_seconds"] == 300
    assert results[0]["street_distance_meters"] == 420.0
    assert results[0]["transit"] == [
        {"duration_seconds": 1800, "transfers": 0},
        {"duration_seconds": 1500, "transfers": 1},
    ]
    assert results[1]["street_duration_seconds"] is None
    assert results[1]["street_distance_meters"] is None
    assert results[1]["transit"] == []
    assert results[2]["street_duration_seconds"] == 120.5   # fractional preserved
    assert results[2]["street_distance_meters"] is None
    assert results[2]["transit"] == []
    assert result["query"]["destination_count"] == 3
    assert result["query"]["one_role"] == "origin"
    assert result["time_frame"]["control"] == "depart_after"


def test_intermodal_available_street_with_empty_transit_is_ok():
    body = {
        "street_durations": [{"duration": 300}, {}, {}],
        "transit_durations": [[], [], []],
    }
    transport = intermodal_transport(body)

    result = onetomany.one_to_many_transit(*FROM, DESTINATIONS, time="2026-10-27T09:00:00Z", transport=transport)

    assert result["outcome"] == errors.OK
    assert result["results"][0]["street_duration_seconds"] == 300
    assert result["results"][1]["street_duration_seconds"] is None


def test_intermodal_all_unavailable_is_no_routes():
    body = {"street_durations": [{}, {}, {}], "transit_durations": [[], [], []]}
    transport = intermodal_transport(body)

    result = onetomany.one_to_many_transit(*FROM, DESTINATIONS, time="2026-10-27T09:00:00Z", transport=transport)

    assert result["outcome"] == errors.NO_ROUTES
    assert len(result["results"]) == 3


def test_intermodal_length_mismatch_is_malformed():
    transport = intermodal_transport(intermodal_body()["malformed_length"])

    result = onetomany.one_to_many_transit(*FROM, DESTINATIONS, time="2026-10-27T09:00:00Z", transport=transport)

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


def test_intermodal_missing_pareto_member_is_malformed_not_zero():
    transport = intermodal_transport(intermodal_body()["missing_member"])

    result = onetomany.one_to_many_transit(*FROM, DESTINATIONS, time="2026-10-27T09:00:00Z", transport=transport)

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)
    assert "transfers" in result["error"] or result["error_details"]


@pytest.mark.parametrize(
    "entry, member",
    [
        ({"duration": 1800}, "transfers"),
        ({"transfers": 1}, "duration"),
    ],
)
def test_intermodal_missing_required_pareto_member_names_the_member(entry, member):
    body = {"street_durations": [{}, {}, {}], "transit_durations": [[entry], [], []]}
    transport = intermodal_transport(body)

    result = onetomany.one_to_many_transit(*FROM, DESTINATIONS, time="2026-10-27T09:00:00Z", transport=transport)

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)
    assert result["error_details"]["member"] == member


def test_intermodal_request_uses_the_pinned_parameter_names():
    transport = intermodal_transport()

    onetomany.one_to_many_transit(
        *FROM, DESTINATIONS[:2], time="2026-10-27T09:00:00Z", arrive_by=True,
        max_transfers=2, transit_modes=["RAIL", "BUS"],
        max_access_walk_minutes=10, max_egress_walk_minutes=5,
        max_travel_minutes=45, transport=transport,
    )

    query = transport.query()
    assert query["one"] == ["55.458267;-4.625827"]
    assert query["many"] == ["55.858315;-4.258436,55.9527965;-3.1877235"]
    assert query["time"] == ["2026-10-27T09:00:00Z"]
    assert query["maxTravelTime"] == ["45"]
    assert query["maxMatchingDistance"] == ["25"]
    assert query["maxTransfers"] == ["2"]
    assert query["transitModes"] == ["RAIL,BUS"]
    assert query["maxPreTransitTime"] == ["600"]
    assert query["maxPostTransitTime"] == ["300"]
    assert query["arriveBy"] == ["true"]
    assert transport.paths() == ["/api/experimental/one-to-many-intermodal"]


def test_intermodal_many_to_one_reads_the_naive_clock_in_the_one_endpoint_zone():
    body = intermodal_body()["valid"]
    transport = FixtureTransport(
        {"/api/v1/reverse-geocode": DISCOVERY, "/api/experimental/one-to-many-intermodal": body}
    )

    result = onetomany.one_to_many_transit(
        *FROM, DESTINATIONS, time="2026-10-27T09:00:00", arrive_by=True, transport=transport,
    )

    assert result["outcome"] == errors.OK
    assert transport.paths() == ["/api/v1/reverse-geocode", "/api/experimental/one-to-many-intermodal"]
    assert transport.query(0)["place"] == ["55.458267,-4.625827"]
    assert transport.query(1)["time"] == ["2026-10-27T09:00:00Z"]
    assert transport.query(1)["arriveBy"] == ["true"]
    frame = result["time_frame"]
    assert frame["control"] == "arrive_by"
    assert frame["frame_zone"] == "Europe/London"
    assert frame["frame_source"] == "endpoint_reverse_geocode"
    assert result["query"]["one_role"] == "destination"


def test_intermodal_caller_timezone_needs_no_discovery_request():
    transport = intermodal_transport()

    result = onetomany.one_to_many_transit(
        *FROM, DESTINATIONS, time="2026-10-27T09:00:00", timezone="Europe/Berlin",
        transport=transport,
    )

    assert result["outcome"] == errors.OK
    assert transport.paths() == ["/api/experimental/one-to-many-intermodal"]
    assert transport.query()["time"] == ["2026-10-27T08:00:00Z"]
    assert result["time_frame"]["frame_source"] == "caller_timezone"


def test_intermodal_explicit_instant_needs_no_discovery_request():
    transport = intermodal_transport()

    result = onetomany.one_to_many_transit(
        *FROM, DESTINATIONS, time="2026-10-27T09:00:00+01:00", transport=transport,
    )

    assert result["outcome"] == errors.OK
    assert transport.paths() == ["/api/experimental/one-to-many-intermodal"]
    assert transport.query()["time"] == ["2026-10-27T08:00:00Z"]
    assert result["time_frame"]["frame_source"] == "not_applicable"


@pytest.mark.parametrize(
    "body",
    [
        {"street_durations": [{}, {}, {}]},
        {"transit_durations": [[], [], []]},
        {"street_durations": {}, "transit_durations": []},
        {"street_durations": [{}, {}, {}], "transit_durations": [[], []]},
        {"street_durations": [{}, {}], "transit_durations": [[], [], []]},
        {"street_durations": [{}, "x", {}], "transit_durations": [[], [], []]},
        {"street_durations": [{}, {}, {}], "transit_durations": [[], [1], []]},
    ],
)
def test_intermodal_malformed_containers_are_shape_failures(body):
    transport = intermodal_transport(body)

    result = onetomany.one_to_many_transit(*FROM, DESTINATIONS, time="2026-10-27T09:00:00Z", transport=transport)

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


@pytest.mark.parametrize(
    "entry",
    [
        {"duration": 1800},
        {"transfers": 0},
        {"duration": "1800", "transfers": 0},
        {"duration": 1800, "transfers": "0"},
        {"duration": 1800, "transfers": -1},
        {"duration": 1800, "transfers": True},
        {"duration": 1800.5, "transfers": 1.0},
        {"duration": -1, "transfers": 0},
    ],
)
def test_intermodal_malformed_pareto_entries_are_shape_failures(entry):
    body = {"street_durations": [{}, {}, {}], "transit_durations": [[entry], [], []]}
    transport = intermodal_transport(body)

    result = onetomany.one_to_many_transit(*FROM, DESTINATIONS, time="2026-10-27T09:00:00Z", transport=transport)

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


def test_intermodal_fractional_pareto_duration_is_preserved():
    body = {"street_durations": [{}, {}, {}], "transit_durations": [[{"duration": 1800.5, "transfers": 1}], [], []]}
    transport = intermodal_transport(body)

    result = onetomany.one_to_many_transit(*FROM, DESTINATIONS, time="2026-10-27T09:00:00Z", transport=transport)

    assert result["outcome"] == errors.OK
    assert result["results"][0]["transit"] == [{"duration_seconds": 1800.5, "transfers": 1}]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_transfers": -1},
        {"max_transfers": 11},
        {"max_transfers": True},
        {"max_access_walk_minutes": -1},
        {"max_access_walk_minutes": 121},
        {"max_egress_walk_minutes": 121},
        {"max_travel_minutes": 0},
        {"max_travel_minutes": 1441},
        {"max_travel_minutes": True},
        {"arrive_by": "false"},
        {"transit_modes": []},
        {"transit_modes": "RAIL"},
        {"transit_modes": [1]},
        {"transit_modes": ["rail"]},
        {"transit_modes": ["WALK"]},
        {"transit_modes": ["DEBUG_WALK"]},
        {"time": "2026-02-30"},
        {"timezone": "Not/AZone"},
    ],
)
def test_intermodal_argument_validation_happens_before_any_request(kwargs):
    transport = FixtureTransport({})
    call = {"time": "2026-10-27T09:00:00Z"}
    call.update(kwargs)

    result = onetomany.one_to_many_transit(*FROM, DESTINATIONS, transport=transport, **call)

    assert result.get("error"), result
    assert result["outcome"] == errors.INVALID_INPUT, result
    assert transport.urls == []


def test_intermodal_transit_modes_reject_street_and_unknown_values_by_reason():
    transport = FixtureTransport({})
    street = onetomany.one_to_many_transit(
        *FROM, DESTINATIONS, time="2026-10-27T09:00:00Z",
        transit_modes=["RAIL", "WALK"], transport=transport,
    )
    assert_error(street, errors.STREET_MODE_NOT_TRANSIT, errors.INVALID_INPUT)
    debug = onetomany.one_to_many_transit(
        *FROM, DESTINATIONS, time="2026-10-27T09:00:00Z",
        transit_modes=["DEBUG_WALK"], transport=transport,
    )
    assert_error(debug, errors.STREET_MODE_NOT_TRANSIT, errors.INVALID_INPUT)
    unknown = onetomany.one_to_many_transit(
        *FROM, DESTINATIONS, time="2026-10-27T09:00:00Z",
        transit_modes=["rail"], transport=transport,
    )
    assert_error(unknown, errors.UNKNOWN_MODE, errors.INVALID_INPUT)
    assert transport.urls == []


def test_intermodal_transit_modes_keep_caller_order_in_one_value():
    transport = intermodal_transport()

    onetomany.one_to_many_transit(
        *FROM, DESTINATIONS[:1], time="2026-10-27T09:00:00Z",
        transit_modes=["TRAM", "RAIL", "BUS"], transport=transport,
    )

    query = transport.query()
    assert query["transitModes"] == ["TRAM,RAIL,BUS"]
    assert len([key for key in query if key == "transitModes"]) == 1


# ── reachable ───────────────────────────────────────────────────────


def reachable_transport(body=None):
    return FixtureTransport({"/api/v6/one-to-all": body or fs.load("synthetic_reachable_pareto.json")})


def test_reachable_maps_places_and_keeps_optional_members_null():
    transport = reachable_transport()

    result = onetomany.reachable(
        *FROM, max_time_min=60, time="2026-10-27T09:00:00Z", transport=transport,
    )

    assert result["outcome"] == errors.OK
    assert result["one"]["name"] == "Ayr"
    assert result["one"]["zone"] == "Europe/London"
    assert len(result["entries"]) == 3
    first, second, third = result["entries"]
    assert first == {
        "name": "Glasgow Central", "lat": 55.858315, "lon": -4.258436,
        "travel_time_seconds": 3060, "transfers_k": 1,
    }
    assert second["transfers_k"] == 2
    assert third["travel_time_seconds"] is None
    assert third["transfers_k"] is None
    assert result["query"]["one_role"] == "origin"
    assert result["query"]["control"] == "depart_after"


def test_reachable_request_uses_the_comma_single_endpoint_form():
    transport = reachable_transport()

    onetomany.reachable(*FROM, max_time_min=90, time="2026-10-27T09:00:00Z", transport=transport)

    query = transport.query()
    assert query["one"] == ["55.458267,-4.625827"]
    assert query["maxTravelTime"] == ["90"]
    assert query["arriveBy"] == ["false"]
    assert query["time"] == ["2026-10-27T09:00:00Z"]
    assert transport.paths() == ["/api/v6/one-to-all"]


def test_reachable_naive_clock_uses_one_bounded_coordinate_inference():
    transport = FixtureTransport(
        {"/api/v1/reverse-geocode": DISCOVERY, "/api/v6/one-to-all": fs.load("synthetic_reachable_pareto.json")}
    )

    result = onetomany.reachable(*FROM, time="2026-10-27T09:00:00", transport=transport)

    assert result["outcome"] == errors.OK
    assert transport.paths() == ["/api/v1/reverse-geocode", "/api/v6/one-to-all"]
    assert transport.query(0)["type"] == ["STOP"]
    assert transport.query(0)["numResults"] == ["1"]
    assert transport.query(1)["time"] == ["2026-10-27T09:00:00Z"]
    frame = result["time_frame"]
    assert frame["frame_source"] == "endpoint_reverse_geocode"
    assert frame["frame_zone"] == "Europe/London"


def test_reachable_arrive_by_reads_the_destination_clock_and_labels_the_role():
    transport = FixtureTransport(
        {"/api/v1/reverse-geocode": DISCOVERY, "/api/v6/one-to-all": fs.load("synthetic_reachable_pareto.json")}
    )

    result = onetomany.reachable(
        *FROM, time="2026-10-27T09:00:00", arrive_by=True, transport=transport,
    )

    assert result["outcome"] == errors.OK
    assert transport.query(1)["arriveBy"] == ["true"]
    assert result["time_frame"]["control"] == "arrive_by"
    assert result["query"]["one_role"] == "destination"
    assert transport.paths() == ["/api/v1/reverse-geocode", "/api/v6/one-to-all"]


def test_reachable_caller_timezone_skips_discovery():
    transport = reachable_transport()

    result = onetomany.reachable(
        *FROM, time="2026-10-27T09:00:00", timezone="Europe/Berlin", transport=transport,
    )

    assert result["outcome"] == errors.OK
    assert transport.paths() == ["/api/v6/one-to-all"]
    assert transport.query()["time"] == ["2026-10-27T08:00:00Z"]
    assert result["time_frame"]["frame_source"] == "caller_timezone"


def test_reachable_endpoint_timezone_declaration_skips_discovery():
    transport = reachable_transport()

    result = onetomany.reachable(
        *FROM, time="2026-10-27T09:00:00", endpoint_timezone="Europe/London",
        transport=transport,
    )

    assert result["outcome"] == errors.OK
    assert transport.paths() == ["/api/v6/one-to-all"]
    assert result["time_frame"]["frame_source"] == "caller_endpoint_timezone"


def test_reachable_empty_all_is_no_routes():
    transport = reachable_transport({
        "one": {"name": "Ayr", "lat": 55.458267, "lon": -4.625827, "tz": "Europe/London"},
        "all": [],
    })

    result = onetomany.reachable(*FROM, time="2026-10-27T09:00:00Z", transport=transport)

    assert result["outcome"] == errors.NO_ROUTES
    assert result["entries"] == []


def test_reachable_zone_disagreement_is_unsupported():
    transport = FixtureTransport(
        {
            "/api/v1/reverse-geocode": [dict(DISCOVERY[0], tz="Europe/Berlin")],
            "/api/v6/one-to-all": fs.load("synthetic_reachable_pareto.json"),
        }
    )

    result = onetomany.reachable(*FROM, time="2026-10-27T09:00:00", transport=transport)

    assert_error(result, errors.TIMEZONE_DISAGREEMENT, errors.UNSUPPORTED)


@pytest.mark.parametrize(
    "body",
    [
        {"all": []},
        {"one": {"name": "Ayr", "lat": 55.458267, "lon": -4.625827}, "all": {}},
        {"one": {"lat": 55.458267, "lon": -4.625827}, "all": []},
        {"one": {"name": "", "lat": 55.458267, "lon": -4.625827}, "all": []},
        {"one": {"name": "Ayr", "lat": "55.4", "lon": -4.6}, "all": []},
        {"one": {"name": "Ayr", "lat": 55.458267}, "all": []},
        {"one": {"name": "Ayr", "lat": 55.458267, "lon": -4.625827},
         "all": [{"place": {"name": "Glasgow", "lat": 55.8, "lon": -4.2}, "duration": 60, "k": 1}, 7]},
    ],
)
def test_reachable_malformed_shapes_are_upstream_failures(body):
    transport = reachable_transport(body)

    result = onetomany.reachable(*FROM, time="2026-10-27T09:00:00Z", transport=transport)

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


@pytest.mark.parametrize(
    "member",
    [
        {"duration": 60.5},
        {"duration": True},
        {"k": 1.0},
        {"k": -1},
        {"k": True},
    ],
)
def test_reachable_malformed_optional_members_are_shape_failures(member):
    entry = {"place": {"name": "Glasgow", "lat": 55.8, "lon": -4.2}}
    entry.update(member)
    transport = reachable_transport(
        {"one": {"name": "Ayr", "lat": 55.458267, "lon": -4.625827, "tz": "Europe/London"}, "all": [entry]}
    )

    result = onetomany.reachable(*FROM, time="2026-10-27T09:00:00Z", transport=transport)

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_time_min": 0},
        {"max_time_min": 1441},
        {"max_time_min": True},
        {"arrive_by": 1},
        {"lat": 91},
        {"lon": -181},
        {"time": "2026-02-30"},
        {"timezone": "Not/AZone"},
    ],
)
def test_reachable_argument_validation_happens_before_any_request(kwargs):
    transport = FixtureTransport({})
    call = {"lat": FROM[0], "lon": FROM[1], "time": "2026-10-27T09:00:00Z"}
    call.update(kwargs)

    result = onetomany.reachable(transport=transport, **call)

    assert result.get("error"), result
    assert result["outcome"] == errors.INVALID_INPUT, result
    assert transport.urls == []


# ── DST is enforced before the confirmed request ────────────────────


@pytest.mark.parametrize(
    "time_value, reason",
    [
        ("2026-10-25T01:30:00", errors.AMBIGUOUS_LOCAL_TIME),
        ("2026-03-29T01:30:00", errors.NONEXISTENT_LOCAL_TIME),
    ],
)
def test_intermodal_dst_rejection_issues_no_confirmed_request(time_value, reason):
    transport = FixtureTransport({})

    result = onetomany.one_to_many_transit(
        *FROM, DESTINATIONS, time=time_value, timezone="Europe/London", transport=transport,
    )

    assert_error(result, reason, errors.INVALID_INPUT)
    assert transport.urls == []


@pytest.mark.parametrize(
    "time_value, reason",
    [
        ("2026-10-25T01:30:00", errors.AMBIGUOUS_LOCAL_TIME),
        ("2026-03-29T01:30:00", errors.NONEXISTENT_LOCAL_TIME),
    ],
)
def test_reachable_dst_rejection_issues_no_confirmed_request(time_value, reason):
    transport = FixtureTransport({})

    result = onetomany.reachable(*FROM, time=time_value, timezone="Europe/London", transport=transport)

    assert_error(result, reason, errors.INVALID_INPUT)
    assert transport.urls == []


def test_intermodal_naive_dst_rejection_stops_after_one_discovery_request():
    transport = FixtureTransport({"/api/v1/reverse-geocode": DISCOVERY})

    result = onetomany.one_to_many_transit(
        *FROM, DESTINATIONS, time="2026-10-25T01:30:00", transport=transport,
    )

    assert_error(result, errors.AMBIGUOUS_LOCAL_TIME, errors.INVALID_INPUT)
    assert transport.paths() == ["/api/v1/reverse-geocode"]


# ── R1: an integer outside the double range stays inside the envelope ──
#
# ``_is_finite_number`` converts an int with ``float(value)``; a JSON integer
# beyond the IEEE-754 range therefore raised ``OverflowError`` straight through
# every public function that validates a coordinate or a response number.
# Caller arguments keep invalid_input/value_out_of_range, unusable response
# members keep malformed_upstream/unexpected_shape with their locators, and
# ordinary ints and floats are untouched.

OVERFLOW_INT = 10 ** 400


@pytest.mark.parametrize("value", [OVERFLOW_INT, -OVERFLOW_INT], ids=["positive", "negative"])
@pytest.mark.parametrize("argument", ["from_lat", "from_lon"])
def test_street_overflow_sized_origin_is_rejected_before_io(argument, value):
    transport = FixtureTransport({})
    call = {"from_lat": FROM[0], "from_lon": FROM[1], "destinations": DESTINATIONS}
    call[argument] = value

    result = onetomany.one_to_many(transport=transport, **call)

    assert_error(result, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert result["error_details"]["argument"] == argument
    assert transport.urls == []


@pytest.mark.parametrize("value", [OVERFLOW_INT, -OVERFLOW_INT], ids=["positive", "negative"])
@pytest.mark.parametrize("axis, suffix", [(0, "lat"), (1, "lon")])
def test_street_overflow_sized_destination_is_rejected_before_io(axis, suffix, value):
    destinations = copy.deepcopy(DESTINATIONS)
    destinations[1][axis] = value

    result = onetomany.one_to_many(*FROM, destinations, transport=FixtureTransport({}))

    assert_error(result, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert result["error_details"]["argument"] == f"destinations[1].{suffix}"


@pytest.mark.parametrize("value", [OVERFLOW_INT, -OVERFLOW_INT], ids=["positive", "negative"])
def test_street_overflow_sized_max_sec_is_rejected_before_io(value):
    transport = FixtureTransport({})

    result = onetomany.one_to_many(*FROM, DESTINATIONS, max_sec=value, transport=transport)

    assert_error(result, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert result["error_details"]["argument"] == "max_sec"
    assert transport.urls == []


@pytest.mark.parametrize("value", [OVERFLOW_INT, -OVERFLOW_INT], ids=["positive", "negative"])
@pytest.mark.parametrize(
    "where", ["from_lat", "from_lon", "destination_lat", "destination_lon"]
)
def test_intermodal_overflow_sized_caller_coordinate_is_rejected_before_io(where, value):
    transport = FixtureTransport({})
    destinations = copy.deepcopy(DESTINATIONS)
    call = {"from_lat": FROM[0], "from_lon": FROM[1], "destinations": destinations}
    if where.startswith("from"):
        call[where] = value
        expected = where
    else:
        axis = 0 if where.endswith("lat") else 1
        destinations[0][axis] = value
        expected = f"destinations[0].{'lat' if axis == 0 else 'lon'}"

    result = onetomany.one_to_many_transit(
        time="2026-10-27T09:00:00Z", transport=transport, **call
    )

    assert_error(result, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert result["error_details"]["argument"] == expected
    assert transport.urls == []


@pytest.mark.parametrize("value", [OVERFLOW_INT, -OVERFLOW_INT], ids=["positive", "negative"])
@pytest.mark.parametrize("argument", ["lat", "lon"])
def test_reachable_overflow_sized_coordinate_is_rejected_before_io(argument, value):
    transport = FixtureTransport({})
    call = {"lat": FROM[0], "lon": FROM[1]}
    call[argument] = value

    result = onetomany.reachable(
        time="2026-10-27T09:00:00Z", transport=transport, **call
    )

    assert_error(result, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert result["error_details"]["argument"] == argument
    assert transport.urls == []


@pytest.mark.parametrize("value", [OVERFLOW_INT, -OVERFLOW_INT], ids=["positive", "negative"])
@pytest.mark.parametrize("member", ["duration", "distance"])
def test_street_overflow_sized_response_member_is_malformed_upstream(member, value):
    body = [{member: value}, {}, {}]

    result = onetomany.one_to_many(*FROM, DESTINATIONS, transport=street_transport(body))

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)
    assert result["error_details"] == {"index": 0, "member": member}


@pytest.mark.parametrize("value", [OVERFLOW_INT, -OVERFLOW_INT], ids=["positive", "negative"])
def test_intermodal_overflow_sized_street_duration_is_malformed_upstream(value):
    body = june(intermodal_body()["valid"])
    body["street_durations"][0]["duration"] = value

    result = onetomany.one_to_many_transit(
        *FROM, DESTINATIONS, time="2026-10-27T09:00:00Z",
        transport=intermodal_transport(body),
    )

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)
    assert result["error_details"] == {"index": 0, "member": "duration"}


@pytest.mark.parametrize("value", [OVERFLOW_INT, -OVERFLOW_INT], ids=["positive", "negative"])
def test_intermodal_overflow_sized_pareto_duration_is_malformed_upstream(value):
    body = june(intermodal_body()["valid"])
    body["transit_durations"][0][0]["duration"] = value

    result = onetomany.one_to_many_transit(
        *FROM, DESTINATIONS, time="2026-10-27T09:00:00Z",
        transport=intermodal_transport(body),
    )

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)
    assert result["error_details"] == {"index": 0, "entry": 0, "member": "duration"}


@pytest.mark.parametrize("value", [OVERFLOW_INT, -OVERFLOW_INT], ids=["positive", "negative"])
@pytest.mark.parametrize("axis", ["lat", "lon"])
def test_reachable_overflow_sized_place_coordinate_is_malformed_upstream(axis, value):
    body = june(fs.load("synthetic_reachable_pareto.json"))
    body["all"][1]["place"][axis] = value

    result = onetomany.reachable(
        *FROM, time="2026-10-27T09:00:00Z", transport=reachable_transport(body)
    )

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)
    assert result["error_details"] == {"member": "place", "index": 1}


def test_street_ordinary_finite_numbers_are_preserved():
    body = [{"duration": 423.75, "distance": 1000}, {}, {}]

    result = onetomany.one_to_many(
        *FROM, DESTINATIONS, max_sec=3600, transport=street_transport(body)
    )

    assert result["outcome"] == errors.OK
    assert result["results"][0]["duration_seconds"] == 423.75   # fraction intact
    assert result["results"][0]["distance_meters"] == 1000       # int intact


def test_reachable_ordinary_finite_integer_coordinates_still_succeed():
    result = onetomany.reachable(
        55, -4, time="2026-10-27T09:00:00Z", transport=reachable_transport()
    )

    assert result["outcome"] == errors.OK
