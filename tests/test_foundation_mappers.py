"""Foundation mappers: duration decomposition, calendar facts, places, timestamps.

These three boundaries are pure functions over already-fetched payloads, so the
whole file runs against the recorded bodies with no transport at all. The §3.1
vectors are asserted to the second, and each quantity is checked to have been
derived the way the specification derives it — not merely to add up.
"""

import copy
from datetime import datetime, timedelta, timezone

import pytest

import fixture_support as fs

from transit_core import durations, errors, places, rendering

UTC = timezone.utc


def aware(y, m, d, hh=0, mm=0, ss=0, offset_hours=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone(timedelta(hours=offset_hours)))


# ── the three recorded vectors (§3.1) ──────────────────────────────

AYR_NCL = (14100, 240, 60, 240, 12240, 1320, 13800)
GLW_EDI = (4620, 660, 300, 240, 3300, 120, 3660)
NCL_AYR = (34380, 600, 240, 360, 12420, 20760, 33540)

VECTOR_KEYS = (
    "elapsed_seconds",
    "access_seconds",
    "egress_seconds",
    "transfer_walk_seconds",
    "in_transit_seconds",
    "waiting_seconds",
    "station_to_station_seconds",
)


@pytest.mark.parametrize(
    "fixture, expected",
    [
        ("plan_ayr_ncl_coord.json", AYR_NCL),
        ("plan_glw_edi_coord.json", GLW_EDI),
        ("plan_ncl_ayr_depart.json", NCL_AYR),
    ],
)
def test_recorded_vectors_decompose_to_the_second(fixture, expected):
    itinerary = fs.load(fixture)["itineraries"][0]
    breakdown = durations.decompose_itinerary(itinerary)
    obj = breakdown.to_object()
    assert tuple(obj[key] for key in VECTOR_KEYS) == expected
    assert obj["duration_basis"] == "transit_itinerary"
    assert obj["warnings"] == []


def test_access_is_measured_from_the_first_transit_leg_not_the_first_leg():
    """Glasgow→Edinburgh itinerary 0 starts with a 660 s walk before any service."""
    itinerary = fs.load("plan_glw_edi_coord.json")["itineraries"][0]
    assert itinerary["legs"][0]["mode"] == "WALK"
    assert itinerary["legs"][0]["duration"] == 660
    breakdown = durations.decompose_itinerary(itinerary).to_object()
    assert breakdown["access_seconds"] == 660          # the walk, not zero
    assert breakdown["in_transit_seconds"] == 3300     # SUBWAY 120 + RAIL 3180


def test_transfer_walking_is_summed_leg_by_leg_and_never_taken_as_a_residual():
    """Ayr→Newcastle has two 120 s internal walks, one of them a dummy leg."""
    itinerary = fs.load("plan_ayr_ncl_coord.json")["itineraries"][0]
    legs = itinerary["legs"]
    transit_positions = [i for i, leg in enumerate(legs) if durations.is_transit_mode(leg["mode"])]
    internal = legs[transit_positions[0] + 1: transit_positions[-1]]
    internal_walks = [leg for leg in internal if leg["mode"] == "WALK"]
    assert [leg["duration"] for leg in internal_walks] == [120, 120]
    assert [leg.get("cancelled") for leg in internal_walks] == [True, None]
    breakdown = durations.decompose_itinerary(itinerary).to_object()
    assert breakdown["transfer_walk_seconds"] == 240
    assert breakdown["station_to_station_seconds"] == 13800
    # station-to-station deliberately keeps the transfer walking inside it
    assert (
        breakdown["elapsed_seconds"]
        - breakdown["access_seconds"]
        - breakdown["egress_seconds"]
        == breakdown["station_to_station_seconds"]
    )


def test_the_three_vectors_conserve_elapsed_exactly():
    for fixture in ("plan_ayr_ncl_coord.json", "plan_glw_edi_coord.json", "plan_ncl_ayr_depart.json"):
        breakdown = durations.decompose_itinerary(fs.load(fixture)["itineraries"][0])
        assert breakdown.conserves, fixture
        total = (
            breakdown.access_seconds
            + breakdown.egress_seconds
            + breakdown.transfer_walk_seconds
            + breakdown.in_transit_seconds
            + breakdown.waiting_seconds
        )
        assert total == breakdown.elapsed_seconds


