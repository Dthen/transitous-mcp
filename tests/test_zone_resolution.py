"""Frame-authority order and bounded zone discovery (§4.3).

Only one endpoint frame is ever needed, so discovery costs at most one request;
a caller declaration or ``timezone`` costs none. Coordinate inference is accepted
only within 500 m inclusive, is recorded with the stop id and distance, and a
disagreement between two *like* authorities is a typed ``unsupported`` failure
while an explicit declaration is never flagged.
"""

import json
from datetime import datetime, timezone

import pytest

import fixture_support as fs

from transit_core import errors, http, timeparse, timezones

STOP_ID = "gb-great-britain_940GZZTWCST"


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body if isinstance(body, bytes) else body.encode()

    def read(self):
        return self._body

    def close(self):
        pass


def transport(body, calls):
    def _transport(request, timeout):
        calls.append(request.full_url)
        return FakeResponse(200, body)

    return _transport


def stop_body(zone="Europe/London"):
    body = fs.load("synthetic_stop_metro_central.json")
    if zone is None:
        body["place"].pop("tz", None)
    else:
        body["place"]["tz"] = zone
    return json.dumps(body).encode()


def reverse_body(zone, lat, lon, stop_id="gb-test_inferred"):
    match = {
        "type": "STOP",
        "name": "Inferred stop",
        "id": stop_id,
        "lat": lat,
        "lon": lon,
        "tokens": [],
        "areas": [],
        "score": -1.0,
    }
    if zone is not None:
        match["tz"] = zone
    return json.dumps([match]).encode()


def coordinate_at(distance_m, origin=(0.0, 0.0)):
    """Latitude offset whose great-circle distance from the origin is `distance_m`."""
    lo, hi = 0.0, 0.05
    for _ in range(90):
        mid = (lo + hi) / 2
        if timezones.distance_meters(origin[0], origin[1], mid, origin[1]) < distance_m:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2, origin[1]


# ── authority order ────────────────────────────────────────────────


def test_a_caller_declaration_wins_with_zero_requests():
    calls = []
    query = timezones.EndpointQuery.coordinate("origin", 55.458267, -4.625827, declared_zone="Europe/London")
    resolution = timezones.resolve_endpoint_zone(query, transport=transport(b"{}", calls))
    assert resolution.zone == "Europe/London"
    assert resolution.source == "caller_endpoint_timezone"
    assert resolution.requests_issued == 0
    assert calls == []


def test_a_declared_zone_is_validated_like_any_other():
    query = timezones.EndpointQuery.stop("destination", STOP_ID, declared_zone="Nowhere/Land")
    with pytest.raises(errors.ToolError) as caught:
        timezones.resolve_endpoint_zone(query, transport=lambda request, timeout: FakeResponse(200, b"{}"))
    assert caught.value.reason == errors.UNKNOWN_TIMEZONE


def test_a_caller_timezone_is_the_frame_and_ends_discovery():
    choice = timeparse.choose_frame(
        control=timeparse.CONTROL_DEPART_AFTER,
        caller_timezone="Europe/Berlin",
        endpoint_zone="Europe/London",
        endpoint_zone_source="endpoint_stop_info",
    )
    assert (choice.zone, choice.source) == ("Europe/Berlin", "caller_timezone")


def test_the_direction_relevant_endpoint_supplies_the_frame():
    depart = timeparse.choose_frame(
        control=timeparse.CONTROL_DEPART_AFTER,
        origin_zone="Europe/London",
        origin_zone_source="endpoint_stop_info",
    )
    assert (depart.zone, depart.source) == ("Europe/London", "endpoint_stop_info")
    arrive = timeparse.choose_frame(
        control=timeparse.CONTROL_ARRIVE_BY,
        destination_zone="Europe/Berlin",
        destination_zone_source="endpoint_reverse_geocode",
    )
    assert (arrive.zone, arrive.source) == ("Europe/Berlin", "endpoint_reverse_geocode")


def test_no_authority_leaves_the_frame_unresolved():
    choice = timeparse.choose_frame(control=timeparse.CONTROL_ARRIVE_BY)
    assert choice.zone is None


# ── stop metadata path ─────────────────────────────────────────────


