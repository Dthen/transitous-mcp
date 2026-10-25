"""T2 duration tests: the §3.1 vectors, conservation, calendar facts (§3.1–3.2).

The recorded vectors are asserted leg by leg as well as by conservation, so a
missing internal walk cannot reconcile arithmetically while misdescribing the
journey (§3.1) — that is exactly the failure mode a residual-based
``transfer_walk_seconds`` hides.
"""

import itertools
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixture_support

from transit_core import errors, plan

UTC = timezone.utc

AYR_NCL_COORD = {
    "from_lat": 55.458267,
    "from_lon": -4.625827,
    "to_lat": 54.968338,
    "to_lon": -1.616046,
    "time": "2026-10-27T09:00:00Z",
    "transit_modes": ["RAIL"],
}

VECTORS = (
    "elapsed_seconds",
    "access_seconds",
    "egress_seconds",
    "transfer_walk_seconds",
    "in_transit_seconds",
    "waiting_seconds",
    "station_to_station_seconds",
)


class StubResponse:
    def __init__(self, body):
        self.status = 200
        self._body = body

    def read(self):
        return self._body

    def close(self):
        pass


class Transport:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.urls = []

    def __call__(self, request, timeout):
        self.urls.append(request.full_url)
        return StubResponse(self.payload)


def run(args, fixture=None, body=None):
    transport = Transport(fixture_support.read_bytes(fixture) if fixture else body)
    result = plan.plan(args, transport=transport)
    assert result["outcome"] == "ok", result
    return result


def itinerary_of(result, option=1):
    for itinerary in result["itineraries"]:
        if itinerary["option"] == option:
            return itinerary
    raise AssertionError(f"no option {option} in {[i['option'] for i in result['itineraries']]}")


def vector(itinerary):
    return tuple(itinerary[name] for name in VECTORS)


# ── §3.1 verification vectors ──────────────────────────────────────


def test_ayr_newcastle_coordinate_vector():
    result = run(AYR_NCL_COORD, fixture="plan_ayr_ncl_coord.json")
    first = itinerary_of(result, 1)
    assert first["elapsed_seconds"] == 14100
    assert first["access_seconds"] == 240
    assert first["egress_seconds"] == 60
    assert first["transfer_walk_seconds"] == 240
    assert first["in_transit_seconds"] == 12240
    assert first["waiting_seconds"] == 1320
    assert first["station_to_station_seconds"] == 13800
    assert first["duration_basis"] == "transit_itinerary"


def test_ayr_newcastle_transfer_walk_is_the_sum_of_both_internal_walks():
    result = run(AYR_NCL_COORD, fixture="plan_ayr_ncl_coord.json")
    legs = itinerary_of(result, 1)["legs"]
    modes = [leg["mode"] for leg in legs]
    assert modes == ["WALK", "REGIONAL_RAIL", "WALK", "REGIONAL_RAIL", "WALK", "LONG_DISTANCE", "WALK"]
    internal = legs[2:5]
    # two 120-second walks between the first and last transit legs, counted one by one
    walk_seconds = [leg["duration_seconds"] for leg in internal if leg["mode"] == "WALK"]
    assert walk_seconds == [120, 120]
    assert sum(walk_seconds) == itinerary_of(result, 1)["transfer_walk_seconds"] == 240
    transit_seconds = [leg["duration_seconds"] for leg in legs if leg["mode"] != "WALK"]
    assert sum(transit_seconds) == itinerary_of(result, 1)["in_transit_seconds"] == 12240
    first = itinerary_of(result, 1)
    recomputed = (
        first["elapsed_seconds"]
        - first["access_seconds"]
        - first["egress_seconds"]
        - sum(walk_seconds)
        - sum(transit_seconds)
    )
    assert recomputed == first["waiting_seconds"] == 1320


def test_ayr_newcastle_five_option_elapsed_span():
    result = run(AYR_NCL_COORD, fixture="plan_ayr_ncl_coord.json")
    elapsed = [itinerary["elapsed_seconds"] for itinerary in result["itineraries"]]
    assert elapsed == [14100, 13860, 12840, 13860, 14100]
    assert min(elapsed) == 12840
    assert max(elapsed) == 14100


def test_ayr_newcastle_station_to_station_excludes_only_access_and_egress():
    result = run(AYR_NCL_COORD, fixture="plan_ayr_ncl_coord.json")
    for itinerary in result["itineraries"]:
        expected = (
            itinerary["elapsed_seconds"]
            - itinerary["access_seconds"]
            - itinerary["egress_seconds"]
        )
        assert itinerary["station_to_station_seconds"] == expected
        # transfer walking stays inside the station-to-station figure
        assert itinerary["station_to_station_seconds"] > itinerary["in_transit_seconds"]


def test_glasgow_edinburgh_coordinate_vector():
    args = {
        "from_lat": 55.858315,
        "from_lon": -4.258436,
        "to_lat": 55.9527965,
        "to_lon": -3.1877235,
        "time": "2026-10-27T09:00:00Z",
        "num_itineraries": 3,
        "transit_modes": ["RAIL"],
    }
    result = run(args, fixture="plan_glw_edi_coord.json")
    assert vector(itinerary_of(result, 1)) == (4620, 660, 300, 240, 3300, 120, 3660)


def test_stop_id_newcastle_fastest_bookkeeping():
    args = {
        "from_stop_id": "gb-great-britain_910GAYRR",
        "to_stop_id": "gb-great-britain_910GNWCSTLE",
        "time": "2026-10-27T09:00:00Z",
        "num_itineraries": 3,
        "transit_modes": ["RAIL"],
    }
    result = run(args, fixture="plan_ayr_ncl_stopids_n3.json")
    fastest = min(result["itineraries"], key=lambda item: item["elapsed_seconds"])
    assert fastest["elapsed_seconds"] == 12540
    assert fastest["changes"] == 2
    assert fastest["transit_leg_count"] == 3
    assert fastest["access_seconds"] == 0
    assert fastest["egress_seconds"] == 0
    assert fastest["station_to_station_seconds"] == 12540


