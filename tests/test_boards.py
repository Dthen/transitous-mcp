"""Departure boards, trip itineraries and map trip segments (§4.9–4.11, §4.13).

Every assertion is driven by a checked-in constructed fixture or a
small in-memory negative where the fixtures have no such shape. The transport double
records the assembled URL, so the request itself is inspected — not just the
mapping of a payload handed to the mapper directly.

The oracles this file exists for:

* the scheduled-only Ayr board is **not** realtime and reports ``scheduled: null``
  (never ``not realTime``);
* a stop-time body that omits the required ``realTime`` is malformed;
* a dummy-walk endpoint cannot report ``stop_cancelled: true`` later in the trip;
* map trips maps the pinned ``TripSegment`` and has nowhere to put a position;
* a naive board clock is read in the stop's own zone; a caller override and an
  explicit instant issue no discovery request.
"""

import copy
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

import fixture_support as fs  # noqa: F401  (puts the repository root on sys.path)

from transit_core import boards, errors

UTC = timezone.utc


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
    """Route one canned body per request path and record every assembled URL."""

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
    """Deep copy so a mutation test cannot leak into another test's body."""

    return copy.deepcopy(body)


def assert_error(result, error_type, outcome=None):
    assert isinstance(result, dict), result
    assert result.get("error"), result
    assert result["error_type"] == error_type, result
    assert result["outcome"] == (outcome or errors.REASON_OUTCOME[error_type]), result
    assert isinstance(result["error_details"], dict)


# ── module identity ─────────────────────────────────────────────────


def test_boards_module_lives_in_the_checkout():
    path = os.path.realpath(boards.__file__)
    assert path.startswith(os.path.realpath(fs.REPO_ROOT) + os.sep)


# ── departures: the constructed boards ──────────────────────────────


def test_scheduled_only_ayr_board_is_not_realtime_and_scheduled_is_null():
    data = fs.load("synthetic_stoptimes_ayr_p3.json")
    transport = FixtureTransport({"/api/v6/stoptimes": data})

    result = boards.departures(
        "gb-great-britain_9100AYRR3",
        count=20,
        time="2026-10-27T09:00:00Z",
        transport=transport,
    )

    assert result["outcome"] == errors.OK
    assert len(result["departures"]) == 20
    for departure in result["departures"]:
        assert departure["realtime"] is False
        assert departure["scheduled"] is None          # StopTime has no `scheduled`
        assert departure["expected_departure"] is None  # realTime false
        assert departure["scheduled_departure"] is not None
        assert departure["delay_seconds"] is None
        assert departure["scheduled_departure"]["zone"] == "Europe/London"

    first = result["departures"][0]
    assert first["scheduled_departure"]["iso"] == "2026-10-27T09:05:00+00:00"
    assert first["scheduled_departure"]["local"] == "2026-10-27T09:05:00"
    assert first["route"] == "ScotRail"      # routeShortName wins
    assert first["headsign"] == "Glasgow Central"
    assert first["mode"] == "REGIONAL_RAIL"
    assert first["route_type"] == 2
    assert first["agency_name"] == "ScotRail"
    assert first["trip_id"].startswith("20261027_09:05_")


def test_realtime_board_keeps_scheduled_null_and_uses_only_the_supplied_departure():
    data = fs.load("synthetic_stoptimes_ber_hbf.json")
    transport = FixtureTransport({"/api/v6/stoptimes": data})

    result = boards.departures(
        "at-Railway-Current-Reference-Data-2026_de:11000:900003200:1:51",
        count=10,
        time="2026-10-21T09:00:00Z",
        transport=transport,
    )

    assert result["outcome"] == errors.OK
    first_ten = result["departures"]
    assert len(first_ten) == 10
    assert any(row["realtime"] is True for row in first_ten)
    for row in first_ten:
        assert row["scheduled"] is None

    delayed = first_ten[1]
    assert delayed["realtime"] is True
    assert delayed["scheduled_departure"]["iso"] == "2026-10-21T11:14:00+02:00"
    assert delayed["expected_departure"]["iso"] == "2026-10-21T11:11:00+02:00"
    assert delayed["delay_seconds"] == -180

    not_realtime = first_ten[2]
    assert not_realtime["realtime"] is False
    assert not_realtime["scheduled_departure"] is not None
    assert not_realtime["expected_departure"] is None
    assert not_realtime["delay_seconds"] is None