def test_a_stop_endpoint_resolves_once_from_stop_metadata():
    calls = []
    query = timezones.EndpointQuery.stop("origin", STOP_ID)
    resolution = timezones.resolve_endpoint_zone(query, transport=transport(stop_body(), calls))
    assert resolution.zone == "Europe/London"
    assert resolution.source == "endpoint_stop_info"
    assert resolution.requests_issued == 1
    assert len(calls) == 1
    assert calls[0] == f"https://api.transitous.org/api/v6/stop?stopId={STOP_ID}"


def test_a_stop_without_a_zone_is_unresolved_not_guessed():
    calls = []
    query = timezones.EndpointQuery.stop("origin", STOP_ID)
    with pytest.raises(errors.ToolError) as caught:
        timezones.resolve_endpoint_zone(query, transport=transport(stop_body(zone=None), calls))
    exc = caught.value
    assert (exc.outcome, exc.reason) == (errors.INVALID_INPUT, errors.TIMEZONE_UNRESOLVED)
    assert exc.details["endpoint"] == "origin"
    assert exc.details["requests_issued"] == 1
    assert "remedy" in exc.details


def test_a_stop_body_of_the_wrong_shape_is_malformed_upstream():
    query = timezones.EndpointQuery.stop("origin", STOP_ID)
    with pytest.raises(errors.ToolError) as caught:
        timezones.resolve_endpoint_zone(query, transport=transport(b"[]", []))
    assert caught.value.outcome == errors.MALFORMED_UPSTREAM


# ── bounded coordinate inference ───────────────────────────────────


def test_coordinate_inference_accepts_exactly_the_500_metre_bound():
    lat, lon = coordinate_at(500.0)
    measured = timezones.distance_meters(0.0, 0.0, lat, lon)
    assert 499.999 < measured <= 500.0, measured
    calls = []
    query = timezones.EndpointQuery.coordinate("origin", 0.0, 0.0)
    resolution = timezones.resolve_endpoint_zone(query, transport=transport(reverse_body("Europe/London", lat, lon), calls))
    assert resolution.zone == "Europe/London"
    assert resolution.source == "endpoint_reverse_geocode"
    assert resolution.requests_issued == 1
    assert resolution.details["stop_id"] == "gb-test_inferred"
    assert resolution.details["distance_m"] <= 500.0
    assert calls[0].startswith("https://api.transitous.org/api/v1/reverse-geocode?")
    assert "place=0.0%2C0.0" in calls[0]
    assert "type=STOP" in calls[0]
    assert "numResults=1" in calls[0]


def test_coordinate_inference_rejects_a_stop_just_beyond_the_bound():
    lat, lon = coordinate_at(500.5)
    measured = timezones.distance_meters(0.0, 0.0, lat, lon)
    assert 500.0 < measured < 501.0, measured
    query = timezones.EndpointQuery.coordinate("destination", 0.0, 0.0)
    with pytest.raises(errors.ToolError) as caught:
        timezones.resolve_endpoint_zone(query, transport=transport(reverse_body("Europe/London", lat, lon), []))
    exc = caught.value
    assert exc.reason == errors.TIMEZONE_UNRESOLVED
    assert exc.details["endpoint"] == "destination"
    assert exc.details["distance_m"] > 500.0


def test_inference_rejects_a_first_result_without_a_zone():
    lat, lon = coordinate_at(100.0)
    query = timezones.EndpointQuery.coordinate("origin", 0.0, 0.0)
    with pytest.raises(errors.ToolError) as caught:
        timezones.resolve_endpoint_zone(query, transport=transport(reverse_body(None, lat, lon), []))
    assert caught.value.reason == errors.TIMEZONE_UNRESOLVED


def test_inference_rejects_an_empty_result_list():
    query = timezones.EndpointQuery.coordinate("origin", 0.0, 0.0)
    with pytest.raises(errors.ToolError) as caught:
        timezones.resolve_endpoint_zone(query, transport=transport(b"[]", []))
    assert caught.value.reason == errors.TIMEZONE_UNRESOLVED


def test_inference_rejects_a_reverse_geocode_body_of_the_wrong_shape():
    query = timezones.EndpointQuery.coordinate("origin", 0.0, 0.0)
    with pytest.raises(errors.ToolError) as caught:
        timezones.resolve_endpoint_zone(query, transport=transport(b'{"results": []}', []))
    assert caught.value.outcome == errors.MALFORMED_UPSTREAM