def test_newcastle_ayr_overnight_vector():
    args = {
        "from_lat": 54.968338,
        "from_lon": -1.616046,
        "to_lat": 55.458267,
        "to_lon": -4.625827,
        "time": "2026-10-27T22:15:00Z",
        "num_itineraries": 3,
        "transit_modes": ["RAIL"],
    }
    result = run(args, fixture="plan_ncl_ayr_depart.json")
    first = itinerary_of(result, 1)
    assert vector(first) == (34380, 600, 240, 360, 12420, 20760, 33540)
    assert first["arrival_date_offset_days"] == 1
    assert first["has_overnight"] is True
    assert first["arrival"]["local_date"] == "2026-10-28"
    assert first["departure"]["local_date"] == "2026-10-27"


def test_the_reverse_itinerary_runs_newcastle_to_ayr():
    args = {
        "from_lat": 54.968338,
        "from_lon": -1.616046,
        "to_lat": 55.458267,
        "to_lon": -4.625827,
        "time": "2026-10-27T22:15:00Z",
        "num_itineraries": 3,
        "transit_modes": ["RAIL"],
    }
    result = run(args, fixture="plan_ncl_ayr_depart.json")
    first = itinerary_of(result, 1)
    transit_legs = [leg for leg in first["legs"] if leg["mode"] != "WALK"]
    assert transit_legs[0]["from"]["name"] == "Newcastle"
    assert transit_legs[-1]["to"]["name"] == "Ayr"
    # the outer legs are endpoint walks, not service legs
    assert first["legs"][0]["mode"] == "WALK"
    # §4.5.2: the upstream's START/END sentinel is substituted with the requested
    # coordinate string for a coordinate endpoint
    assert first["legs"][0]["from"]["name"] == "54.968338,-1.616046"
    assert first["legs"][0]["to"]["name"] == "Newcastle"
    assert first["legs"][-1]["mode"] == "WALK"
    assert first["legs"][-1]["from"]["name"] == "Ayr"
    assert first["legs"][-1]["to"]["name"] == "55.458267,-4.625827"


def test_the_through_service_is_one_leg_and_no_change():
    args = {
        "from_stop_id": "gb-great-britain_910GGLGC",
        "to_stop_id": "gb-great-britain_910GNWCSTLE",
        "time": "2026-10-27T06:00:00Z",
        "num_itineraries": 5,
        "max_transfers": 0,
        "transit_modes": ["RAIL"],
        "search_window_seconds": 10800,
    }
    result = run(args, fixture="plan_glw_ncl_direct.json")
    first = itinerary_of(result, 1)
    assert first["transit_leg_count"] == 1
    assert first["changes"] == 0
    leg = first["legs"][0]
    assert leg["mode"] == "LONG_DISTANCE"
    assert leg["intermediate_stop_count"] == 5
    assert "Edinburgh" in leg["intermediate_stop_names"]
    assert leg["headsign"]
    assert result["query"]["search_window_seconds"] == 10800


# ── conservation over the whole corpus ─────────────────────────────


@pytest.mark.parametrize(
    "fixture,args",
    [
        ("plan_ayr_ncl_coord.json", AYR_NCL_COORD),
        (
            "plan_glw_edi_coord.json",
            {
                "from_lat": 55.858315,
                "from_lon": -4.258436,
                "to_lat": 55.9527965,
                "to_lon": -3.1877235,
                "time": "2026-10-27T09:00:00Z",
                "transit_modes": ["RAIL"],
            },
        ),
        (
            "plan_ncl_ayr_depart.json",
            {
                "from_lat": 54.968338,
                "from_lon": -1.616046,
                "to_lat": 55.458267,
                "to_lon": -4.625827,
                "time": "2026-10-27T22:15:00Z",
                "transit_modes": ["RAIL"],
            },
        ),
        (
            "plan_ber_muc.json",
            {
                "from_lat": 52.5251,
                "from_lon": 13.3694,
                "to_lat": 48.1402,
                "to_lon": 11.5587,
                "time": "2026-10-27T09:00:00Z",
                "transit_modes": ["RAIL"],
            },
        ),
        (
            "plan_ayr_man_stopids_n8.json.gz",
            {
                "from_stop_id": "gb-great-britain_910GAYRR",
                "to_stop_id": "gb-great-britain_910GMNCRPIC",
                "time": "2026-10-27T09:00:00Z",
                "num_itineraries": 8,
                "transit_modes": ["RAIL"],
            },
        ),
    ],
)
def test_every_recorded_itinerary_conserves(fixture, args):
    result = run(args, fixture=fixture)
    assert result["itineraries"]
    for itinerary in result["itineraries"]:
        assert itinerary["duration_basis"] == "transit_itinerary", itinerary["option"]
        for name in VECTORS:
            assert itinerary[name] is not None, (itinerary["option"], name)
            assert itinerary[name] >= 0, (itinerary["option"], name)
        total = (
            itinerary["access_seconds"]
            + itinerary["egress_seconds"]
            + itinerary["transfer_walk_seconds"]
            + itinerary["in_transit_seconds"]
            + itinerary["waiting_seconds"]
        )
        assert total == itinerary["elapsed_seconds"], itinerary["option"]