def test_realtime_board_without_a_supplied_departure_reports_unknown_expected_and_delay():
    """R2 lock: realTime true with only a scheduled instant.

    Labelled synthetic row: every fixture row that is realtime carries both
    instants, so the "not inferred, not zero" branch was asserted nowhere. The
    expected departure must not fall back to the scheduled one, and a one-sided
    difference is null rather than zero.
    """

    data = june(fs.load("synthetic_stoptimes_ber_hbf.json"))
    row = data["stopTimes"][0]
    row["realTime"] = True
    row["place"].pop("departure", None)          # synthetic: scheduled, never expected
    data["stopTimes"] = [row]
    transport = FixtureTransport({"/api/v6/stoptimes": data})

    result = boards.departures(
        "at-Railway-Current-Reference-Data-2026_de:11000:900003200:1:51",
        count=1,
        time="2026-10-21T09:00:00Z",
        transport=transport,
    )

    assert result["outcome"] == errors.OK
    (departure,) = result["departures"]
    assert departure["realtime"] is True
    assert departure["scheduled"] is None             # independent of realTime
    assert departure["expected_departure"] is None    # never the scheduled fallback
    assert departure["delay_seconds"] is None         # never zero from one instant
    assert departure["scheduled_departure"]["iso"] == "2026-10-21T11:11:00+02:00"
    assert departure["scheduled_departure"]["local"] == "2026-10-21T11:11:00"
    assert departure["scheduled_departure"]["zone"] == "Europe/Berlin"


def test_departure_count_truncates_a_larger_body_in_upstream_order():
    """The board keeps the first `count` upstream rows, in the order supplied."""
    from datetime import datetime

    data = fs.load("synthetic_stoptimes_ayr_p3.json")
    assert len(data["stopTimes"]) > 5
    # Deliberately non-chronological: a sort-before-cap regression must not
    # look equivalent to retaining the provider's first five rows.
    data["stopTimes"][0], data["stopTimes"][7] = data["stopTimes"][7], data["stopTimes"][0]
    transport = FixtureTransport({"/api/v6/stoptimes": data})

    result = boards.departures(
        "gb-great-britain_9100AYRR3",
        count=5,
        time="2026-10-27T09:00:00Z",
        transport=transport,
    )

    assert result["outcome"] == errors.OK
    assert transport.query()["n"] == ["5"]
    assert result["query"]["count"] == 5
    assert result["returned_count"] == 5
    assert len(result["departures"]) == 5
    expected = [
        int(datetime.fromisoformat(row["place"]["scheduledDeparture"].replace("Z", "+00:00")).timestamp())
        for row in data["stopTimes"][:5]
    ]
    observed = [row["scheduled_departure"]["epoch_seconds"] for row in result["departures"]]
    assert observed == expected
    # the first retained row is still the upstream first row, not a re-sorted one
    assert result["departures"][0]["trip_id"] == data["stopTimes"][0]["tripId"]


def test_trip_cancelled_and_stop_cancelled_stay_separate():
    data = fs.load("synthetic_trip_cancelled_stoptime.json")
    transport = FixtureTransport({"/api/v6/stoptimes": data})

    result = boards.departures(
        "gb-great-britain_9100AYRR3", count=10, time="2026-10-27T09:00:00Z",
        transport=transport,
    )

    assert result["outcome"] == errors.OK
    rows = result["departures"]
    assert [row["trip_cancelled"] for row in rows] == [True, False, False]
    # the second row is a cancelled stop event on a trip that still runs
    assert [row["stop_cancelled"] for row in rows] == [True, True, False]
    # realtime availability is independent of both cancellation flags
    assert [row["realtime"] for row in rows] == [True, True, False]
    assert [row["scheduled"] for row in rows] == [None, None, None]


def test_missing_required_realtime_is_malformed_not_scheduled():
    data = june(fs.load("synthetic_stoptimes_ayr_p3.json"))
    del data["stopTimes"][0]["realTime"]
    transport = FixtureTransport({"/api/v6/stoptimes": data})

    result = boards.departures(
        "gb-great-britain_9100AYRR3", count=5, time="2026-10-27T09:00:00Z",
        transport=transport,
    )

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)
    assert result["error_details"]["index"] == 0


def test_wrongly_typed_realtime_is_malformed():
    data = june(fs.load("synthetic_stoptimes_ayr_p3.json"))
    data["stopTimes"][0]["realTime"] = "false"
    transport = FixtureTransport({"/api/v6/stoptimes": data})

    result = boards.departures(
        "gb-great-britain_9100AYRR3", count=5, time="2026-10-27T09:00:00Z",
        transport=transport,
    )

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


def test_departures_body_must_be_an_object():
    transport = FixtureTransport({"/api/v6/stoptimes": []})
    result = boards.departures(
        "gb-great-britain_9100AYRR3", time="2026-10-27T09:00:00Z", transport=transport,
    )
    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("stopTimes"),
        lambda d: d.__setitem__("stopTimes", {}),
        lambda d: d.__setitem__("place", []),
        lambda d: d.__setitem__("stopTimes", [42]),
    ],
)
def test_malformed_board_containers_are_upstream_shape_failures(mutate):
    data = june(fs.load("synthetic_stoptimes_ayr_p3.json"))
    mutate(data)
    transport = FixtureTransport({"/api/v6/stoptimes": data})

    result = boards.departures(
        "gb-great-britain_9100AYRR3", count=5, time="2026-10-27T09:00:00Z",
        transport=transport,
    )

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


# ── departures: clocks and discovery ────────────────────────────────


STOP_INFO = {"place": {"name": "Ayr", "stopId": "gb-great-britain_9100AYRR3",
                       "lat": 55.45814, "lon": -4.6258755, "tz": "Europe/London"}}