def test_a_failed_discovery_request_keeps_its_transport_classification():
    query = timezones.EndpointQuery.stop("origin", STOP_ID)
    with pytest.raises(errors.ToolError) as caught:
        timezones.resolve_endpoint_zone(query, transport=http_error_transport())
    assert caught.value.outcome == errors.UPSTREAM_REJECTED
    assert caught.value.reason == errors.SERVER_ERROR


def http_error_transport():
    import io
    import urllib.error

    def _transport(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, io.BytesIO(b"kaboom"))

    return _transport


def test_discovery_is_budgeted_to_one_request_per_call():
    lat, lon = coordinate_at(100.0)
    for query, body in (
        (timezones.EndpointQuery.stop("origin", STOP_ID), stop_body()),
        (timezones.EndpointQuery.coordinate("origin", 0.0, 0.0), reverse_body("Europe/London", lat, lon)),
    ):
        calls = []
        timezones.resolve_endpoint_zone(query, transport=transport(body, calls))
        assert len(calls) == 1, query
    # a caller declaration costs nothing at all
    calls = []
    timezones.resolve_endpoint_zone(
        timezones.EndpointQuery.stop("origin", STOP_ID, declared_zone="Europe/London"),
        transport=transport(stop_body(), calls),
    )
    assert calls == []


# ── disagreement, like authorities only ────────────────────────────


def test_a_stop_metadata_zone_disagreeing_with_the_response_is_unsupported():
    calls = []
    resolution = timezones.resolve_endpoint_zone(
        timezones.EndpointQuery.stop("origin", STOP_ID), transport=transport(stop_body("Europe/London"), calls)
    )
    with pytest.raises(errors.ToolError) as caught:
        timezones.check_zone_agreement(resolution, "Europe/Berlin")
    exc = caught.value
    assert (exc.outcome, exc.reason) == (errors.UNSUPPORTED, errors.TIMEZONE_DISAGREEMENT)
    assert exc.details["request_zone"] == "Europe/London"
    assert exc.details["request_source"] == "endpoint_stop_info"
    assert exc.details["response_zone"] == "Europe/Berlin"
    assert exc.details["response_source"] == "response_place"
    assert "declare" in exc.details["remedy"].lower()


def test_an_inferred_zone_disagreeing_with_the_response_is_unsupported():
    lat, lon = coordinate_at(100.0)
    resolution = timezones.resolve_endpoint_zone(
        timezones.EndpointQuery.coordinate("origin", 0.0, 0.0),
        transport=transport(reverse_body("Europe/London", lat, lon), []),
    )
    with pytest.raises(errors.ToolError) as caught:
        timezones.check_zone_agreement(resolution, "Europe/Berlin")
    assert caught.value.reason == errors.TIMEZONE_DISAGREEMENT
    assert caught.value.details["request_source"] == "endpoint_reverse_geocode"


def test_a_caller_declaration_is_never_flagged_against_the_response():
    resolution = timezones.resolve_endpoint_zone(
        timezones.EndpointQuery.stop("origin", STOP_ID, declared_zone="Europe/London"),
        transport=transport(stop_body("Europe/Berlin"), []),
    )
    assert timezones.check_zone_agreement(resolution, "Europe/Berlin") is None


def test_a_caller_timezone_is_never_flagged_against_the_response():
    resolution = timezones.ZoneResolution(zone="Europe/Berlin", source="caller_timezone", requests_issued=0, details={})
    assert timezones.check_zone_agreement(resolution, "Europe/London") is None


def test_agreement_is_silent_and_a_missing_response_zone_is_not_a_conflict():
    resolution = timezones.ZoneResolution(zone="Europe/London", source="endpoint_stop_info", requests_issued=1, details={})
    assert timezones.check_zone_agreement(resolution, "Europe/London") is None
    assert timezones.check_zone_agreement(resolution, None) is None


def test_an_unresolved_frame_has_no_zone_to_compare():
    resolution = timezones.ZoneResolution(zone=None, source="unavailable", requests_issued=1, details={})
    assert timezones.check_zone_agreement(resolution, "Europe/London") is None


# ── frame provenance for rendering ─────────────────────────────────