@pytest.mark.parametrize(
    "fixture",
    ["plan_ayr_ncl_coord.json", "plan_glw_ncl_walk60.json", "plan_ams_ber.json", "plan_ber_muc.json"],
)
def test_every_itinerary_in_a_recorded_body_conserves(fixture):
    for index, itinerary in enumerate(fs.load(fixture)["itineraries"]):
        breakdown = durations.decompose_itinerary(itinerary)
        assert breakdown.duration_basis == "transit_itinerary", (fixture, index)
        assert breakdown.conserves, (fixture, index)
        assert breakdown.waiting_seconds >= 0, (fixture, index)


def test_elapsed_comes_from_instants_not_from_clock_faces():
    # 09:01Z → 12:56Z is 14100 s whatever the legs claim to add up to
    itinerary = fs.load("plan_ayr_ncl_coord.json")["itineraries"][0]
    breakdown = durations.decompose_itinerary(itinerary)
    assert breakdown.elapsed_seconds == 14100
    leg_total = sum(leg["duration"] for leg in itinerary["legs"])
    assert leg_total == 12780                    # deliberately not the elapsed time
    assert breakdown.elapsed_seconds != leg_total


# ── incomplete and inconsistent inputs ─────────────────────────────


def test_a_missing_internal_walk_duration_nulls_the_component_and_warns():
    itinerary = copy.deepcopy(fs.load("plan_ayr_ncl_coord.json")["itineraries"][0])
    internal = [
        leg
        for leg in itinerary["legs"]
        if leg["mode"] == "WALK" and leg["duration"] == 120 and leg.get("cancelled") is True
    ]
    assert len(internal) == 1
    # remove the declared duration *and* the instants it could be derived from
    internal[0].pop("duration")
    internal[0].pop("startTime")
    internal[0].pop("endTime")
    internal[0]["from"].pop("departure")
    internal[0]["to"].pop("arrival")
    breakdown = durations.decompose_itinerary(itinerary).to_object()
    assert breakdown["transfer_walk_seconds"] is None
    assert breakdown["waiting_seconds"] is None
    assert breakdown["duration_basis"] == "incomplete"
    assert "duration_components_incomplete" in breakdown["warnings"]
    # the components that do not need the walk are still reported, never zeroed
    assert breakdown["access_seconds"] == 240
    assert breakdown["in_transit_seconds"] == 12240


def test_a_missing_instant_nulls_only_the_components_that_need_it():
    itinerary = copy.deepcopy(fs.load("plan_glw_edi_coord.json")["itineraries"][0])
    itinerary.pop("endTime")
    itinerary["legs"][-1]["to"].pop("arrival")
    itinerary["legs"][-1].pop("endTime")
    breakdown = durations.decompose_itinerary(itinerary).to_object()
    assert breakdown["egress_seconds"] is None
    assert breakdown["waiting_seconds"] is None
    assert breakdown["access_seconds"] == 660
    assert "duration_components_incomplete" in breakdown["warnings"]


def test_a_negative_remainder_is_null_with_a_warning_not_a_negative_number():
    legs = [
        {
            "mode": "REGIONAL_RAIL",
            "duration": 1000,                      # claims 1000 s inside a 600 s journey
            "from": {"name": "A", "departure": "2026-10-27T09:00:00Z"},
            "to": {"name": "B", "arrival": "2026-10-27T09:10:00Z"},
        }
    ]
    breakdown = durations.decompose(
        legs, start=aware(2026, 10, 27, 9, 0), end=aware(2026, 10, 27, 9, 10)
    ).to_object()
    assert breakdown["elapsed_seconds"] == 600
    assert breakdown["in_transit_seconds"] == 1000
    assert breakdown["waiting_seconds"] is None
    assert "duration_decomposition_inconsistent" in breakdown["warnings"]
    assert breakdown["waiting_seconds"] != 0


def test_a_street_only_itinerary_never_reports_transit_components_or_zero_changes():
    direct = fs.load("synthetic_plan_direct_alternatives.json")["direct"][0]
    breakdown = durations.decompose_itinerary(direct).to_object()
    assert breakdown["duration_basis"] == "street_only"
    assert breakdown["elapsed_seconds"] == 12600
    assert breakdown["in_transit_seconds"] == 0
    for key in ("access_seconds", "egress_seconds", "transfer_walk_seconds", "station_to_station_seconds", "waiting_seconds"):
        assert breakdown[key] is None, key