def test_naive_clock_uses_one_bounded_stop_info_request():
    data = fs.load("synthetic_stoptimes_ayr_p3.json")
    transport = FixtureTransport({"/api/v6/stop": STOP_INFO, "/api/v6/stoptimes": data})

    result = boards.departures(
        "gb-great-britain_9100AYRR3", count=1, time="2026-10-27T09:00:00",
        transport=transport,
    )

    assert result["outcome"] == errors.OK
    assert transport.paths() == ["/api/v6/stop", "/api/v6/stoptimes"]
    assert transport.query(0)["stopId"] == ["gb-great-britain_9100AYRR3"]
    assert transport.query(1)["time"] == ["2026-10-27T09:00:00Z"]
    frame = result["time_frame"]
    assert frame["form"] == "naive_local"
    assert frame["control"] == "depart_after"
    assert frame["frame_zone"] == "Europe/London"
    assert frame["frame_source"] == "endpoint_stop_info"


def test_caller_timezone_override_needs_no_discovery_request():
    data = fs.load("synthetic_stoptimes_ayr_p3.json")
    transport = FixtureTransport({"/api/v6/stoptimes": data})

    result = boards.departures(
        "gb-great-britain_9100AYRR3", count=1, time="2026-10-27T09:00:00",
        timezone="Europe/Berlin", transport=transport,
    )

    assert result["outcome"] == errors.OK
    assert transport.paths() == ["/api/v6/stoptimes"]
    assert transport.query()["time"] == ["2026-10-27T08:00:00Z"]
    assert result["time_frame"]["frame_source"] == "caller_timezone"
    assert result["time_frame"]["frame_zone"] == "Europe/Berlin"


def test_endpoint_timezone_declaration_needs_no_discovery_request():
    data = fs.load("synthetic_stoptimes_ayr_p3.json")
    transport = FixtureTransport({"/api/v6/stoptimes": data})

    result = boards.departures(
        "gb-great-britain_9100AYRR3", count=1, time="2026-10-27T09:00:00",
        endpoint_timezone="Europe/London", transport=transport,
    )

    assert result["outcome"] == errors.OK
    assert transport.paths() == ["/api/v6/stoptimes"]
    assert transport.query()["time"] == ["2026-10-27T09:00:00Z"]
    assert result["time_frame"]["frame_source"] == "caller_endpoint_timezone"


def test_explicit_instant_is_preserved_and_needs_no_discovery_request():
    data = fs.load("synthetic_stoptimes_ayr_p3.json")
    transport = FixtureTransport({"/api/v6/stoptimes": data})

    result = boards.departures(
        "gb-great-britain_9100AYRR3", count=1, time="2026-10-27T09:00:00+01:00",
        transport=transport,
    )

    assert result["outcome"] == errors.OK
    assert transport.paths() == ["/api/v6/stoptimes"]
    assert transport.query()["time"] == ["2026-10-27T08:00:00Z"]
    frame = result["time_frame"]
    assert frame["form"] == "instant"
    assert frame["frame_zone"] is None
    assert frame["frame_source"] == "not_applicable"
    assert frame["interpreted_instant"]["epoch_seconds"] == 1793088000


def test_fractional_instant_is_accepted_and_truncated_on_the_wire():
    data = fs.load("synthetic_stoptimes_ayr_p3.json")
    transport = FixtureTransport({"/api/v6/stoptimes": data})

    result = boards.departures(
        "gb-great-britain_9100AYRR3", count=1,
        time="2026-10-27T09:00:00.750Z", transport=transport,
    )

    # O3: T1 accepts the fraction and truncates it on the second-precision wire.
    assert result["outcome"] == errors.OK
    assert transport.query()["time"] == ["2026-10-27T09:00:00Z"]
    assert result["time_frame"]["as_given"] == "2026-10-27T09:00:00.750Z"


def test_omitted_time_is_resolved_exactly_once():
    data = fs.load("synthetic_stoptimes_ayr_p3.json")
    transport = FixtureTransport({"/api/v6/stoptimes": data})
    calls = []

    def now():
        calls.append(1)
        return datetime(2026, 10, 27, 9, 0, tzinfo=UTC)

    result = boards.departures(
        "gb-great-britain_9100AYRR3", count=1, transport=transport, now=now,
    )

    assert result["outcome"] == errors.OK
    assert len(calls) == 1
    assert transport.query()["time"] == ["2026-10-27T09:00:00Z"]
    assert result["time_frame"]["form"] == "omitted"
    assert result["time_frame"]["as_given"] is None


def test_stop_zone_disagreement_between_stop_info_and_response_is_unsupported():
    data = fs.load("synthetic_stoptimes_ayr_p3.json")
    stop_info = copy.deepcopy(STOP_INFO)
    stop_info["place"]["tz"] = "Europe/Berlin"
    transport = FixtureTransport({"/api/v6/stop": stop_info, "/api/v6/stoptimes": data})

    result = boards.departures(
        "gb-great-britain_9100AYRR3", count=1, time="2026-10-27T09:00:00",
        transport=transport,
    )

    assert_error(result, errors.TIMEZONE_DISAGREEMENT, errors.UNSUPPORTED)
    assert transport.paths() == ["/api/v6/stop", "/api/v6/stoptimes"]