@pytest.mark.parametrize(
    "frame_source, zone_source",
    [
        ("endpoint_stop_info", "response_place"),
        ("endpoint_reverse_geocode", "inferred_nearby_stop"),
        ("caller_endpoint_timezone", "caller_endpoint"),
        ("caller_timezone", "caller_timezone"),
        ("not_applicable", "unavailable"),
        ("unavailable", "unavailable"),
    ],
)
def test_frame_sources_map_to_the_documented_timestamp_provenance(frame_source, zone_source):
    from transit_core import rendering

    assert rendering.zone_source_for_frame(frame_source) == zone_source


def test_a_zone_with_unknown_origin_is_never_reported_as_supplied_by_a_response():
    """T1-02: ``unavailable`` is preserved even when the zone rendered fine.

    A resolved zone is not evidence that the upstream response supplied it, so the
    old silent upgrade to ``response_place`` would forge provenance for a zone that
    came from anywhere else. Only the payload-mapping path — the ``None`` default —
    may claim a response supplied it.
    """
    from transit_core import rendering

    instant = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    stamp = rendering.timestamp(instant, zone="Europe/London", zone_source=rendering.UNAVAILABLE)
    assert stamp["zone"] == "Europe/London"
    assert stamp["local"] == "2026-10-27T09:00:00"
    assert stamp["zone_source"] == rendering.UNAVAILABLE
    assert stamp["display"].endswith("(Europe/London)")
    # the response-mapping default is the one path allowed to assert it
    assert rendering.timestamp(instant, zone="Europe/London")["zone_source"] == rendering.RESPONSE_PLACE


@pytest.mark.parametrize(
    "zone_source",
    [
        "not_a_source",                     # unknown string
        timezones.STOP_INFO,                # frame vocabulary, not a zone_source
        "inferred_nearby_stops",            # typo of the real value
        "",                                 # empty string
        0,                                  # wrong type
        ["response_place"],                 # unhashable, must not raise
        {"zone_source": "response_place"},  # unhashable, must not raise
    ],
)
def test_an_out_of_vocabulary_zone_source_is_unavailable_never_response_place(zone_source):
    """§2.5: provenance is a claim; an unknown source is not evidence of a response."""
    from transit_core import rendering

    instant = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    stamp = rendering.timestamp(instant, zone="Europe/London", zone_source=zone_source)
    assert stamp["zone"] == "Europe/London"
    assert stamp["zone_source"] == rendering.UNAVAILABLE
    assert set(stamp) == {
        "iso",
        "epoch_seconds",
        "local",
        "local_date",
        "utc_offset_seconds",
        "zone",
        "zone_source",
        "display",
    }


# ── payload tz that is not an IANA id (M3) ─────────────────────────


def test_a_payload_zone_that_is_not_an_iana_id_is_an_unresolved_endpoint():
    """§4.3: ``unknown_timezone`` belongs to caller ids, not to the upstream's tz."""
    query = timezones.EndpointQuery.stop("origin", STOP_ID)
    with pytest.raises(errors.ToolError) as caught:
        timezones.resolve_endpoint_zone(
            query, transport=transport(stop_body(zone="Not/AZone"), [])
        )
    exc = caught.value
    assert (exc.outcome, exc.reason) == (errors.INVALID_INPUT, errors.TIMEZONE_UNRESOLVED)
    assert exc.details["endpoint"] == "origin"
    assert exc.details["requests_issued"] == 1
    assert exc.details["payload_zone"] == "Not/AZone"
    assert "remedy" in exc.details


def test_a_reverse_geocode_payload_zone_that_is_not_an_iana_id_is_unresolved():
    lat, lon = coordinate_at(100.0)
    query = timezones.EndpointQuery.coordinate("origin", 0.0, 0.0)
    with pytest.raises(errors.ToolError) as caught:
        timezones.resolve_endpoint_zone(
            query, transport=transport(reverse_body("Not/AZone", lat, lon), [])
        )
    exc = caught.value
    assert (exc.outcome, exc.reason) == (errors.INVALID_INPUT, errors.TIMEZONE_UNRESOLVED)
    assert exc.details["endpoint"] == "origin"
    assert exc.details["payload_zone"] == "Not/AZone"
    assert "remedy" in exc.details