def test_duration_basis_object_has_the_documented_shape():
    breakdown = durations.decompose_itinerary(fs.load("plan_glw_edi_coord.json")["itineraries"][0]).to_object()
    assert set(breakdown) == set(VECTOR_KEYS) | {"duration_basis", "warnings"}
    assert isinstance(breakdown["warnings"], list)


# ── response mode classification (§3.1, §3.3, §4.13) ───────────────

# Synthetic input, not a corpus fixture: the transit list of the pinned
# `Mode` enum in openapi-v2.10.2.yaml lines 3440-3460, transcribed by hand.
PINNED_TRANSIT_MODES = (
    "TRANSIT",
    "TRAM",
    "SUBWAY",
    "FERRY",
    "AIRPLANE",
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
    "OTHER",
    "AREAL_LIFT",
    "METRO",
    "CABLE_CAR",
)


@pytest.mark.parametrize("mode", PINNED_TRANSIT_MODES)
def test_every_pinned_transit_response_mode_drives_durations_and_cancellation(mode):
    """A transit response mode is never decomposed or labelled as a street leg."""
    # synthetic leg: every pinned transit mode, with a real cancellation flag
    leg = {
        "mode": mode,
        "duration": 600,
        "cancelled": True,
        "from": {"name": "A", "departure": "2026-10-27T09:00:00Z"},
        "to": {"name": "B", "arrival": "2026-10-27T09:10:00Z"},
    }
    assert durations.is_transit_mode(mode) is True
    breakdown = durations.decompose(
        [leg], start=aware(2026, 10, 27, 9, 0), end=aware(2026, 10, 27, 9, 10)
    ).to_object()
    assert breakdown["duration_basis"] == "transit_itinerary"
    assert breakdown["in_transit_seconds"] == 600
    assert breakdown["waiting_seconds"] == 0
    flags = places.leg_cancellation(leg)
    assert flags["cancelled"] is True
    assert flags["walking_route_unavailable"] is False


def test_the_transit_table_is_the_pinned_transit_section_not_the_request_enum():
    assert durations.TRANSIT_MODES == set(PINNED_TRANSIT_MODES)
    # ODM and RIDE_SHARING are marked "Transit | Street" upstream; the shared
    # classification keeps them street, and that decision is documented in
    # durations.py. They are not invented here as transit response modes.
    assert {"ODM", "RIDE_SHARING"}.isdisjoint(durations.TRANSIT_MODES)
    assert {"ODM", "RIDE_SHARING"} <= durations.STREET_MODES
    assert durations.STREET_MODES.isdisjoint(durations.TRANSIT_MODES)


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_leg_duration_is_absent_not_a_crash(duration):
    # synthetic: no recorded body carries a non-finite duration
    leg = {"mode": "BUS", "duration": duration}
    breakdown = durations.decompose(
        [leg], start=aware(2026, 10, 27, 9, 0), end=aware(2026, 10, 27, 9, 10)
    ).to_object()
    assert breakdown["in_transit_seconds"] is None
    assert breakdown["waiting_seconds"] is None
    assert breakdown["duration_basis"] == "incomplete"
    assert "duration_components_incomplete" in breakdown["warnings"]


def test_a_non_finite_duration_falls_back_to_valid_leg_instants():
    leg = {
        "mode": "BUS",
        "duration": float("nan"),
        "from": {"name": "A", "departure": "2026-10-27T09:00:00Z"},
        "to": {"name": "B", "arrival": "2026-10-27T09:10:00Z"},
    }
    breakdown = durations.decompose(
        [leg], start=aware(2026, 10, 27, 9, 0), end=aware(2026, 10, 27, 9, 10)
    ).to_object()
    assert breakdown["in_transit_seconds"] == 600
    assert breakdown["duration_basis"] == "transit_itinerary"
    assert breakdown["warnings"] == []


def test_a_huge_integer_duration_is_preserved_without_a_float_conversion():
    huge = 10 ** 400
    leg = {
        "mode": "BUS",
        "duration": huge,
        "from": {"name": "A", "departure": "2026-10-27T09:00:00Z"},
        "to": {"name": "B", "arrival": "2026-10-27T09:10:00Z"},
    }
    breakdown = durations.decompose(
        [leg], start=aware(2026, 10, 27, 9, 0), end=aware(2026, 10, 27, 9, 10)
    ).to_object()
    assert breakdown["in_transit_seconds"] == huge
    # a whole-second Python int needs no float conversion, so it cannot overflow
    assert "duration_decomposition_inconsistent" in breakdown["warnings"]