# ── departures: argument validation ─────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stop_id": ""},
        {"stop_id": None},
        {"stop_id": 42},
        {"count": 0},
        {"count": 51},
        {"count": True},
        {"count": "10"},
        {"window": 0},
        {"window": 86401},
        {"window": True},
        {"window": 0.5},
    ],
)
def test_departure_argument_validation_happens_before_any_request(kwargs):
    transport = FixtureTransport({})
    call = {"stop_id": "gb-great-britain_9100AYRR3", "time": "2026-10-27T09:00:00Z"}
    call.update(kwargs)

    result = boards.departures(transport=transport, **call)

    assert_error(result, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert transport.urls == []


def test_malformed_time_is_rejected_before_any_request():
    transport = FixtureTransport({})
    result = boards.departures(
        "gb-great-britain_9100AYRR3", time="2026-02-30", transport=transport,
    )
    assert_error(result, errors.MALFORMED_TIME, errors.INVALID_INPUT)
    assert transport.urls == []


def test_unknown_timezone_is_rejected_before_any_request():
    transport = FixtureTransport({})
    result = boards.departures(
        "gb-great-britain_9100AYRR3", time="2026-10-27T09:00:00Z",
        timezone="Not/AZone", transport=transport,
    )
    assert_error(result, errors.UNKNOWN_TIMEZONE, errors.INVALID_INPUT)
    assert transport.urls == []


def test_request_echo_carries_the_query_controls():
    data = fs.load("synthetic_stoptimes_ayr_p3.json")
    transport = FixtureTransport({"/api/v6/stoptimes": data})

    result = boards.departures(
        "gb-great-britain_9100AYRR3", count=20, window=900,
        time="2026-10-27T09:00:00Z", transport=transport,
    )

    assert transport.query()["stopId"] == ["gb-great-britain_9100AYRR3"]
    assert transport.query()["n"] == ["20"]
    assert transport.query()["window"] == ["900"]
    assert result["query"] == {
        "stop_id": "gb-great-britain_9100AYRR3",
        "count": 20,
        "window_seconds": 900,
        "control": "depart_after",
    }
    assert result["returned_count"] == 20


def test_missing_every_departure_timestamp_zone_is_null_with_a_warning():
    data = june(fs.load("synthetic_stoptimes_ayr_p3.json"))
    del data["stopTimes"][0]["place"]["tz"]
    transport = FixtureTransport({"/api/v6/stoptimes": data})

    result = boards.departures(
        "gb-great-britain_9100AYRR3", count=1, time="2026-10-27T09:00:00Z",
        transport=transport,
    )

    assert result["outcome"] == errors.OK
    departure = result["departures"][0]
    assert departure["scheduled_departure"]["zone"] is None
    assert departure["scheduled_departure"]["zone_source"] == "unavailable"
    assert departure["scheduled_departure"]["display"].endswith("(zone unavailable)")
    assert "endpoint_zone_unavailable" in result["warnings"]


# ── trip ────────────────────────────────────────────────────────────


def test_trip_maps_the_itinerary_and_intermediate_places():
    data = fs.load("synthetic_trip_intermediate_stops.json")
    transport = FixtureTransport({"/api/v6/trip": data})

    result = boards.trip("synthetic-trip-intermediate-1", transport=transport)

    assert result["outcome"] == errors.OK
    assert result["trip_id"] == "synthetic-trip-intermediate-1"
    assert transport.query()["tripId"] == ["synthetic-trip-intermediate-1"]
    assert len(result["legs"]) == 1
    leg = result["legs"][0]
    assert leg["index"] == 0
    assert leg["mode"] == "REGIONAL_RAIL"
    assert leg["route"] == "1"
    assert leg["headsign"] == "Glasgow Central"
    assert leg["agency_name"] is None            # payload supplies no agencyName
    assert leg["realtime"] is False
    assert leg["scheduled"] is True
    assert leg["cancelled"] is None              # no supplied cancelled flag
    assert leg["departure"]["iso"] == "2026-10-27T09:05:00+00:00"
    assert leg["arrival"]["iso"] == "2026-10-27T09:56:00+00:00"
    assert leg["scheduled_departure"]["iso"] == "2026-10-27T09:05:00+00:00"
    assert leg["scheduled_arrival"]["iso"] == "2026-10-27T09:56:00+00:00"
    assert leg["from"]["stop_id"] == "gb-great-britain_910GAYRR"
    assert leg["from"]["zone"] == "Europe/London"
    assert leg["to"]["zone"] == "Europe/London"
    assert leg["intermediate_stop_count"] == 3
    names = [stop["name"] for stop in leg["intermediate_stops"]]
    assert names == ["Troon", "Kilmarnock", "Barrhead"]
    first = leg["intermediate_stops"][0]
    assert first["stop_id"] == "gb-great-britain_910GTROON"
    assert first["arrival"]["iso"] == "2026-10-27T09:16:00+00:00"
    assert first["departure"]["iso"] == "2026-10-27T09:17:00+00:00"
    assert first["scheduled_arrival"]["iso"] == "2026-10-27T09:16:00+00:00"
    assert first["scheduled_departure"]["iso"] == "2026-10-27T09:17:00+00:00"
    assert first["track"] is None
    assert first["cancelled"] is None
    assert first["stop_cancelled"] is None


def _dummy_then_service():
    return {
        "id": "synthetic-whole-itinerary",
        "duration": 3000,
        "startTime": "2026-10-27T09:00:00Z",
        "endTime": "2026-10-27T09:50:00Z",
        "transfers": 0,
        "legs": [
            {
                "mode": "WALK",
                "startTime": "2026-10-27T09:00:00Z",
                "endTime": "2026-10-27T09:05:00Z",
                "scheduledStartTime": "2026-10-27T09:00:00Z",
                "scheduledEndTime": "2026-10-27T09:05:00Z",
                "realTime": False,
                "scheduled": True,
                "duration": 300,
                "from": {"name": "START", "lat": 55.45, "lon": -4.62, "cancelled": True},
                "to": {"name": "Ayr", "stopId": "S1", "lat": 55.458, "lon": -4.626,
                       "tz": "Europe/London", "cancelled": True},
                "cancelled": True,
                "legGeometry": {"points": "", "precision": 6, "length": 0},
            },
            {
                "mode": "REGIONAL_RAIL",
                "startTime": "2026-10-27T09:10:00Z",
                "endTime": "2026-10-27T09:50:00Z",
                "scheduledStartTime": "2026-10-27T09:10:00Z",
                "scheduledEndTime": "2026-10-27T09:50:00Z",
                "realTime": True,
                "scheduled": True,
                "duration": 2400,
                "cancelled": True,
                "from": {"name": "Ayr", "stopId": "S1", "lat": 55.458, "lon": -4.626,
                         "tz": "Europe/London", "cancelled": True},
                "to": {"name": "Glasgow Central", "stopId": "S2", "lat": 55.858,
                       "lon": -4.258, "tz": "Europe/London", "cancelled": True},
                "legGeometry": {"points": "abc", "precision": 6, "length": 10},
                "intermediateStops": [
                    {"name": "Ayr", "stopId": "S1", "lat": 55.458, "lon": -4.626,
                     "tz": "Europe/London", "cancelled": True},
                    {"name": "Troon", "stopId": "S3", "lat": 55.54, "lon": -4.65,
                     "tz": "Europe/London", "cancelled": False},
                ],
            },
        ],
    }


def test_dummy_walk_endpoint_never_reports_stop_cancelled_true_later_in_the_trip():
    transport = FixtureTransport({"/api/v6/trip": _dummy_then_service()})

    result = boards.trip("any-trip", transport=transport)

    assert result["outcome"] == errors.OK
    walk, service = result["legs"]
    assert walk["walking_route_unavailable"] is True
    assert walk["duration_is_estimate"] is True
    assert walk["cancelled"] is None
    assert walk["from"]["stop_cancelled"] is None
    assert walk["from"]["source_cancelled"] is True
    assert walk["from"]["source_cancelled_origin"] == "walking_route_unavailable"
    assert walk["to"]["stop_cancelled"] is None
    assert walk["to"]["source_cancelled_origin"] == "walking_route_unavailable"

    # the same stop appears later as a real service endpoint and must stay honest
    assert service["cancelled"] is True
    assert service["from"]["stop_cancelled"] is None
    assert service["from"]["source_cancelled"] is True
    assert service["from"]["source_cancelled_origin"] == "walking_route_unavailable"
    assert service["intermediate_stops"][0]["stop_cancelled"] is None
    assert service["intermediate_stops"][0]["source_cancelled_origin"] == "walking_route_unavailable"

    # a place cancelled outside the dummy context is a real stop cancellation
    assert service["to"]["stop_cancelled"] is True
    assert service["to"]["source_cancelled_origin"] == "service"
    assert service["intermediate_stops"][1]["stop_cancelled"] is False


def test_trip_argument_validation_happens_before_any_request():
    transport = FixtureTransport({})
    for bad in ("", None, 7):
        result = boards.trip(bad, transport=transport)
        assert_error(result, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert transport.urls == []


def test_trip_body_must_be_an_object():
    transport = FixtureTransport({"/api/v6/trip": []})
    result = boards.trip("x", transport=transport)
    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("legs"),
        lambda d: d.__setitem__("legs", {}),
        lambda d: d.__setitem__("legs", [42]),
        lambda d: d["legs"][0].__setitem__("mode", None),
        lambda d: d["legs"][0].__setitem__("from", []),
        lambda d: d["legs"][0].__setitem__("startTime", "not-a-time"),
        lambda d: d["legs"][0].__setitem__("intermediateStops", {}),
    ],
)
def test_malformed_trip_shapes_are_upstream_shape_failures(mutate):
    data = fs.load("synthetic_trip_intermediate_stops.json")
    mutate(data)
    transport = FixtureTransport({"/api/v6/trip": data})

    result = boards.trip("synthetic-trip-intermediate-1", transport=transport)

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