def test_elapsed_comes_from_the_instants_not_the_declared_duration():
    body = {
        "requestParameters": {},
        "debugOutput": {},
        "from": {"name": "A", "lat": 1.0, "lon": 2.0, "tz": "Europe/London"},
        "to": {"name": "B", "lat": 3.0, "lon": 4.0, "tz": "Europe/London"},
        "direct": [],
        "itineraries": [
            {
                # the declared duration contradicts the instants; the instants win
                "duration": 999,
                "startTime": "2026-10-27T09:00:00Z",
                "endTime": "2026-10-27T09:30:00Z",
                "transfers": 0,
                "id": "synthetic-elapsed",
                "legs": [
                    {
                        "mode": "RAIL",
                        "startTime": "2026-10-27T09:00:00Z",
                        "endTime": "2026-10-27T09:30:00Z",
                        "duration": 1800,
                        "from": {"name": "A", "tz": "Europe/London"},
                        "to": {"name": "B", "tz": "Europe/London"},
                        "legGeometry": {"points": "abc"},
                    }
                ],
            }
        ],
        "previousPageCursor": None,
        "nextPageCursor": None,
    }
    result = run(dict(AYR_NCL_COORD), body=body)
    itinerary = itinerary_of(result)
    assert itinerary["elapsed_seconds"] == 1800
    assert itinerary["in_transit_seconds"] == 1800


def test_a_missing_component_is_null_and_warns_rather_than_zero():
    """§3.1 rule 1: an absent input yields null plus a named warning.

    Labelled synthetic body: one internal street leg carries neither a duration
    nor its own instants, so the transfer walk cannot be known.
    """
    body = {
        "requestParameters": {},
        "debugOutput": {},
        "from": {"name": "A", "lat": 1.0, "lon": 2.0, "tz": "Europe/London"},
        "to": {"name": "B", "lat": 3.0, "lon": 4.0, "tz": "Europe/London"},
        "direct": [],
        "itineraries": [
            {
                "duration": 3600,
                "startTime": "2026-10-27T09:00:00Z",
                "endTime": "2026-10-27T10:00:00Z",
                "transfers": 1,
                "id": "synthetic-incomplete",
                "legs": [
                    {
                        "mode": "WALK",
                        "startTime": "2026-10-27T09:00:00Z",
                        "endTime": "2026-10-27T09:05:00Z",
                        "duration": 300,
                        "from": {"name": "A", "tz": "Europe/London"},
                        "to": {"name": "S1", "tz": "Europe/London"},
                        "legGeometry": {"points": "a"},
                    },
                    {
                        "mode": "RAIL",
                        "startTime": "2026-10-27T09:05:00Z",
                        "endTime": "2026-10-27T09:40:00Z",
                        "duration": 2100,
                        "from": {"name": "S1", "tz": "Europe/London"},
                        "to": {"name": "S2", "tz": "Europe/London"},
                        "legGeometry": {"points": "b"},
                    },
                    {
                        # no duration and no instants: the honest answer is null
                        "mode": "WALK",
                        "from": {"name": "S2", "tz": "Europe/London"},
                        "to": {"name": "S3", "tz": "Europe/London"},
                        "legGeometry": {"points": "c"},
                    },
                    {
                        "mode": "RAIL",
                        "startTime": "2026-10-27T09:45:00Z",
                        "endTime": "2026-10-27T10:00:00Z",
                        "duration": 900,
                        "from": {"name": "S3", "tz": "Europe/London"},
                        "to": {"name": "B", "tz": "Europe/London"},
                        "legGeometry": {"points": "d"},
                    },
                ],
            }
        ],
        "previousPageCursor": None,
        "nextPageCursor": None,
    }
    result = run(dict(AYR_NCL_COORD), body=body)
    itinerary = itinerary_of(result)
    assert itinerary["transfer_walk_seconds"] is None
    assert itinerary["waiting_seconds"] is None
    assert itinerary["in_transit_seconds"] == 3000
    assert itinerary["access_seconds"] == 300
    assert itinerary["egress_seconds"] == 0
    assert itinerary["duration_basis"] == "incomplete"
    assert "duration_components_incomplete" in [w["code"] for w in result["warnings"]]


def test_a_negative_remainder_is_null_never_emitted_as_negative():
    """Labelled synthetic body: a declared leg duration larger than the journey.

    The components are never adjusted and no negative waiting time is emitted.
    """
    body = {
        "requestParameters": {},
        "debugOutput": {},
        "from": {"name": "A", "lat": 1.0, "lon": 2.0, "tz": "Europe/London"},
        "to": {"name": "B", "lat": 3.0, "lon": 4.0, "tz": "Europe/London"},
        "direct": [],
        "itineraries": [
            {
                "duration": 3600,
                "startTime": "2026-10-27T09:00:00Z",
                "endTime": "2026-10-27T10:00:00Z",
                "transfers": 0,
                "id": "synthetic-inconsistent",
                "legs": [
                    {
                        "mode": "RAIL",
                        "startTime": "2026-10-27T09:00:00Z",
                        "endTime": "2026-10-27T09:30:00Z",
                        "duration": 9000,
                        "from": {"name": "A", "tz": "Europe/London"},
                        "to": {"name": "B", "tz": "Europe/London"},
                        "legGeometry": {"points": "abc"},
                    }
                ],
            }
        ],
        "previousPageCursor": None,
        "nextPageCursor": None,
    }
    result = run(dict(AYR_NCL_COORD), body=body)
    itinerary = itinerary_of(result)
    assert itinerary["waiting_seconds"] is None
    assert itinerary["in_transit_seconds"] == 9000
    assert "duration_decomposition_inconsistent" in [w["code"] for w in result["warnings"]]


def test_a_street_only_itinerary_has_no_transit_components_and_no_changes():
    args = {
        "from_lat": 55.458267,
        "from_lon": -4.625827,
        "to_lat": 54.968338,
        "to_lon": -1.616046,
        "time": "2026-10-27T09:00:00Z",
        "max_transfers": 0,
        "transit_modes": ["RAIL"],
    }
    transport = Transport(fixture_support.read_bytes("synthetic_plan_direct_alternatives.json"))
    result = plan.plan(args, transport=transport)
    assert result["outcome"] == "no_routes"
    direct = result["direct"][0]
    assert direct["duration_basis"] == "street_only"
    assert direct["in_transit_seconds"] == 0
    for name in ("access_seconds", "egress_seconds", "transfer_walk_seconds", "waiting_seconds", "station_to_station_seconds"):
        assert direct[name] is None, name
    assert direct["changes"] is None
    assert "no_transit_leg" in [w["code"] for w in result["warnings"]]