# ── calendar, day offset, overnight ────────────────────────────────


def test_an_overnight_journey_reports_a_one_day_offset_and_overnight():
    plan = fs.load("plan_ncl_ayr_depart.json")
    journey = plan["itineraries"][0]
    facts = durations.calendar_facts(
        aware(2026, 10, 27, 22, 55), aware(2026, 10, 28, 8, 28), "Europe/London", "Europe/London"
    ).to_object()
    assert facts["arrival_date_offset_days"] == 1
    assert facts["has_overnight"] is True
    assert facts["warnings"] == []
    assert journey["startTime"] == "2026-10-27T22:55:00Z"


def test_a_same_day_journey_is_neither_overnight_nor_offset():
    facts = durations.calendar_facts(
        aware(2026, 10, 27, 9, 1), aware(2026, 10, 27, 12, 56), "Europe/London", "Europe/London"
    ).to_object()
    assert facts["arrival_date_offset_days"] == 0
    assert facts["has_overnight"] is False
    assert facts["warnings"] == []


def test_a_westbound_date_line_crossing_is_negative_and_warned():
    itinerary = fs.load("synthetic_westbound_date_line.json")["itineraries"][0]
    facts = durations.calendar_facts(
        timezone_parse(itinerary["startTime"]),
        timezone_parse(itinerary["endTime"]),
        "Pacific/Auckland",
        "Pacific/Honolulu",
    ).to_object()
    assert facts["arrival_date_offset_days"] == -1
    assert facts["warnings"] == ["date_line_crossing"]
    # a negative calendar offset is not an overnight: the origin clock never crosses midnight
    assert facts["has_overnight"] is False


def timezone_parse(value):
    from transit_core import timezones

    return timezones.parse_rfc3339(value)


def test_overnight_is_computed_in_the_origin_zone_only():
    # 23:30 Berlin → 00:30 London is 2 h later and crosses Berlin midnight but not London's
    facts = durations.calendar_facts(
        aware(2026, 10, 27, 21, 30), aware(2026, 10, 27, 23, 30), "Europe/Berlin", "Europe/London"
    ).to_object()
    assert facts["has_overnight"] is True
    # ...while the destination-local date is still 27 October: the two quantities differ
    assert facts["arrival_date_offset_days"] == 0


def test_calendar_without_instants_is_null_with_a_warning():
    facts = durations.calendar_facts(None, None, "Europe/London", "Europe/London").to_object()
    assert facts["arrival_date_offset_days"] is None
    assert facts["has_overnight"] is None
    assert facts["warnings"] == ["calendar_inputs_incomplete"]


OVERNIGHT_START = aware(2026, 10, 27, 22, 55)
OVERNIGHT_END = aware(2026, 10, 28, 8, 28)


@pytest.mark.parametrize(
    "origin_zone, destination_zone, offset, overnight, warnings",
    [
        ("Europe/London", "Europe/London", 1, True, []),
        ("Europe/London", None, None, True, ["endpoint_zone_unavailable"]),
        (None, "Europe/London", None, None, ["endpoint_zone_unavailable"]),
        ("Not/AZone", "Europe/London", None, None, ["endpoint_zone_unavailable"]),
        ("Europe/London", "Not/AZone", None, True, ["endpoint_zone_unavailable"]),
        (None, None, None, None, ["endpoint_zone_unavailable"]),
    ],
)
def test_calendar_quantities_are_independent_of_an_unavailable_display_zone(
    origin_zone, destination_zone, offset, overnight, warnings
):
    """§3.2: ``has_overnight`` needs the origin zone only; the date offset needs both."""
    facts = durations.calendar_facts(
        OVERNIGHT_START, OVERNIGHT_END, origin_zone, destination_zone
    ).to_object()
    assert facts == {
        "arrival_date_offset_days": offset,
        "has_overnight": overnight,
        "warnings": warnings,
    }


def test_missing_instants_and_a_missing_zone_report_both_missing_inputs():
    facts = durations.calendar_facts(None, None, None, "Europe/London").to_object()
    assert facts["arrival_date_offset_days"] is None
    assert facts["has_overnight"] is None
    assert facts["warnings"] == ["calendar_inputs_incomplete", "endpoint_zone_unavailable"]