def test_unordered_trip_legs_are_malformed_before_mapping():
    data = fs.load("synthetic_trip_intermediate_stops.json")
    second = copy.deepcopy(data["legs"][0])
    second["startTime"] = "2026-10-27T09:00:00Z"
    second["endTime"] = "2026-10-27T09:04:00Z"
    data["legs"] = [data["legs"][0], second]
    transport = FixtureTransport({"/api/v6/trip": data})

    result = boards.trip("synthetic-trip-intermediate-1", transport=transport)

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


def test_trip_without_any_scheduled_pair_reports_null_not_a_copy():
    data = fs.load("synthetic_trip_intermediate_stops.json")
    leg = data["legs"][0]
    leg.pop("scheduledStartTime")
    leg.pop("scheduledEndTime")
    leg["from"].pop("scheduledDeparture")
    leg["to"].pop("scheduledArrival")
    transport = FixtureTransport({"/api/v6/trip": data})

    result = boards.trip("synthetic-trip-intermediate-1", transport=transport)

    assert result["outcome"] == errors.OK
    assert result["legs"][0]["scheduled_departure"] is None
    assert result["legs"][0]["scheduled_arrival"] is None
    assert result["legs"][0]["departure"] is not None


# ── map trips ───────────────────────────────────────────────────────