def test_a_westbound_date_line_crossing_is_negative_and_warned():
    transport = Transport(fixture_support.read_bytes("synthetic_westbound_date_line.json"))
    result = plan.plan(dict(AYR_NCL_COORD), transport=transport)
    assert result["outcome"] == "ok", result
    itinerary = itinerary_of(result)
    assert itinerary["arrival_date_offset_days"] == -1
    assert itinerary["has_overnight"] is False
    assert itinerary["elapsed_seconds"] > 0
    assert "date_line_crossing" in [w["code"] for w in result["warnings"]]


def test_calendar_facts_use_each_endpoints_own_zone():
    args = {
        "from_lat": 52.3789,
        "from_lon": 4.9003,
        "to_lat": 52.5251,
        "to_lon": 13.3694,
        "time": "2026-10-27T09:00:00Z",
        "num_itineraries": 2,
        "max_transfers": 2,
        "transit_modes": ["RAIL"],
    }
    result = run(args, fixture="plan_ams_ber.json")
    first = itinerary_of(result)
    # the recorded payload tags Amsterdam Centraal as Europe/Brussels; that is the
    # upstream's value and is echoed, never corrected
    assert first["departure"]["zone"] == "Europe/Brussels"
    assert first["arrival"]["zone"] == "Europe/Berlin"
    assert first["departure"]["utc_offset_seconds"] == 3600
    assert first["arrival"]["utc_offset_seconds"] == 3600
    assert first["arrival_date_offset_days"] == 0
    # each endpoint renders its own local date and clock
    assert first["departure"]["local_date"] == "2026-10-27"
    assert first["arrival"]["local_date"] == "2026-10-27"
    assert first["departure"]["local"] != first["arrival"]["local"]


# ── leg-level fields that feed the quantities ──────────────────────


def test_leg_indices_and_durations_are_numeric_and_ordered():
    result = run(AYR_NCL_COORD, fixture="plan_ayr_ncl_coord.json")
    for itinerary in result["itineraries"]:
        assert [leg["index"] for leg in itinerary["legs"]] == list(range(len(itinerary["legs"])))
        starts = [leg["departure"]["epoch_seconds"] for leg in itinerary["legs"]]
        assert starts == sorted(starts)
        for leg in itinerary["legs"]:
            assert isinstance(leg["duration_seconds"], int)
            assert leg["duration_seconds"] >= 0


def test_realtime_absent_is_reported_honestly():
    result = run(AYR_NCL_COORD, fixture="plan_ayr_ncl_coord.json")
    assert result["query"]["realtime_present"] is False
    assert "realtime_unavailable" in [w["code"] for w in result["warnings"]]
    for itinerary in result["itineraries"]:
        assert itinerary["realtime_present"] is False
        for leg in itinerary["legs"]:
            assert leg["realtime"] is False
            assert leg["scheduled"] in (True, False)


def test_elapsed_summary_for_the_recorded_manchester_eight_option_page():
    args = {
        "from_stop_id": "gb-great-britain_910GAYRR",
        "to_stop_id": "gb-great-britain_910GMNCRPIC",
        "time": "2026-10-27T09:00:00Z",
        "num_itineraries": 8,
        "transit_modes": ["RAIL"],
    }
    result = run(args, fixture="plan_ayr_man_stopids_n8.json.gz")
    assert result["query"]["upstream_count"] == 8
    elapsed = [itinerary["elapsed_seconds"] for itinerary in result["itineraries"]]
    assert elapsed == [15780, 15660, 15780, 15660, 15780, 15660, 15480, 15780]
    changes = [itinerary["changes"] for itinerary in result["itineraries"]]
    assert changes == [2, 1, 2, 2, 2, 1, 2, 2]
    assert 15480 in elapsed
    assert min(elapsed) == 15480


def test_duration_module_errors_are_malformed_upstream_not_zero():
    body = {
        "requestParameters": {},
        "debugOutput": {},
        "from": {"name": "A", "lat": 1.0, "lon": 2.0},
        "to": {"name": "B", "lat": 3.0, "lon": 4.0},
        "direct": [],
        "itineraries": [
            {
                "duration": 1800,
                "startTime": "2026-10-27T09:00:00Z",
                "endTime": "2026-10-27T09:30:00Z",
                "transfers": 0,
                "id": "synthetic-bad-leg",
                "legs": [
                    {
                        "mode": "RAIL",
                        "startTime": "not-a-time",
                        "endTime": "2026-10-27T09:30:00Z",
                        "duration": 1800,
                        "from": {"name": "A"},
                        "to": {"name": "B"},
                        "legGeometry": {"points": "abc"},
                    }
                ],
            }
        ],
        "previousPageCursor": None,
        "nextPageCursor": None,
    }
    transport = Transport(body)
    result = plan.plan(dict(AYR_NCL_COORD), transport=transport)
    assert result["error_type"] == errors.UNEXPECTED_SHAPE
    assert result["error_details"]["leg"] == 0


# ── §3.1 ordered instants and declared durations (T2-S01, T2-S02) ──
#
# The ordering precondition belongs to this layer: durations.decompose_itinerary
# assumes instants "already validated as ordered" (§3.1). These bodies exercise
# the instants the consumers actually use (`startTime` else `from.departure`,
# `endTime` else `to.arrival`), the itinerary's own bounds, and the sign of a
# declared leg duration.

SYNTH_START = "2026-10-27T09:00:00Z"
SYNTH_END = "2026-10-27T10:00:00Z"

_MISSING = object()