def test_a_missing_destination_zone_does_not_invent_a_date_line_crossing():
    itinerary = fs.load("synthetic_westbound_date_line.json")["itineraries"][0]
    facts = durations.calendar_facts(
        timezone_parse(itinerary["startTime"]),
        timezone_parse(itinerary["endTime"]),
        "Pacific/Auckland",
        None,
    ).to_object()
    assert facts["arrival_date_offset_days"] is None
    assert facts["has_overnight"] is False
    # no date difference means no evidence for a crossing, and no fabricated warning
    assert facts["warnings"] == ["endpoint_zone_unavailable"]


def test_a_dst_gap_does_not_change_the_overnight_verdict():
    # 23:30 GMT on 28 March becomes 03:00 BST on 29 March at 02:00Z: one local midnight
    facts = durations.calendar_facts(
        aware(2026, 3, 28, 23, 30), aware(2026, 3, 29, 2, 0), "Europe/London", "Europe/London"
    ).to_object()
    assert facts["arrival_date_offset_days"] == 1
    assert facts["has_overnight"] is True
    assert facts["warnings"] == []


def test_naive_instants_are_never_read_in_the_host_zone():
    facts = durations.calendar_facts(
        datetime(2026, 10, 27, 22, 55), datetime(2026, 10, 28, 8, 28),
        "Europe/London", "Europe/London",
    ).to_object()
    assert facts["arrival_date_offset_days"] is None
    assert facts["has_overnight"] is None
    assert facts["warnings"] == ["calendar_inputs_incomplete"]


# ── canonical timestamp object (§2.5) ──────────────────────────────

TIMESTAMP_KEYS = {
    "iso",
    "epoch_seconds",
    "local",
    "local_date",
    "utc_offset_seconds",
    "zone",
    "zone_source",
    "display",
}


def test_timestamp_renders_exactly_the_eight_documented_fields_in_the_display_zone():
    instant = aware(2026, 10, 23, 16, 36)
    stamp = rendering.timestamp(instant, zone="Europe/London")
    assert set(stamp) == TIMESTAMP_KEYS
    assert stamp["iso"] == "2026-10-23T17:36:00+01:00"
    assert stamp["local"] == "2026-10-23T17:36:00"
    assert stamp["local_date"] == "2026-10-23"
    assert stamp["utc_offset_seconds"] == 3600
    assert stamp["zone"] == "Europe/London"
    assert stamp["zone_source"] == "response_place"
    assert stamp["epoch_seconds"] == int(instant.timestamp())
    assert stamp["display"] == "2026-10-23 17:36 +01:00 (Europe/London)"


def test_timestamp_without_a_zone_keeps_the_true_offset_and_says_so():
    instant = datetime(2026, 10, 23, 17, 36, tzinfo=timezone(timedelta(hours=1)))
    stamp = rendering.timestamp(instant, zone=None)
    assert stamp["zone"] is None
    assert stamp["zone_source"] == "unavailable"
    assert stamp["utc_offset_seconds"] == 3600
    assert stamp["iso"] == "2026-10-23T17:36:00+01:00"
    assert stamp["display"] == "2026-10-23 17:36 +01:00 (zone unavailable)"


def test_a_utc_instant_is_never_passed_off_as_a_local_rendering():
    instant = aware(2026, 10, 23, 17, 36)                 # 17:36Z
    unavailable = rendering.timestamp(instant, zone=None)
    assert unavailable["local"] == "2026-10-23T17:36:00"
    assert unavailable["utc_offset_seconds"] == 0
    london = rendering.timestamp(instant, zone="Europe/London")
    assert london["local"] == "2026-10-23T18:36:00"       # GMT+1 until 25 October
    assert london["iso"] != unavailable["iso"]


def test_timestamp_offset_formatting_and_halves():
    assert rendering.format_offset(0) == "+00:00"
    assert rendering.format_offset(3600) == "+01:00"
    assert rendering.format_offset(-18000) == "-05:00"
    assert rendering.format_offset(19800) == "+05:30"
    stamp = rendering.timestamp(aware(2026, 10, 27, 6, 30), zone="Asia/Kolkata")
    assert stamp["iso"] == "2026-10-27T12:00:00+05:30"
    assert stamp["utc_offset_seconds"] == 19800