BBOX = dict(min_lat=55.4, min_lon=-4.7, max_lat=55.9, max_lon=-4.2)
WINDOW = dict(start_time="2026-10-27T09:00:00Z", end_time="2026-10-27T10:00:00Z")


def _map_transport(data=None):
    return FixtureTransport({"/api/v6/map/trips": data or fs.load("synthetic_map_trips_window.json")})


def test_map_trips_maps_the_pinned_trip_segment():
    transport = _map_transport()

    result = boards.map_trips(**BBOX, **WINDOW, transport=transport)

    assert result["outcome"] == errors.OK
    assert len(result["trips"]) == 2
    first = result["trips"][0]
    assert first["trip_id"] == "synthetic-trip-map-1"
    assert first["route"] == "1"
    assert first["mode"] == "BUS"
    assert first["from"]["name"] == "Ayr"
    assert first["to"]["name"] == "Glasgow Central"
    assert first["departure"]["iso"] == "2026-10-27T09:08:00+00:00"
    assert first["arrival"]["iso"] == "2026-10-27T09:57:00+00:00"
    assert first["scheduled_departure"]["iso"] == "2026-10-27T09:05:00+00:00"
    assert first["scheduled_arrival"]["iso"] == "2026-10-27T09:56:00+00:00"
    assert first["from_lat"] == 55.458267
    assert first["from_lon"] == -4.625827
    assert first["to_lat"] == 55.858315
    assert first["to_lon"] == -4.258436
    assert first["distance_meters"] == 1840.5
    assert first["polyline_present"] is True
    assert first["realtime"] is True
    assert first["from"]["zone"] == "Europe/London"
    assert result["trips"][1]["realtime"] is False
    assert result["trips"][1]["route"] == "X2"

    flat = json.dumps(result)
    assert "vehicle_position" not in flat
    assert "position" not in flat


def test_map_trips_request_uses_ordered_bounds_and_an_instant_window():
    transport = _map_transport()

    boards.map_trips(**BBOX, **WINDOW, transport=transport)

    query = transport.query()
    assert query["min"] == ["55.4,-4.7"]
    assert query["max"] == ["55.9,-4.2"]
    assert query["zoom"] == ["12"]
    assert query["startTime"] == ["2026-10-27T09:00:00Z"]
    assert query["endTime"] == ["2026-10-27T10:00:00Z"]
    assert transport.paths() == ["/api/v6/map/trips"]


def test_map_trips_naive_window_reads_in_the_caller_timezone():
    transport = _map_transport()

    result = boards.map_trips(
        **BBOX, start_time="2026-10-27T09:00:00", end_time="2026-10-27T10:00:00",
        timezone="Europe/London", transport=transport,
    )

    assert result["outcome"] == errors.OK
    assert transport.query()["startTime"] == ["2026-10-27T09:00:00Z"]
    assert transport.query()["endTime"] == ["2026-10-27T10:00:00Z"]
    assert result["time_window"]["frame_zone"] == "Europe/London"
    assert result["time_window"]["frame_source"] == "caller_timezone"
    assert result["time_window"]["start_form"] == "naive_local"


def test_map_trips_naive_window_without_a_zone_is_rejected_before_io():
    transport = _map_transport()

    result = boards.map_trips(
        **BBOX, start_time="2026-10-27T09:00:00", end_time="2026-10-27T10:00:00",
        transport=transport,
    )

    assert_error(result, errors.BBOX_WINDOW_REQUIRES_EXPLICIT_ZONE, errors.INVALID_INPUT)
    assert "timezone" in result["error_details"]["remedy"]
    assert transport.urls == []


def test_map_trips_caller_timezone_with_instants_is_display_only():
    transport = _map_transport()

    result = boards.map_trips(**BBOX, **WINDOW, timezone="Europe/London", transport=transport)

    window = result["time_window"]
    assert window["start_form"] == "instant"
    assert window["frame_zone"] is None          # an instant has no frame
    assert window["frame_source"] == "not_applicable"
    assert window["start_time"]["zone"] == "Europe/London"
    assert window["start_time"]["zone_source"] == "caller_timezone"