def synthetic_leg(
    mode="RAIL",
    *,
    frm="A",
    to="B",
    start=None,
    end=None,
    departure=None,
    arrival=None,
    duration=_MISSING,
):
    leg = {
        "mode": mode,
        "from": {"name": frm, "tz": "Europe/London"},
        "to": {"name": to, "tz": "Europe/London"},
        "legGeometry": {"points": "abc"},
    }
    if start is not None:
        leg["startTime"] = start
    if end is not None:
        leg["endTime"] = end
    if departure is not None:
        leg["from"]["departure"] = departure
    if arrival is not None:
        leg["to"]["arrival"] = arrival
    if duration is not _MISSING:
        leg["duration"] = duration
    return leg


def synthetic_itinerary(legs, *, start=SYNTH_START, end=SYNTH_END, ident="synthetic-precondition"):
    return {
        "duration": 3600,
        "startTime": start,
        "endTime": end,
        "transfers": 0,
        "id": ident,
        "legs": legs,
    }


def synthetic_plan(legs=None, *, start=SYNTH_START, end=SYNTH_END, itineraries=None, direct=None):
    if itineraries is None:
        itineraries = [synthetic_itinerary(legs or [], start=start, end=end)]
    return {
        "requestParameters": {},
        "debugOutput": {},
        "from": {"name": "A", "lat": 1.0, "lon": 2.0, "tz": "Europe/London"},
        "to": {"name": "B", "lat": 3.0, "lon": 4.0, "tz": "Europe/London"},
        "direct": [] if direct is None else direct,
        "itineraries": itineraries,
        "previousPageCursor": None,
        "nextPageCursor": None,
    }


def evaluate(body):
    """Run the callable boundary without assuming success."""
    return plan.plan(dict(AYR_NCL_COORD), transport=Transport(body))


def assert_unexpected_shape(result):
    assert result["outcome"] == "malformed_upstream", result
    assert result["error_type"] == errors.UNEXPECTED_SHAPE, result


def test_a_first_transit_departure_before_the_itinerary_start_is_malformed():
    legs = [
        synthetic_leg("RAIL", frm="A", to="X", start="2026-10-27T09:00:00Z",
                      end="2026-10-27T09:30:00Z", duration=1800),
        synthetic_leg("RAIL", frm="X", to="B", start="2026-10-27T09:30:00Z",
                      end="2026-10-27T12:00:00Z", duration=9000),
    ]
    body = synthetic_plan(legs, start="2026-10-27T10:00:00Z", end="2026-10-27T12:00:00Z")
    assert_unexpected_shape(evaluate(body))


def test_a_last_transit_arrival_after_the_itinerary_end_is_malformed():
    legs = [
        synthetic_leg("RAIL", start=SYNTH_START, end="2026-10-27T10:30:00Z", duration=5400),
    ]
    assert_unexpected_shape(evaluate(synthetic_plan(legs)))


@pytest.mark.parametrize("boundary", ["first-departure", "last-arrival"])
def test_itinerary_bounds_use_the_endpoint_instants(boundary):
    if boundary == "first-departure":
        leg = synthetic_leg("RAIL", departure="2026-10-27T08:50:00Z",
                            end="2026-10-27T09:30:00Z", duration=2400)
    else:
        leg = synthetic_leg("RAIL", start="2026-10-27T09:30:00Z",
                            arrival="2026-10-27T10:10:00Z", duration=2400)
    assert_unexpected_shape(evaluate(synthetic_plan([leg])))


@pytest.mark.parametrize("instant_field", ["startTime", "departure"])
def test_leg_order_is_checked_on_the_instants_the_mapper_uses(instant_field):
    first = synthetic_leg("RAIL", frm="A", to="X", start="2026-10-27T10:30:00Z",
                          end="2026-10-27T10:45:00Z", duration=900)
    second = synthetic_leg("RAIL", frm="X", to="B", end="2026-10-27T10:10:00Z",
                           duration=600, arrival="2026-10-27T10:10:00Z")
    if instant_field == "startTime":
        second["startTime"] = "2026-10-27T10:00:00Z"
    else:
        second["from"]["departure"] = "2026-10-27T10:00:00Z"
    body = synthetic_plan([first, second], start="2026-10-27T10:00:00Z",
                          end="2026-10-27T12:00:00Z")
    assert_unexpected_shape(evaluate(body))


@pytest.mark.parametrize("start_field", ["startTime", "departure"])
def test_a_leg_ending_before_it_starts_on_effective_instants_is_malformed(start_field):
    if start_field == "startTime":
        leg = synthetic_leg("RAIL", start="2026-10-27T09:30:00Z",
                            arrival="2026-10-27T09:10:00Z", duration=600)
    else:
        leg = synthetic_leg("RAIL", departure="2026-10-27T09:30:00Z",
                            arrival="2026-10-27T09:10:00Z", duration=600)
    assert_unexpected_shape(evaluate(synthetic_plan([leg])))


@pytest.mark.parametrize(
    "start_field,end_field",
    [("departure", "arrival"), ("startTime", "arrival"), ("departure", "endTime")],
)
def test_chronological_endpoint_only_instants_are_accepted(start_field, end_field):
    fields = {}
    if start_field == "departure":
        fields["departure"] = "2026-10-27T09:10:00Z"
    else:
        fields["start"] = "2026-10-27T09:10:00Z"
    if end_field == "arrival":
        fields["arrival"] = "2026-10-27T09:50:00Z"
    else:
        fields["end"] = "2026-10-27T09:50:00Z"
    leg = synthetic_leg("RAIL", **fields)
    result = run(AYR_NCL_COORD, body=synthetic_plan([leg]))
    itinerary = itinerary_of(result)
    assert itinerary["elapsed_seconds"] == 3600
    assert itinerary["access_seconds"] == 600
    assert itinerary["egress_seconds"] == 600
    assert itinerary["transfer_walk_seconds"] == 0
    assert itinerary["in_transit_seconds"] == 2400
    assert itinerary["waiting_seconds"] == 0
    assert itinerary["station_to_station_seconds"] == 2400
    departure = datetime(2026, 10, 27, 9, 10, tzinfo=UTC)
    arrival = datetime(2026, 10, 27, 9, 50, tzinfo=UTC)
    assert itinerary["legs"][0]["departure"]["epoch_seconds"] == int(departure.timestamp())
    assert itinerary["legs"][0]["arrival"]["epoch_seconds"] == int(arrival.timestamp())