def test_a_naive_instant_is_a_programming_error_not_a_rendered_guess():
    with pytest.raises(ValueError):
        rendering.timestamp(datetime(2026, 10, 23, 17, 36))


# ── place mapping and dummy-walk provenance (§4.13) ────────────────

PLACE_KEYS = {
    "name",
    "stop_id",
    "parent_id",
    "lat",
    "lon",
    "zone",
    "zone_source",
    "vertex_type",
    "modes",
    "stop_code",
    "stop_cancelled",
    "source_cancelled",
    "source_cancelled_origin",
}


def ayr_itinerary():
    return fs.load("plan_ayr_ncl_coord.json")["itineraries"][0]


def test_the_dummy_walk_predicate_tests_the_points_not_the_geometry_object():
    legs = ayr_itinerary()["legs"]
    dummy_start = legs[0]
    assert dummy_start["mode"] == "WALK" and dummy_start["cancelled"] is True
    assert dummy_start["legGeometry"] == {"points": "", "precision": 6, "length": 0}
    assert places.is_dummy_walk(dummy_start) is True

    real_walk = legs[4]
    assert real_walk["mode"] == "WALK" and "cancelled" not in real_walk
    assert real_walk["legGeometry"]["points"] != ""
    assert places.is_dummy_walk(real_walk) is False


def test_a_walk_flagged_cancelled_with_real_geometry_is_not_a_dummy_walk():
    leg = {
        "mode": "WALK",
        "cancelled": True,
        "duration": 120,
        "legGeometry": {"points": "abc123", "precision": 6, "length": 40},
    }
    assert places.is_dummy_walk(leg) is False


def test_a_walk_with_no_geometry_at_all_is_a_dummy_walk():
    leg = {"mode": "WALK", "cancelled": True, "duration": 120}
    assert places.is_dummy_walk(leg) is True


def test_a_transit_leg_is_never_a_dummy_walk_even_with_an_empty_polyline():
    leg = {"mode": "REGIONAL_RAIL", "cancelled": True, "duration": 60, "legGeometry": {"points": ""}}
    assert places.is_dummy_walk(leg) is False


@pytest.mark.parametrize(
    "geometry",
    ["abc123", {"points": 123}, {"points": None}, 5, [1, 2]],
)
def test_an_incorrectly_typed_geometry_is_malformed_not_a_dummy_walk(geometry):
    leg = {"mode": "WALK", "cancelled": True, "legGeometry": geometry}
    with pytest.raises(errors.ToolError) as caught:
        places.is_dummy_walk(leg)
    assert caught.value.outcome == errors.MALFORMED_UPSTREAM
    assert caught.value.reason == errors.UNEXPECTED_SHAPE


def test_every_place_in_the_itinerary_is_mapped_with_the_documented_shape():
    pairs = places.map_itinerary_places(ayr_itinerary()["legs"])
    assert len(pairs) == len(ayr_itinerary()["legs"])
    for leg_from, leg_to in pairs:
        assert set(leg_from) == PLACE_KEYS
        assert set(leg_to) == PLACE_KEYS


def test_a_dummy_walk_endpoint_never_claims_the_stop_is_cancelled():
    legs = ayr_itinerary()["legs"]
    pairs = places.map_itinerary_places(legs)
    suppressed = [
        place
        for pair in pairs
        for place in pair
        if place["source_cancelled"] is True
    ]
    assert suppressed, "the recorded itinerary does carry dummy-walk endpoint flags"
    assert len(suppressed) == 4
    for place in suppressed:
        assert place["stop_cancelled"] is None
        assert place["source_cancelled_origin"] == "walking_route_unavailable"
    # and no place anywhere in the itinerary reports a cancelled stop
    assert all(
        place["stop_cancelled"] is not True for pair in pairs for place in pair
    )


def test_the_same_stop_can_appear_with_and_without_a_dummy_endpoint_flag():
    pairs = places.map_itinerary_places(ayr_itinerary()["legs"])
    ayr_occurrences = [
        place
        for pair in pairs
        for place in pair
        if place["stop_id"] == "gb-great-britain_9100AYRR3"
    ]
    assert len(ayr_occurrences) >= 2
    assert any(p["stop_cancelled"] is None for p in ayr_occurrences)
    assert any(p["stop_cancelled"] is False for p in ayr_occurrences)
    # the parent stop id is carried alongside, not substituted for the platform
    assert all(p["parent_id"] == "gb-great-britain_910GAYRR" for p in ayr_occurrences)