def test_map_trips_explicit_instants_need_no_invented_endpoint():
    transport = _map_transport()

    result = boards.map_trips(**BBOX, **WINDOW, transport=transport)

    window = result["time_window"]
    assert window["start_form"] == "instant"
    assert window["end_form"] == "instant"
    assert window["frame_zone"] is None
    assert window["frame_source"] == "not_applicable"
    assert window["start_time"]["zone"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_lat": 55.9},
        {"max_lon": -4.7},
        {"min_lat": -91},
        {"max_lat": 91},
        {"min_lon": -181},
        {"max_lon": 181},
        {"zoom": 0},
        {"zoom": 20},
        {"zoom": True},
        {"zoom": 12.5},
        {"start_time": None},
        {"end_time": None},
        {"start_time": "2026-10-27T10:00:00Z"},
        {"end_time": "2026-10-27T09:00:00Z"},
        {"start_time": "2026-10-27T09:00:00Z", "end_time": None},
    ],
)
def test_map_trips_argument_validation_happens_before_any_request(kwargs):
    transport = FixtureTransport({})
    call = dict(BBOX, **WINDOW)
    call.update(kwargs)

    result = boards.map_trips(transport=transport, **call)

    assert_error(result, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert transport.urls == []


def test_map_trips_unknown_timezone_is_rejected_before_any_request():
    transport = FixtureTransport({})
    result = boards.map_trips(**BBOX, **WINDOW, timezone="Nope/Nope", transport=transport)
    assert_error(result, errors.UNKNOWN_TIMEZONE, errors.INVALID_INPUT)
    assert transport.urls == []


def test_map_trips_missing_realtime_is_malformed():
    data = june(fs.load("synthetic_map_trips_window.json"))
    del data[0]["realTime"]
    transport = _map_transport(data)

    result = boards.map_trips(**BBOX, **WINDOW, transport=transport)

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)
    assert result["error_details"]["index"] == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("realTime"),
        lambda d: d.__setitem__("realTime", "yes"),
        lambda d: d.__setitem__("departure", "not-a-time"),
        lambda d: d.__setitem__("scheduledDeparture", "2026-13-40T09:00:00Z"),
        lambda d: d.__setitem__("from", []),
        lambda d: d.__setitem__("trips", []),
        lambda d: d.__setitem__("distance", "1840.5"),
    ],
)
def test_malformed_trip_segments_are_upstream_shape_failures(mutate):
    data = june(fs.load("synthetic_map_trips_window.json"))
    mutate(data[0])
    transport = _map_transport(data)

    result = boards.map_trips(**BBOX, **WINDOW, transport=transport)

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


def test_map_trips_body_must_be_an_array():
    transport = _map_transport({"trips": []})
    result = boards.map_trips(**BBOX, **WINDOW, transport=transport)
    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


def test_map_trips_preserves_the_documented_fifty_entry_cap():
    segment = fs.load("synthetic_map_trips_window.json")[0]
    data = []
    for index in range(60):
        item = copy.deepcopy(segment)
        item["trips"][0]["tripId"] = f"trip-{index}"
        data.append(item)
    transport = _map_transport(data)

    result = boards.map_trips(**BBOX, **WINDOW, transport=transport)

    assert result["outcome"] == errors.OK
    assert len(result["trips"]) == 50
    assert result["returned_count"] == 50
    assert result["truncated"] is True
    assert result["max_results"] == 50


def test_map_trips_polyline_and_missing_zone_are_honest():
    data = june(fs.load("synthetic_map_trips_window.json"))
    data[0].pop("polyline")
    del data[0]["to"]["tz"]
    transport = _map_transport(data)

    result = boards.map_trips(**BBOX, **WINDOW, transport=transport)

    assert result["outcome"] == errors.OK
    first = result["trips"][0]
    assert first["polyline_present"] is False
    assert first["distance_meters"] == 1840.5
    assert first["arrival"]["zone"] is None
    assert first["arrival"]["utc_offset_seconds"] == 0
    assert "endpoint_zone_unavailable" in result["warnings"]


# ── host-zone independence ──────────────────────────────────────────