def test_boundary_equal_instants_and_a_zero_duration_are_valid():
    leg = synthetic_leg("RAIL", start=SYNTH_START, end=SYNTH_END, duration=0)
    result = run(AYR_NCL_COORD, body=synthetic_plan([leg]))
    itinerary = itinerary_of(result)
    assert itinerary["access_seconds"] == 0
    assert itinerary["egress_seconds"] == 0
    assert itinerary["in_transit_seconds"] == 0
    assert itinerary["waiting_seconds"] == 3600
    assert itinerary["duration_basis"] == "transit_itinerary"


@pytest.mark.parametrize(
    "instants,expected_access",
    [({"start": "2026-10-27T09:10:00Z"}, 600), ({}, None)],
    ids=["end-missing", "both-instants-missing"],
)
def test_a_leg_with_missing_instants_stays_partial_and_warns(instants, expected_access):
    leg = synthetic_leg("RAIL", duration=1800, **instants)
    result = run(AYR_NCL_COORD, body=synthetic_plan([leg]))
    itinerary = itinerary_of(result)
    assert itinerary["access_seconds"] == expected_access
    assert itinerary["egress_seconds"] is None
    assert itinerary["in_transit_seconds"] == 1800
    assert itinerary["station_to_station_seconds"] is None
    assert itinerary["waiting_seconds"] is None
    assert itinerary["duration_basis"] == "incomplete"
    assert "duration_components_incomplete" in [w["code"] for w in result["warnings"]]


@pytest.mark.parametrize(
    "mode,duration",
    [
        ("RAIL", -600),
        ("WALK", -600),
        ("TRANSIT", -0.5),
        ("WALK", -0.0009),
        ("RAIL", -1e-09),
    ],
)
def test_a_negative_declared_leg_duration_is_malformed(mode, duration):
    leg = synthetic_leg(mode, start=SYNTH_START, end=SYNTH_END, duration=duration)
    assert_unexpected_shape(evaluate(synthetic_plan([leg])))


def test_a_direct_alternative_with_a_negative_declared_duration_is_malformed():
    leg = synthetic_leg("RAIL", start=SYNTH_START, end="2026-10-27T09:30:00Z", duration=-600)
    body = synthetic_plan(itineraries=[], direct=[synthetic_itinerary([leg])])
    assert_unexpected_shape(evaluate(body))


def test_a_direct_alternative_outside_its_itinerary_bounds_is_malformed():
    leg = synthetic_leg("RAIL", start="2026-10-27T08:30:00Z",
                        end="2026-10-27T09:00:00Z", duration=1800)
    body = synthetic_plan(itineraries=[], direct=[synthetic_itinerary([leg])])
    assert_unexpected_shape(evaluate(body))


# ── §3.1 known-instant ordering across *partial* records ──────────
#
# A partial body is accepted only while its known instants agree. The §3.1
# quantities fix which relations matter, and only those: `access_seconds` is the
# first transit departure minus the itinerary start, `egress_seconds` is the
# itinerary end minus the last transit arrival, and `station_to_station_seconds`
# is their difference — so each of the three terms must be ordered even when some
# other timestamp is absent. Missing inputs are never filled, clamped or sorted;
# a body whose necessary inputs are absent stays `incomplete` with `null`
# components, which is why the exhaustive grid below asserts acceptance for every
# non-contradictory slot combination rather than only rejection.

PARENT_START = "2026-10-27T10:00:00Z"
PARENT_END = "2026-10-27T12:00:00Z"


def parent_case_legs():
    """The reviewer's partial counterexample: the last leg's start is missing."""
    return [
        synthetic_leg("RAIL", frm="A", to="X", start="2026-10-27T10:30:00Z",
                      end="2026-10-27T10:45:00Z", duration=900),
        synthetic_leg("RAIL", frm="X", to="B", end="2026-10-27T10:10:00Z", duration=600),
    ]


@pytest.mark.parametrize(
    "variant", ["top-level", "endpoint-fallback", "direct-alternative"]
)
def test_a_last_transit_arrival_before_the_first_departure_is_malformed(variant):
    """Both instants are known, so their contradiction is decidable.

    The pre-fix body returned ``ok`` with ``station_to_station_seconds = -1200``:
    the missing last start blinded the own-leg check, the monotonic-start check
    skipped the leg, and each one-sided bound passed on its own.
    """
    legs = parent_case_legs()
    if variant == "endpoint-fallback":
        legs[0]["from"]["departure"] = legs[0].pop("startTime")
    if variant == "direct-alternative":
        body = synthetic_plan(
            itineraries=[],
            direct=[synthetic_itinerary(legs, start=PARENT_START, end=PARENT_END)],
        )
    else:
        body = synthetic_plan(legs, start=PARENT_START, end=PARENT_END)
    assert_unexpected_shape(evaluate(body))


def test_the_parent_case_shape_is_accepted_when_its_known_instants_agree():
    """Positive control: the same missing start, with an ordered arrival."""
    legs = parent_case_legs()
    legs[1]["endTime"] = "2026-10-27T10:50:00Z"
    result = run(AYR_NCL_COORD, body=synthetic_plan(legs, start=PARENT_START, end=PARENT_END))
    itinerary = itinerary_of(result)
    assert itinerary["access_seconds"] == 1800
    assert itinerary["egress_seconds"] == 4200
    assert itinerary["station_to_station_seconds"] == 1200
    # in-transit 1500 exceeds the 1200 station-to-station span: the sanctioned
    # negative remainder is null with the inconsistency warning, not malformed.
    assert itinerary["waiting_seconds"] is None
    assert "duration_decomposition_inconsistent" in [w["code"] for w in result["warnings"]]