def test_a_genuine_cancellation_keeps_stop_cancelled_true():
    plan = fs.load("synthetic_plan_cancelled_transit_leg.json")
    pairs = places.map_itinerary_places(plan["itineraries"][0]["legs"])
    ayr = [place for pair in pairs for place in pair if place["stop_id"] == "gb-great-britain_910GAYRR"]
    assert ayr and all(p["stop_cancelled"] is True for p in ayr)
    assert all(p["source_cancelled_origin"] == "service" for p in ayr)


def test_place_zones_come_from_the_payload_or_are_unavailable():
    pairs = places.map_itinerary_places(ayr_itinerary()["legs"])
    start = pairs[0][0]
    assert start["name"] == "START"
    assert start["zone"] == "Europe/London"          # the recorded leg places do carry tz
    assert start["zone_source"] == "response_place"
    ayr = pairs[0][1]
    assert ayr["zone"] == "Europe/London"
    assert ayr["zone_source"] == "response_place"
    assert ayr["vertex_type"] == "TRANSIT"
    assert ayr["stop_id"] == "gb-great-britain_9100AYRR3"
    # a place the payload left without a zone is rendered as unavailable, not guessed
    envelope_place = fs.load("plan_ams_ber.json")["from"]
    mapped = places.map_place(envelope_place)
    assert mapped["zone"] is None
    assert mapped["zone_source"] == "unavailable"


def test_parent_id_is_mapped_from_a_body_that_carries_it_and_absent_otherwise():
    stoptimes = fs.load("stoptimes_ayr_p3.json")
    mapped = places.map_place(stoptimes["place"])
    assert mapped["parent_id"] == "gb-great-britain_910GAYRR"
    stop = fs.load("stop_metro_central.json")["place"]
    assert places.map_place(stop)["parent_id"] is None


@pytest.mark.parametrize(
    "raw, expected",
    [(True, True), (False, False), (None, None), ("true", None), (1, None), (0, None), ({}, None)],
)
def test_tristate_never_coerces_a_non_boolean(raw, expected):
    assert places.tristate(raw) is expected


def test_a_transit_leg_reports_cancelled_tri_state_from_the_payload_only():
    legs = ayr_itinerary()["legs"]
    assert places.leg_cancellation(legs[1])["cancelled"] is False
    assert places.leg_cancellation({"mode": "REGIONAL_RAIL", "duration": 60})["cancelled"] is None
    cancelled = places.leg_cancellation({"mode": "REGIONAL_RAIL", "cancelled": True, "duration": 60})
    assert cancelled["cancelled"] is True
    assert cancelled["walking_route_unavailable"] is False
    assert cancelled["duration_is_estimate"] is False


def test_a_dummy_walk_is_labelled_as_an_unroutable_estimate_never_as_cancelled_service():
    leg = ayr_itinerary()["legs"][0]
    flags = places.leg_cancellation(leg)
    assert flags["cancelled"] is None
    assert flags["walking_route_unavailable"] is True
    assert flags["duration_is_estimate"] is True
    assert "walking route" in flags["note"]
    assert "estimate" in flags["note"]


def test_a_real_walk_carries_no_walking_failure_flag():
    flags = places.leg_cancellation(ayr_itinerary()["legs"][4])
    assert flags["cancelled"] is None
    assert flags["walking_route_unavailable"] is False
    assert flags["duration_is_estimate"] is False
    assert flags["note"] is None


def test_stop_time_cancellation_maps_both_members_tri_state():
    stop_times = fs.load("synthetic_trip_cancelled_stoptime.json")["stopTimes"]
    mapped = [places.map_stop_time_cancellation(st) for st in stop_times]
    assert mapped == [
        {"stop_cancelled": True, "trip_cancelled": True},
        {"stop_cancelled": True, "trip_cancelled": False},
        {"stop_cancelled": False, "trip_cancelled": False},
    ]
    partial = places.map_stop_time_cancellation({"cancelled": True})
    assert partial == {"stop_cancelled": True, "trip_cancelled": None}
    assert places.map_stop_time_cancellation({}) == {"stop_cancelled": None, "trip_cancelled": None}