INVARIANCE_SCRIPT = r'''
import json, os, sys
from urllib.parse import urlsplit

REPO = os.environ["T4_REPO"]
sys.path.insert(0, REPO)
from transit_core import boards, onetomany

D = os.path.join(REPO, "tests", "fixtures")
def load(name):
    with open(os.path.join(D, name), "rb") as handle:
        return json.loads(handle.read())

class Response:
    status = 200
    def __init__(self, body): self.body = body
    def read(self): return json.dumps(self.body).encode()
    def close(self): pass

class Transport:
    def __init__(self, routes): self.routes = routes
    def __call__(self, request, timeout):
        return Response(self.routes[urlsplit(request.full_url).path])

t = Transport({
    "/api/v6/stoptimes": load("synthetic_stoptimes_ber_hbf.json"),
    "/api/v6/trip": load("synthetic_trip_intermediate_stops.json"),
    "/api/v6/map/trips": load("synthetic_map_trips_window.json"),
    "/api/v6/one-to-all": load("synthetic_reachable_pareto.json"),
    "/api/v1/one-to-many": load("synthetic_one_to_many_street.json"),
    "/api/experimental/one-to-many-intermodal": load("synthetic_one_to_many_transit.json")["valid"],
})
out = {}
out["departures"] = boards.departures(
    "at-Railway-Current-Reference-Data-2026_de:11000:900003200:1:51",
    count=10, time="2026-10-21T09:00:00Z", transport=t)
out["trip"] = boards.trip("synthetic-trip-intermediate-1", transport=t)
out["map_trips"] = boards.map_trips(
    min_lat=55.4, min_lon=-4.7, max_lat=55.9, max_lon=-4.2,
    start_time="2026-10-27T09:00:00Z", end_time="2026-10-27T10:00:00Z", transport=t)
out["reachable"] = onetomany.reachable(
    55.458267, -4.625827, max_time_min=60, time="2026-10-27T09:00:00Z",
    timezone="Europe/London", transport=t)
out["street"] = onetomany.one_to_many(
    55.458267, -4.625827, [[55.858315, -4.258436], [55.9527965, -3.1877235], [55.5016, -4.6142]],
    transport=t)
out["intermodal"] = onetomany.one_to_many_transit(
    55.458267, -4.625827,
    [[55.858315, -4.258436], [55.9527965, -3.1877235], [55.5016, -4.6142]],
    time="2026-10-27T09:00:00", timezone="Europe/London", transport=t)
print(json.dumps(out, sort_keys=True))
'''


def test_decisions_are_identical_under_four_host_zones(tmp_path):
    script = tmp_path / "invariance.py"
    script.write_text(INVARIANCE_SCRIPT, encoding="utf-8")
    outputs = []
    for zone in ("UTC", "Europe/London", "America/New_York", "Australia/Sydney"):
        env = dict(os.environ)
        env["TZ"] = zone
        env["T4_REPO"] = fs.REPO_ROOT
        completed = subprocess.run(
            ["/usr/bin/python3", str(script)],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert completed.returncode == 0, (zone, completed.stderr)
        outputs.append(completed.stdout)
    assert len(set(outputs)) == 1, "output differs between host zones"


# ── R1: an integer outside the double range stays inside the envelope ──
#
# ``_is_finite_number`` converts an int with ``float(value)``; a JSON integer
# beyond the IEEE-754 range therefore raised ``OverflowError`` straight through
# the public functions. Caller coordinates keep invalid_input/value_out_of_range,
# an unusable response member keeps malformed_upstream/unexpected_shape, and
# ordinary ints and floats are untouched.

OVERFLOW_INT = 10 ** 400


@pytest.mark.parametrize("value", [OVERFLOW_INT, -OVERFLOW_INT], ids=["positive", "negative"])
@pytest.mark.parametrize("argument", ["min_lat", "min_lon", "max_lat", "max_lon"])
def test_map_trips_overflow_sized_coordinate_is_rejected_before_io(argument, value):
    transport = FixtureTransport({})
    call = dict(BBOX, **WINDOW)
    call[argument] = value

    result = boards.map_trips(transport=transport, **call)

    assert_error(result, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert result["error_details"]["argument"] == argument
    assert transport.urls == []


@pytest.mark.parametrize("value", [OVERFLOW_INT, -OVERFLOW_INT], ids=["positive", "negative"])
def test_map_trips_overflow_sized_distance_is_malformed_upstream(value):
    data = june(fs.load("synthetic_map_trips_window.json"))
    data[0]["distance"] = value

    result = boards.map_trips(**BBOX, **WINDOW, transport=_map_transport(data))

    assert_error(result, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)
    assert result["error_details"] == {"index": 0, "member": "distance"}


@pytest.mark.parametrize("value", [OVERFLOW_INT, -OVERFLOW_INT], ids=["positive", "negative"])
@pytest.mark.parametrize("key", ["lat", "lon"])
def test_map_trips_overflow_sized_endpoint_coordinate_leaves_the_owned_field_unknown(key, value):
    data = june(fs.load("synthetic_map_trips_window.json"))
    data[0]["from"][key] = value

    result = boards.map_trips(**BBOX, **WINDOW, transport=_map_transport(data))

    assert result["outcome"] == errors.OK
    # from_lat/from_lon are the nullable owned members; never a fabricated number.
    assert result["trips"][0]["from_" + key] is None
    assert result["trips"][0]["distance_meters"] == 1840.5


@pytest.mark.parametrize(
    "bbox",
    [
        (55, -4, 56, -3),              # ordinary integers
        (55.4, -4.7, 55.9, -4.2),      # ordinary floats
    ],
    ids=["integers", "floats"],
)
def test_map_trips_ordinary_finite_coordinates_still_succeed(bbox):
    result = boards.map_trips(*bbox, **WINDOW, transport=_map_transport())

    assert result["outcome"] == errors.OK
    assert len(result["trips"]) == 2