@pytest.mark.parametrize("instant_field", ["startTime", "departure"])
def test_a_first_transit_departure_after_the_itinerary_end_is_malformed(instant_field):
    if instant_field == "startTime":
        leg = synthetic_leg("RAIL", start="2026-10-27T10:10:00Z", duration=600)
    else:
        leg = synthetic_leg("RAIL", departure="2026-10-27T10:10:00Z", duration=600)
    assert_unexpected_shape(evaluate(synthetic_plan([leg])))


@pytest.mark.parametrize("instant_field", ["endTime", "arrival"])
def test_a_last_transit_arrival_before_the_itinerary_start_is_malformed(instant_field):
    if instant_field == "endTime":
        leg = synthetic_leg("RAIL", end="2026-10-27T08:50:00Z", duration=600)
    else:
        leg = synthetic_leg("RAIL", arrival="2026-10-27T08:50:00Z", duration=600)
    assert_unexpected_shape(evaluate(synthetic_plan([leg])))


def test_known_transit_instants_may_equal_the_itinerary_boundaries():
    """The interval relations are inclusive, so both endpoints stay valid."""
    leg = synthetic_leg("RAIL", start=SYNTH_END, end=SYNTH_END, duration=0)
    result = run(AYR_NCL_COORD, body=synthetic_plan([leg]))
    itinerary = itinerary_of(result)
    assert itinerary["access_seconds"] == 3600
    assert itinerary["egress_seconds"] == 0
    assert itinerary["station_to_station_seconds"] == 0
    assert itinerary["waiting_seconds"] == 0


def test_a_first_departure_and_last_arrival_may_be_the_same_instant():
    legs = [
        synthetic_leg("RAIL", frm="A", to="X", start="2026-10-27T09:30:00Z",
                      end="2026-10-27T09:30:00Z", duration=0),
        synthetic_leg("RAIL", frm="X", to="B", start="2026-10-27T09:30:00Z",
                      end="2026-10-27T09:30:00Z", duration=0),
    ]
    result = run(AYR_NCL_COORD, body=synthetic_plan(legs))
    itinerary = itinerary_of(result)
    assert itinerary["access_seconds"] == 1800
    assert itinerary["egress_seconds"] == 1800
    assert itinerary["station_to_station_seconds"] == 0
    assert itinerary["waiting_seconds"] == 0
    assert itinerary["duration_basis"] == "transit_itinerary"


@pytest.mark.parametrize(
    "first_start,last_end",
    [(None, None), ("2026-10-27T09:10:00Z", None), (None, "2026-10-27T09:50:00Z")],
    ids=["both-missing", "departure-known", "arrival-known"],
)
def test_a_two_leg_itinerary_with_missing_known_instants_stays_partial(first_start, last_end):
    legs = [
        synthetic_leg("RAIL", frm="A", to="X", duration=600),
        synthetic_leg("RAIL", frm="X", to="B", duration=600),
    ]
    if first_start is not None:
        legs[0]["startTime"] = first_start
    if last_end is not None:
        legs[1]["endTime"] = last_end
    result = run(AYR_NCL_COORD, body=synthetic_plan(legs))
    itinerary = itinerary_of(result)
    assert itinerary["access_seconds"] == (None if first_start is None else 600)
    assert itinerary["egress_seconds"] == (None if last_end is None else 600)
    assert itinerary["transfer_walk_seconds"] == 0
    assert itinerary["in_transit_seconds"] == 1200
    assert itinerary["station_to_station_seconds"] is None
    assert itinerary["waiting_seconds"] is None
    assert itinerary["duration_basis"] == "incomplete"
    assert "duration_components_incomplete" in [w["code"] for w in result["warnings"]]


# The grid's instant slots, as minute offsets from the itinerary start: absent, one
# before the interval, two inside it and one after it. Two inside values are needed
# so that a first departure after the last arrival is expressible with both instants
# inside the itinerary interval — the parent case's relation — rather than only as a
# bound violation.
_GRID_START = "2026-10-27T09:00:00Z"
_GRID_END = "2026-10-27T10:00:00Z"
_GRID_BASE = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
_GRID_MINUTES = 60
_GRID_SLOTS = (None, -10, 10, 50, 70)
_GRID_VARIANTS = ("top-level", "endpoint")
_GRID_CASES = len(_GRID_SLOTS) ** 4 * len(_GRID_VARIANTS)


def grid_iso(offset):
    """The RFC 3339 instant ``offset`` minutes after the itinerary start."""
    return (_GRID_BASE + timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def grid_body(instants, variant):
    """Two transit legs with the given effective instants.

    ``endpoint`` expresses the first transit departure through ``from.departure``
    and the last transit arrival through ``to.arrival``, the fallbacks the
    consumers actually read; ``top-level`` uses ``startTime``/``endTime``.
    """
    first_start, first_end, last_start, last_end = instants
    first = synthetic_leg("RAIL", frm="A", to="X", duration=600)
    last = synthetic_leg("RAIL", frm="X", to="B", duration=600)
    if first_start is not None:
        if variant == "endpoint":
            first["from"]["departure"] = grid_iso(first_start)
        else:
            first["startTime"] = grid_iso(first_start)
    if last_end is not None:
        if variant == "endpoint":
            last["to"]["arrival"] = grid_iso(last_end)
        else:
            last["endTime"] = grid_iso(last_end)
    if first_end is not None:
        first["endTime"] = grid_iso(first_end)
    if last_start is not None:
        last["startTime"] = grid_iso(last_start)
    return synthetic_plan([first, last], start=_GRID_START, end=_GRID_END)


def grid_contradiction(instants):
    """The relation a body violates, derived from the §3.1 definitions alone.

    With ``start = 0`` and ``end = 60`` minutes, ``access`` = first departure,
    ``egress`` = 60 − last arrival and ``station_to_station`` = elapsed − access −
    egress = last arrival − first departure; each is a component that must be
    non-negative, and the first departure / last arrival are the instants the
    itinerary has to contain. A leg's own duration and the ordering of the leg
    starts are the remaining quantity inputs. Returns ``None`` when every known
    relation holds.
    """
    first_start, first_end, last_start, last_end = instants
    if first_start is not None and not 0 <= first_start <= _GRID_MINUTES:
        return "first-departure-bound"
    if last_end is not None and not 0 <= last_end <= _GRID_MINUTES:
        return "last-arrival-bound"
    if first_start is not None and last_end is not None and last_end < first_start:
        return "first-after-last"
    if first_start is not None and first_end is not None and first_end < first_start:
        return "leg-order-first"
    if last_start is not None and last_end is not None and last_end < last_start:
        return "leg-order-last"
    known_starts = [value for value in (first_start, last_start) if value is not None]
    if known_starts != sorted(known_starts):
        return "leg-start-order"
    return None


def grid_expected(instants):
    """The components §3.1 defines for a two-transit-leg body, from its instants."""
    first_start, _, _, last_end = instants
    elapsed = _GRID_MINUTES * 60
    access = None if first_start is None else first_start * 60
    egress = None if last_end is None else (_GRID_MINUTES - last_end) * 60
    transfer_walk = 0
    in_transit = 1200
    station = None if access is None or egress is None else elapsed - access - egress
    waiting = None
    if station is not None:
        remainder = station - transfer_walk - in_transit
        waiting = remainder if remainder >= 0 else None
    return {
        "elapsed_seconds": elapsed,
        "access_seconds": access,
        "egress_seconds": egress,
        "transfer_walk_seconds": transfer_walk,
        "in_transit_seconds": in_transit,
        "station_to_station_seconds": station,
        "waiting_seconds": waiting,
    }


def test_a_two_transit_leg_instant_grid_never_emits_a_negative_component():
    """Exhaustive bounded grid over every known/absent instant combination.

    Every contradictory body must be rejected and every other body must be
    accepted with exactly the §3.1 components — so "reject everything partial"
    cannot pass, and a missed relation cannot emit a negative quantity. The
    date/offset quantities are deliberately excluded from the non-negativity
    invariant: a negative ``utc_offset_seconds``, a negative
    ``arrival_date_offset_days`` (westbound date line) and a pre-epoch
    ``epoch_seconds`` are all legitimate.
    """
    cases = 0
    accepted = 0
    rejected = 0
    incomplete = 0
    both_absent = 0
    complete: list = []
    reasons: dict = {}
    accepted_by_variant: dict = {}
    for instants in itertools.product(_GRID_SLOTS, repeat=4):
        for variant in _GRID_VARIANTS:
            cases += 1
            contradiction = grid_contradiction(instants)
            result = evaluate(grid_body(instants, variant))
            label = f"{variant} {instants}"
            if contradiction is not None:
                rejected += 1
                reasons[contradiction] = reasons.get(contradiction, 0) + 1
                assert result["outcome"] == "malformed_upstream", (label, contradiction, result)
                assert result["error_type"] == errors.UNEXPECTED_SHAPE, (label, result)
                continue
            assert result["outcome"] == "ok", (label, result.get("reason"), result)
            accepted += 1
            itinerary = itinerary_of(result)
            expected = grid_expected(instants)
            observed = {name: itinerary[name] for name in expected}
            assert observed == expected, (label, observed, expected)
            for name, value in observed.items():
                assert value is None or value >= 0, (label, name, value)
            assert itinerary["elapsed_seconds"] == 3600, (label, itinerary["elapsed_seconds"])
            codes = {w["code"] for w in result["warnings"]}
            if expected["access_seconds"] is None or expected["egress_seconds"] is None:
                incomplete += 1
                if expected["access_seconds"] is None and expected["egress_seconds"] is None:
                    both_absent += 1
                assert itinerary["duration_basis"] == "incomplete", (label, itinerary["duration_basis"])
                assert "duration_components_incomplete" in codes, (label, codes)
            else:
                complete.append(label)
                assert itinerary["duration_basis"] == "transit_itinerary", (label, codes)
                if expected["waiting_seconds"] is None:
                    assert "duration_decomposition_inconsistent" in codes, (label, codes)
            accepted_by_variant[variant] = accepted_by_variant.get(variant, 0) + 1
    assert cases == _GRID_CASES == 1250
    assert (accepted, rejected) == (222, 1028), (accepted, rejected)
    assert reasons == {
        "first-departure-bound": 500,
        "last-arrival-bound": 300,
        "first-after-last": 50,
        "leg-order-first": 70,
        "leg-order-last": 60,
        "leg-start-order": 48,
    }, reasons
    # Positive controls: partial bodies stay supported, not rejected wholesale.
    assert (incomplete, both_absent, len(complete)) == (170, 50, 52)
    # Both instant spellings accept the same bodies: the guard reads the effective
    # pair, so a fallback-only instant is as usable as a top-level one.
    assert accepted_by_variant == {"top-level": 111, "endpoint": 111}, accepted_by_variant
    assert complete, "no complete itinerary was accepted"


def test_a_direct_alternative_is_checked_for_known_instant_ordering():
    """The same relations hold for `direct` alternatives, not just itineraries."""
    legs = [
        synthetic_leg("RAIL", frm="A", to="X", start="2026-10-27T10:30:00Z",
                      end="2026-10-27T10:45:00Z", duration=900),
        synthetic_leg("RAIL", frm="X", to="B", end="2026-10-27T10:10:00Z", duration=600),
    ]
    body = synthetic_plan(
        itineraries=[],
        direct=[synthetic_itinerary(legs, start=PARENT_START, end=PARENT_END)],
    )
    assert_unexpected_shape(evaluate(body))
