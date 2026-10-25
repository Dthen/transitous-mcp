"""Geocode, reverse-geocode and nearby-stop contracts (§4.7, §4.8).

Three defects are policed here, each at the boundary that owns it:

* **Identity.** ``stop_id`` is populated for a ``STOP`` match only, and is then the
  payload ``id`` copied verbatim — the same value as ``match_id``, blank ids
  included, because §4.7 applies no blank-to-null rule on this path. An
  ``ADDRESS`` or ``PLACE`` identifier is never presented as a stop id. The nearby
  ``Place`` mapper is the one path that reports an absent or blank ``stopId`` as
  ``null``.
* **Grouping.** The pinned ``Match`` schema has no parent grouping, so a geocode or
  reverse-geocode result carries no ``parent_id`` and no enrichment request is
  issued. The pinned ``Place`` schema does carry ``parentId``, so nearby stops pass
  it through and report ``null`` when the payload omits it.
* **Bounds.** Every argument is validated before any request: the radius limit the
  description always promised, the counts, the coordinate ranges and the exact
  §4.4.1 transit-mode enum. ``near`` biases ranking and filters nothing.

Every wire assertion parses the query string of the URL the production code actually
assembled, never a parameter mapping the test built itself.
"""

import json
import math
import os
import re
import urllib.error
import urllib.parse

import pytest

import fixture_support as fs  # noqa: F401  (puts the repository root on sys.path)

from transit_core import errors, geocode, http, timezones

REPO_ROOT = fs.REPO_ROOT
GEOCODE_SOURCE = os.path.join(REPO_ROOT, "transit_core", "geocode.py")

#: One degree of latitude on the great circle, in metres. Used as an independent
#: physical constant so the distance assertions do not mirror the implementation.
METRES_PER_DEGREE_LAT = math.pi * timezones.EARTH_RADIUS_M / 180.0

ACCEPTED_MODES = (
    "TRANSIT", "TRAM", "SUBWAY", "METRO", "BUS", "COACH", "RAIL", "HIGHSPEED_RAIL",
    "LONG_DISTANCE", "NIGHT_RAIL", "REGIONAL_FAST_RAIL", "REGIONAL_RAIL", "SUBURBAN",
    "FUNICULAR", "AERIAL_LIFT", "AREAL_LIFT", "CABLE_CAR", "FERRY", "AIRPLANE",
    "OTHER", "ODM", "RIDE_SHARING", "FLEX",
)

STREET_MODES = ("WALK", "BIKE", "RENTAL", "CAR", "HGV", "CAR_PARKING", "CAR_DROPOFF")


# ── recording transport ─────────────────────────────────────────────


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        if isinstance(body, bytes):
            self._body = body
        else:
            self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def close(self):
        pass


class Wire:
    """A transport that records every assembled URL instead of hitting the network."""

    def __init__(self, body=None, status=200, exc=None):
        self.body = [] if body is None else body
        self.status = status
        self.exc = exc
        self.requests = []

    def transport(self, request, timeout):
        self.requests.append(request.full_url)
        if self.exc is not None:
            raise self.exc
        return FakeResponse(self.status, self.body)

    def respond(self, body, status=200, exc=None):
        self.body, self.status, self.exc = body, status, exc

    @property
    def url(self):
        assert len(self.requests) == 1, f"expected exactly one request, got {self.requests}"
        return self.requests[0]

    def split(self):
        return urllib.parse.urlsplit(self.url)

    def path(self):
        return self.split().path

    def query(self):
        return urllib.parse.parse_qs(self.split().query, keep_blank_values=True)

    def values(self, name):
        return self.query().get(name)


@pytest.fixture
def wire():
    recorder = Wire()
    http.set_transport(recorder.transport)
    try:
        yield recorder
    finally:
        http.reset_transport()


# ── helpers: synthetic places and matches ───────────────────────────


def stop_entry(stop_id, name, lat, lon, **extra):
    """One pinned ``Place`` as ``/api/v6/map/stops`` returns it."""
    entry = {
        "name": name,
        "stopId": stop_id,
        "lat": lat,
        "lon": lon,
        "tz": "Europe/London",
        "vertexType": "TRANSIT",
        "modes": ["BUS"],
        "importance": 0.5,
    }
    entry.update(extra)
    return entry


def stops_offset_north(center_lat, center_lon, offsets_m):
    """Stops whose great-circle distance north of ``center`` is each offset, in metres."""
    return [
        stop_entry(
            f"stop-{index:02d}",
            f"Stop {index:02d}",
            center_lat + offset / METRES_PER_DEGREE_LAT,
            center_lon,
        )
        for index, offset in enumerate(offsets_m)
    ]


def match_entry(match_type, match_id, name="Somewhere", lat=55.0, lon=-4.0, **extra):
    """One pinned ``Match``, shaped only against the schema's required members."""
    entry = {
        "type": match_type,
        "name": name,
        "id": match_id,
        "lat": lat,
        "lon": lon,
        "tokens": [],
        "areas": [],
        "score": -1.0,
    }
    entry.update(extra)
    return entry


def assert_typed_failure(exc_info, reason, outcome):
    assert isinstance(exc_info.value, errors.ToolError)
    assert exc_info.value.reason == reason
    assert exc_info.value.outcome == outcome
    result = exc_info.value.to_result()
    assert set(result) == {"error", "outcome", "error_type", "error_details"}
    assert result["error_type"] == reason
    assert result["outcome"] == outcome


def assert_ok(result):
    assert result["outcome"] == errors.OK
    assert "error" not in result
    assert "error_type" not in result


# ── module contract ─────────────────────────────────────────────────


def test_module_lives_in_the_checkout_and_imports_no_test_framework():
    assert os.path.realpath(geocode.__file__) == os.path.realpath(GEOCODE_SOURCE)
    source = open(GEOCODE_SOURCE, encoding="utf-8").read()
    assert "pytest" not in source
    assert "import server" not in source


def test_production_code_branches_on_no_name_country_code_or_zone():
    """§2.7: real place names live in fixtures, never in production logic."""
    source = open(GEOCODE_SOURCE, encoding="utf-8").read().lower()
    forbidden = (
        "ayr", "newcastle", "manchester", "piccadilly", "central station",
        "trenitalia", "flixbus", "stagecoach", "translink", "metrolink",
        "great-britain", "europe/", "australia/", "america/",
    )
    for token in forbidden:
        assert token not in source, f"production source mentions {token!r}"
    for code in ("gb", "au", "us", "de"):
        assert re.search(rf"\b{code}\b", source) is None, f"production source mentions {code!r}"


def test_field_sets_are_the_approved_sets():
    assert geocode.GEOCODE_MATCH_FIELDS == (
        "name", "match_type", "match_id", "stop_id", "lat", "lon", "modes",
        "importance", "country", "timezone", "score", "category", "street",
        "house_number", "zip", "level", "areas",
    )
    assert "parent_id" not in geocode.GEOCODE_MATCH_FIELDS
    assert "distance_m" not in geocode.GEOCODE_MATCH_FIELDS
    assert geocode.NEARBY_STOP_FIELDS == (
        "stop_id", "name", "lat", "lon", "distance_m", "modes", "timezone",
        "parent_id", "vertex_type",
    )


def test_accepted_mode_table_is_the_pinned_transit_enum():
    assert tuple(geocode.GEOCODE_MODES) == ACCEPTED_MODES
    assert set(geocode.STREET_MODES) == set(STREET_MODES)
    assert not set(geocode.GEOCODE_MODES) & set(geocode.STREET_MODES)
    assert not any(mode.startswith("DEBUG_") for mode in geocode.GEOCODE_MODES)


# ── geocode: metadata-independent argument validation ───────────────


@pytest.mark.parametrize("query", [None, "", "   ", "\t\n", 5, 5.0, True, [], {}, b"Ayr"])
def test_a_missing_or_blank_query_is_rejected_before_any_request(wire, query):
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode(query)
    assert_typed_failure(caught, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert caught.value.outcome == errors.INVALID_INPUT
    assert caught.value.details.get("argument") == "query"
    assert wire.requests == []


def test_the_query_is_trimmed_on_the_wire(wire):
    wire.respond(fs.load("geocode_ayr.json"))
    geocode.geocode("   Ayr   ")
    assert wire.query()["text"] == ["Ayr"]
    assert len(wire.query()["text"]) == 1


@pytest.mark.parametrize("types", [["STREET"], ["ADDRESS", "TOWN"], ["stop"], [""], [5], "STOP", 5, []])
def test_an_unusable_types_argument_is_rejected_before_any_request(wire, types):
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr", types=types)
    assert_typed_failure(caught, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert wire.requests == []


def test_types_reach_the_wire_as_one_comma_value(wire):
    wire.respond([])
    geocode.geocode("Newcastle", types=["ADDRESS", "STOP"])
    assert wire.query()["type"] == ["ADDRESS,STOP"]


def test_a_single_type_value_is_byte_identical_to_one_plain_value(wire):
    wire.respond([])
    geocode.geocode("Newcastle", types=["STOP"])
    assert wire.query()["type"] == ["STOP"]
    assert urllib.parse.urlencode({"type": "STOP"}) in wire.split().query


@pytest.mark.parametrize("mode", STREET_MODES + ("DEBUG_BUS_ROUTE", "DEBUG_FERRY_ROUTE", "DEBUG_RAILWAY_ROUTE"))
def test_a_street_mode_is_rejected_as_street_mode_not_transit(wire, mode):
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr", modes=[mode])
    assert_typed_failure(caught, errors.STREET_MODE_NOT_TRANSIT, errors.INVALID_INPUT)
    assert caught.value.details.get("argument") == "modes"
    assert wire.requests == []


@pytest.mark.parametrize("mode", ["RAILWAY", "rail", "RAIL ", "TRANSIT,RAIL", ""])
def test_a_near_miss_mode_is_rejected_as_unknown_mode(wire, mode):
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr", modes=[mode])
    assert_typed_failure(caught, errors.UNKNOWN_MODE, errors.INVALID_INPUT)
    assert caught.value.details.get("accepted_modes") == list(ACCEPTED_MODES)
    assert wire.requests == []


@pytest.mark.parametrize("modes", ["RAIL", 5, [], {"RAIL": True}])
def test_an_unusable_modes_argument_is_rejected_before_any_request(wire, modes):
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr", modes=modes)
    assert caught.value.outcome == errors.INVALID_INPUT
    assert wire.requests == []


def test_every_accepted_mode_is_sent_in_the_callers_order(wire):
    wire.respond([])
    reversed_modes = list(reversed(ACCEPTED_MODES))
    geocode.geocode("Ayr", modes=reversed_modes)
    assert wire.query()["mode"] == [",".join(reversed_modes)]


def test_near_is_sent_as_a_place_pair(wire):
    wire.respond([])
    geocode.geocode("Ayr", near=[54.969116, -1.6161327])
    assert wire.query()["place"] == ["54.969116,-1.6161327"]


@pytest.mark.parametrize(
    "near",
    [[1], [1, 2, 3], ["55", "-1"], [True, 0], [91, 0], [-91, 0], [0, 181], [0, -181],
     "55,-1", {"lat": 55, "lon": -1}, 5, [None, 0], [float("nan"), 0], [float("inf"), 0],
     [10 ** 400, 0], [0, -(10 ** 400)]],
)
def test_an_unusable_near_is_rejected_before_any_request(wire, near):
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr", near=near)
    assert_typed_failure(caught, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert wire.requests == []


def test_near_bias_zero_is_not_dropped_from_the_request(wire):
    """Zero is a supplied value; truthiness would silently discard it."""
    wire.respond([])
    geocode.geocode("Ayr", near=[55.0, -4.6], near_bias=0)
    assert wire.query()["placeBias"] == ["0"]


@pytest.mark.parametrize("bias", [0, 1, 0.5, 0.0, 1.0])
def test_a_supplied_near_bias_reaches_the_wire(wire, bias):
    wire.respond([])
    geocode.geocode("Ayr", near=[55.0, -4.6], near_bias=bias)
    assert float(wire.query()["placeBias"][0]) == pytest.approx(float(bias))


@pytest.mark.parametrize("bias", [-0.01, 1.01, -1, 2, True, False, "0.5", [0.5],
                                  float("nan"), float("inf"), 10 ** 400])
def test_a_near_bias_outside_zero_to_one_is_rejected_before_any_request(wire, bias):
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr", near=[55.0, -4.6], near_bias=bias)
    assert_typed_failure(caught, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert wire.requests == []


def test_an_omitted_near_bias_is_not_sent(wire):
    wire.respond([])
    geocode.geocode("Ayr", near=[55.0, -4.6])
    assert wire.values("placeBias") is None


def test_num_results_defaults_to_ten_on_the_wire(wire):
    wire.respond([])
    geocode.geocode("Ayr")
    assert wire.query()["numResults"] == ["10"]
    assert geocode.DEFAULT_NUM_RESULTS == 10


@pytest.mark.parametrize("value", [1, 40, 25])
def test_num_results_inside_one_to_forty_is_sent(wire, value):
    wire.respond([])
    geocode.geocode("Ayr", num_results=value)
    assert wire.query()["numResults"] == [str(value)]


@pytest.mark.parametrize("value", [0, 41, -1, 100])
def test_num_results_outside_one_to_forty_is_rejected_before_any_request(wire, value):
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr", num_results=value)
    assert_typed_failure(caught, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert wire.requests == []


@pytest.mark.parametrize("value", [True, False, 5.5, 5.0, "10"])
def test_num_results_must_be_a_real_integer(wire, value):
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr", num_results=value)
    assert_typed_failure(caught, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert wire.requests == []


def test_language_reaches_the_wire_as_one_comma_value(wire):
    wire.respond([])
    geocode.geocode("Ayr", language="en")
    assert wire.query()["language"] == ["en"]
    geocode.geocode("Ayr", language=["en", "de"])
    assert wire.requests[1].endswith("language=en%2Cde") or "language=en%2Cde" in wire.requests[1]


@pytest.mark.parametrize("language", ["", "   ", [], [""], 5, ["en", 5], {}])
def test_an_unusable_language_is_rejected_before_any_request(wire, language):
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr", language=language)
    assert_typed_failure(caught, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert wire.requests == []


def test_geocode_issues_exactly_one_request_and_never_an_enrichment_request(wire):
    wire.respond(fs.load("geocode_newcastle_central_station.json"))
    geocode.geocode("Newcastle Central station", num_results=10)
    assert len(wire.requests) == 1
    assert wire.path() == "/api/v1/geocode"


def test_no_array_parameter_produces_a_repeated_query_key(wire):
    wire.respond([])
    geocode.geocode("Ayr", types=["ADDRESS", "STOP"], modes=["RAIL", "BUS"], language=["en", "de"])
    query = wire.query()
    for name in ("type", "mode", "language"):
        assert len(query[name]) == 1, f"{name} was serialised as a repeated key: {query[name]}"


def test_every_supplied_filter_appears_once_in_one_request(wire):
    wire.respond([])
    geocode.geocode(
        "Ayr", types=["STOP"], modes=["RAIL"], near=[55.0, -4.6], near_bias=0.25,
        num_results=5, language="en",
    )
    assert set(wire.query()) == {"text", "type", "mode", "place", "placeBias", "numResults", "language"}
    assert wire.query()["placeBias"] == ["0.25"]
    assert wire.query()["numResults"] == ["5"]


# ── geocode: capture-backed success mapping ─────────────────────────


def test_the_effective_query_echo_agrees_with_the_assembled_url(wire):
    wire.respond([])
    result = geocode.geocode(
        "  Ayr  ", types=["STOP"], modes=["RAIL"], near=[55.0, -4.6], near_bias=0.25,
        num_results=5, language=["en", "de"],
    )
    echo = result["effective_query"]
    query = wire.query()
    assert echo["text"] == query["text"][0]
    assert ",".join(echo["types"]) == query["type"][0]
    assert ",".join(echo["modes"]) == query["mode"][0]
    assert f"{echo['near'][0]},{echo['near'][1]}" == query["place"][0]
    assert str(echo["near_bias"]) == query["placeBias"][0]
    assert echo["num_results"] == int(query["numResults"][0])
    assert ",".join(echo["language"]) == query["language"][0]


def test_a_recorded_unqualified_query_reports_the_payload_ranking_honestly(wire):
    """The capture ranks a SUBWAY/BUS stop above every rail-ish stop; say so."""
    payload = fs.load("geocode_newcastle_central_station.json")
    wire.respond(payload)
    result = geocode.geocode("Newcastle Central station")
    assert_ok(result)
    assert result["count"] == len(payload) == 10
    assert result["results"][0]["match_type"] == "STOP"
    assert result["results"][0]["modes"] == ["SUBWAY", "BUS"]
    first_rail_ish = next(
        index for index, match in enumerate(result["results"])
        if match["match_type"] == "STOP" and "REGIONAL_RAIL" in (match["modes"] or [])
    )
    assert first_rail_ish > 0, "the capture ranks the rail stop above the subway stop; re-read it"


def test_stop_identity_is_the_planner_stop_id(wire):
    wire.respond(fs.load("geocode_newcastle_central_station.json"))
    top = geocode.geocode("Newcastle Central station")["results"][0]
    assert top["match_type"] == "STOP"
    assert top["match_id"] == "gb-great-britain_940GZZTWCST"
    assert top["stop_id"] == top["match_id"]


def test_place_and_address_matches_carry_no_stop_id(wire):
    wire.respond(fs.load("geocode_manchester_piccadilly_station.json"))
    results = geocode.geocode("Manchester Piccadilly Station")["results"]
    non_stops = [match for match in results if match["match_type"] != "STOP"]
    assert non_stops, "the capture is expected to contain PLACE and ADDRESS matches"
    for match in non_stops:
        assert match["stop_id"] is None, match
        assert isinstance(match["match_id"], str)


def test_an_address_identifier_is_never_a_stop_identifier(wire):
    payload = fs.load("geocode_manchester_piccadilly_station.json")
    addresses = [match for match in payload if match["type"] == "ADDRESS"]
    assert addresses and addresses[0]["id"] == ""
    wire.respond(payload)
    results = geocode.geocode("Manchester Piccadilly Station")["results"]
    address = next(match for match in results if match["match_type"] == "ADDRESS")
    assert address["match_id"] == ""
    assert address["stop_id"] is None


def test_identity_keys_on_the_match_type_not_on_the_shape_of_the_identifier(wire):
    """A stop-shaped id in an ADDRESS match is still an address, not a stop."""
    branded = match_entry("ADDRESS", "gb-great-britain_940GZZTWCST", name="Central Station")
    wire.respond([branded])
    result = geocode.geocode("Central Station")
    assert result["results"][0]["match_type"] == "ADDRESS"
    assert result["results"][0]["match_id"] == "gb-great-britain_940GZZTWCST"
    assert result["results"][0]["stop_id"] is None


def test_every_match_carries_exactly_the_approved_field_set(wire):
    wire.respond(fs.load("geocode_ayr.json"))
    for match in geocode.geocode("Ayr")["results"]:
        assert tuple(match) == geocode.GEOCODE_MATCH_FIELDS
        assert "parent_id" not in match
        assert "distance_m" not in match


def test_no_geocode_result_carries_parent_grouping(wire):
    wire.respond(fs.load("geocode_newcastle_central_station.json"))
    result = geocode.geocode("Newcastle Central station")
    assert "parent_id" not in json.dumps(result["results"])


def test_metadata_fields_are_copied_from_the_payload(wire):
    payload = fs.load("geocode_ayr.json")
    wire.respond(payload)
    results = geocode.geocode("Ayr")["results"]
    place = next(match for match in results if match["match_type"] == "PLACE")
    raw = next(match for match in payload if match["id"] == place["match_id"])
    assert place["country"] == raw["country"]
    assert place["timezone"] == raw["tz"]
    assert place["score"] == raw["score"]
    assert place["category"] == raw["category"]
    assert place["areas"] == raw["areas"]
    assert place["zip"] == raw.get("zip")
    assert place["street"] == raw.get("street")
    assert place["house_number"] == raw.get("houseNumber")
    assert place["level"] == raw.get("level")
    assert place["modes"] == raw.get("modes")


def test_zero_and_empty_values_are_preserved_rather_than_nulled(wire):
    payload = fs.load("geocode_manchester_piccadilly_station.json")
    wire.respond(payload)
    results = geocode.geocode("Manchester Piccadilly Station")["results"]
    zero_importance = next(
        match for match in results if match["match_id"] == "gb-great-britain_1800EB13971"
    )
    assert zero_importance["importance"] == 0.0
    assert zero_importance["modes"] == []
    below_ground = next(
        match for match in results if match["match_id"] == "gb-great-britain_910GMNCRPIC"
    )
    assert below_ground["level"] == -1.0


def test_a_single_result_request_is_not_a_disambiguation_strategy(wire):
    """The first rank for the qualified query is a bus stop, not the rail station.

    The stub returns the whole recorded body whatever ``numResults`` says, so this
    checks what the request would rank first — the count itself is the upstream's
    job — and that the rail stop is a different identifier, further down.
    """
    payload = fs.load("geocode_manchester_piccadilly_station.json")
    wire.respond(payload)
    single = geocode.geocode("Manchester Piccadilly Station", num_results=1)
    assert wire.query()["numResults"] == ["1"]
    top = single["results"][0]
    assert top["match_type"] == "STOP"
    assert top["modes"] == ["BUS"]
    rail_ish = [
        match for match in single["results"]
        if "REGIONAL_RAIL" in (match["modes"] or [])
    ]
    assert rail_ish, "the capture is expected to contain a rail stop"
    assert rail_ish[0]["match_id"] != top["match_id"]
    assert len(wire.requests) == 1


def test_a_mode_filter_is_what_promotes_the_mainline_stop(wire):
    wire.respond(fs.load("geocode_man_picc_rrail.json"))
    result = geocode.geocode("Manchester Piccadilly Station", types=["STOP"], modes=["REGIONAL_RAIL"])
    assert wire.query()["mode"] == ["REGIONAL_RAIL"]
    assert wire.query()["type"] == ["STOP"]
    assert result["results"][0]["match_id"] == "gb-great-britain_910GMNCRPIC"
    assert "REGIONAL_RAIL" in result["results"][0]["modes"]


def test_a_stop_type_filter_reaches_the_wire_and_the_body_is_passed_through(wire):
    """The filter is forwarded; the returned body is mapped, not re-filtered.

    The recorded body here is already all STOPs, so the assertion below holds
    whether or not anything filters: it pins the request and the identity mapping,
    not a server-side or client-side enforcement guarantee.
    ``test_a_mixed_synthetic_body_is_passed_through_under_a_stop_type_filter`` is
    the case that distinguishes the two.
    """
    wire.respond(fs.load("geocode_newcastle_stop_rail.json"))
    result = geocode.geocode("Newcastle", types=["STOP"], modes=["RAIL"])
    assert all(match["match_type"] == "STOP" for match in result["results"])
    assert all(match["stop_id"] == match["match_id"] for match in result["results"])
    assert wire.query()["type"] == ["STOP"]
    assert wire.query()["mode"] == ["RAIL"]


def test_a_mixed_synthetic_body_is_passed_through_under_a_stop_type_filter(wire):
    """Characterisation, not enforcement: a non-matching row survives the filter.

    The body is synthetic, built against the pinned ``Match`` schema rather than
    captured, and this test also passes on the uncorrected candidate — that is the
    point. ``geocode`` forwards ``type`` to the upstream and maps every row it
    receives (§4.7 delegates the filtering upstream), so under ``types=["STOP"]`` an
    ADDRESS row comes back unchanged and only ``stop_id`` follows the type rule.
    """
    body = [
        match_entry("ADDRESS", "gb-synthetic-address-1", name="1 Market Street"),
        match_entry(
            "STOP", "gb-synthetic-stop-1", name="Central Station", modes=["SUBWAY", "BUS"],
        ),
    ]
    wire.respond(body)
    result = geocode.geocode("Central Station", types=["STOP"])
    assert wire.query()["type"] == ["STOP"]
    assert len(wire.requests) == 1
    assert result["count"] == 2
    assert [match["match_type"] for match in result["results"]] == ["ADDRESS", "STOP"]
    address, stop = result["results"]
    assert address["match_id"] == "gb-synthetic-address-1"
    assert address["stop_id"] is None
    assert stop["match_id"] == "gb-synthetic-stop-1"
    assert stop["stop_id"] == stop["match_id"]


def test_near_biases_the_ranking_and_filters_nothing(wire):
    payload = fs.load("geocode_ayr.json")
    wire.respond(payload)
    biased = geocode.geocode("Ayr", near=[55.458, -4.626], near_bias=1)
    wire.respond(payload)
    unbiased = geocode.geocode("Ayr")
    assert biased["count"] == unbiased["count"] == len(payload)
    assert biased["results"] == unbiased["results"]
    far_away = [match for match in biased["results"] if match["country"] == "AU"]
    assert far_away, "the capture contains a distant namesake; a bias must not filter it out"
    assert "distance_m" not in biased["results"][0]


# ── geocode: malformed and failed upstreams ─────────────────────────


@pytest.mark.parametrize("body", [{"matches": []}, None, "text", 5, True])
def test_a_non_array_geocode_body_is_malformed_not_an_empty_success(wire, body):
    wire.respond(body)
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr")
    assert_typed_failure(caught, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


def test_a_mixed_array_with_a_non_object_member_is_malformed(wire):
    wire.respond([fs.load("geocode_ayr.json")[0], "not a match"])
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr")
    assert_typed_failure(caught, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)
    assert caught.value.details.get("index") == 1


@pytest.mark.parametrize("member", ["type", "name", "id", "lat", "lon", "tokens", "areas", "score"])
def test_a_match_missing_a_pinned_required_member_is_malformed(wire, member):
    entry = match_entry("STOP", "gb-test_1")
    entry.pop(member)
    wire.respond([entry])
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr")
    assert_typed_failure(caught, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)
    assert member in str(caught.value.details.get("missing"))


@pytest.mark.parametrize(
    "member, value",
    [("type", "TOWN"), ("name", 5), ("id", 5), ("lat", "55.0"), ("lon", None),
     ("score", "high"), ("areas", {}), ("tokens", "none"), ("lat", 91), ("modes", "BUS"),
     ("importance", "high"), ("level", True), ("tz", 5)],
)
def test_a_match_with_a_wrongly_typed_or_unknown_member_is_malformed(wire, member, value):
    entry = match_entry("STOP", "gb-test_1")
    entry[member] = value
    wire.respond([entry])
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr")
    assert_typed_failure(caught, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


def test_an_empty_array_is_an_honest_empty_success(wire):
    wire.respond([])
    result = geocode.geocode("Nowhere in particular")
    assert_ok(result)
    assert result["count"] == 0
    assert result["results"] == []


@pytest.mark.parametrize(
    "status, reason, outcome",
    [(400, errors.BAD_REQUEST, errors.UPSTREAM_REJECTED),
     (401, errors.AUTH_OR_BLOCKED, errors.UPSTREAM_REJECTED),
     (404, errors.NOT_FOUND, errors.UPSTREAM_REJECTED),
     (429, errors.RATE_LIMITED, errors.UPSTREAM_REJECTED),
     (500, errors.SERVER_ERROR, errors.UPSTREAM_REJECTED)],
)
def test_http_failures_keep_the_shared_taxonomy(wire, status, reason, outcome):
    wire.respond(b"upstream said no", status=status)
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr")
    assert_typed_failure(caught, reason, outcome)


def test_a_non_json_body_is_malformed_upstream(wire):
    wire.respond(b"<html>not json</html>")
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr")
    assert_typed_failure(caught, errors.NON_JSON_BODY, errors.MALFORMED_UPSTREAM)


def test_a_transport_failure_is_a_typed_transport_error(wire):
    wire.respond([], exc=urllib.error.URLError("connection refused"))
    with pytest.raises(errors.ToolError) as caught:
        geocode.geocode("Ayr")
    assert_typed_failure(caught, errors.CONNECTION_FAILURE, errors.TRANSPORT_ERROR)


# ── reverse geocode ─────────────────────────────────────────────────


def test_reverse_geocode_sends_the_coordinate_pair_and_nothing_else(wire):
    wire.respond([])
    geocode.reverse_geocode(55.458267, -4.625827)
    assert wire.path() == "/api/v1/reverse-geocode"
    assert wire.query() == {"place": ["55.458267,-4.625827"]}
    assert len(wire.requests) == 1


def test_reverse_geocode_sends_supplied_filters(wire):
    wire.respond([])
    geocode.reverse_geocode(55.458267, -4.625827, types=["STOP"], num_results=5)
    query = wire.query()
    assert query["type"] == ["STOP"]
    assert query["numResults"] == ["5"]


@pytest.mark.parametrize("lat, lon", [(91, 0), (-91, 0), (0, 181), (0, -181), (True, 0), (0, False),
                                      ("55", 0), (None, 0), (0, None), (float("nan"), 0)])
def test_reverse_geocode_validates_coordinates_before_any_request(wire, lat, lon):
    with pytest.raises(errors.ToolError) as caught:
        geocode.reverse_geocode(lat, lon)
    assert_typed_failure(caught, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert wire.requests == []


@pytest.mark.parametrize("value", [0, 41, True, 5.5, "5"])
def test_reverse_geocode_rejects_an_unusable_num_results_before_any_request(wire, value):
    with pytest.raises(errors.ToolError) as caught:
        geocode.reverse_geocode(55.0, -4.6, num_results=value)
    assert_typed_failure(caught, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert wire.requests == []


def test_reverse_geocode_uses_the_match_identity_rule(wire):
    """No reverse-geocode capture exists, so the recorded Match container is reused."""
    payload = fs.load("geocode_newcastle_central_station.json")
    wire.respond(payload)
    result = geocode.reverse_geocode(54.969116, -1.6161327)
    assert_ok(result)
    assert result["center"] == [54.969116, -1.6161327]
    stops = [match for match in result["results"] if match["match_type"] == "STOP"]
    others = [match for match in result["results"] if match["match_type"] != "STOP"]
    assert stops and others
    for match in stops:
        assert match["stop_id"] == match["match_id"]
    for match in others:
        assert match["stop_id"] is None
    assert "parent_id" not in json.dumps(result["results"])
    assert len(wire.requests) == 1


@pytest.mark.parametrize("body", [{"place": {}}, None, 5, "text"])
def test_a_malformed_reverse_geocode_body_is_not_an_empty_success(wire, body):
    wire.respond(body)
    with pytest.raises(errors.ToolError) as caught:
        geocode.reverse_geocode(55.0, -4.6)
    assert_typed_failure(caught, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


def test_an_empty_reverse_geocode_array_is_an_honest_empty_success(wire):
    wire.respond([])
    result = geocode.reverse_geocode(55.0, -4.6)
    assert_ok(result)
    assert result["count"] == 0
    assert result["results"] == []


# ── nearby stops: radius enforcement and bounding box ───────────────


def expected_bbox(lat, lon, radius):
    """The approved bounding-box geometry: 111 000 m per degree, cos(lat) on longitude."""
    dlat = radius / 111000.0
    cos_lat = math.cos(math.radians(lat))
    dlon = 180.0 if cos_lat <= 1e-12 else radius / (111000.0 * cos_lat)
    return (max(-90.0, lat - dlat), max(-180.0, lon - dlon),
            min(90.0, lat + dlat), min(180.0, lon + dlon))


def bbox_from_wire(wire):
    (min_lat, min_lon), (max_lat, max_lon) = (
        [float(part) for part in wire.query()["min"][0].split(",")],
        [float(part) for part in wire.query()["max"][0].split(",")],
    )
    return (min_lat, min_lon, max_lat, max_lon)


def test_nearby_stops_sends_only_the_bounding_box(wire):
    wire.respond([])
    geocode.nearby_stops(55.0, -1.6)
    assert wire.path() == "/api/v6/map/stops"
    assert set(wire.query()) == {"min", "max"}
    assert len(wire.requests) == 1


def test_the_default_radius_produces_the_approved_bounding_box(wire):
    wire.respond([])
    geocode.nearby_stops(55.0, -1.6)
    assert geocode.DEFAULT_NEARBY_RADIUS_M == 1000
    assert bbox_from_wire(wire) == expected_bbox(55.0, -1.6, 1000)
    min_lat, min_lon, max_lat, max_lon = bbox_from_wire(wire)
    assert min_lat < max_lat and min_lon < max_lon
    assert (max_lat - min_lat) * 111000.0 == pytest.approx(2000.0)


@pytest.mark.parametrize("radius", [1, 5000, 2500])
def test_a_radius_inside_one_to_5000_is_accepted(wire, radius):
    wire.respond([])
    geocode.nearby_stops(55.0, -1.6, radius=radius)
    assert bbox_from_wire(wire) == expected_bbox(55.0, -1.6, radius)


@pytest.mark.parametrize("radius", [0, 5001, -1, 10000])
def test_a_radius_outside_one_to_5000_is_rejected_before_any_request(wire, radius):
    with pytest.raises(errors.ToolError) as caught:
        geocode.nearby_stops(55.0, -1.6, radius=radius)
    assert_typed_failure(caught, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert caught.value.details.get("argument") == "radius"
    assert wire.requests == []


@pytest.mark.parametrize("radius", [True, False, 1000.5, 1000.0, "1000"])
def test_a_non_integer_radius_is_rejected_before_any_request(wire, radius):
    with pytest.raises(errors.ToolError) as caught:
        geocode.nearby_stops(55.0, -1.6, radius=radius)
    assert_typed_failure(caught, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert wire.requests == []


@pytest.mark.parametrize("lat, lon", [(91, 0), (0, 181), (True, 0), ("55", -1.6), (None, None),
                                      (float("inf"), 0), (float("nan"), 0), (10 ** 400, 0), (0, -(10 ** 400))])
def test_nearby_stops_validates_the_center_before_any_request(wire, lat, lon):
    with pytest.raises(errors.ToolError) as caught:
        geocode.nearby_stops(lat, lon)
    assert_typed_failure(caught, errors.VALUE_OUT_OF_RANGE, errors.INVALID_INPUT)
    assert wire.requests == []


# ── nearby stops: selection, ordering and fields ────────────────────


def test_the_nearest_twenty_inside_the_radius_are_returned_in_order(wire):
    payload = stops_offset_north(55.0, -1.6, [(index + 1) * 100 for index in range(25)])
    wire.respond(payload)
    result = geocode.nearby_stops(55.0, -1.6, radius=5000)
    assert_ok(result)
    assert result["count"] == 20
    assert result["truncated"] is True
    assert [stop["stop_id"] for stop in result["stops"]] == [f"stop-{index:02d}" for index in range(20)]
    distances = [stop["distance_m"] for stop in result["stops"]]
    assert distances == sorted(distances)
    assert all(abs(distance - (index + 1) * 100) <= 0.2 for index, distance in enumerate(distances))
    assert len(wire.requests) == 1


def test_all_stops_inside_the_radius_are_returned_when_they_fit(wire):
    payload = stops_offset_north(55.0, -1.6, [100, 200, 300])
    wire.respond(payload)
    result = geocode.nearby_stops(55.0, -1.6)
    assert result["count"] == 3
    assert result["truncated"] is False
    assert result["radius_m"] == 1000
    assert result["center"] == [55.0, -1.6]


def test_a_stop_beyond_the_radius_is_excluded_even_inside_the_bounding_box(wire):
    """At this latitude the box is wider than the circle; the radius still decides.

    The box corner is inside the requested ``min``/``max`` window but 1.4 radii from
    the centre, so the great-circle test is what removes it. That over-inclusion is
    the ordinary-latitude case; see the module docstring of ``geocode`` for where the
    box can instead be narrower than the circle.
    """
    dlat = 1000.0 / 111000.0
    dlon = 1000.0 / (111000.0 * math.cos(math.radians(55.0)))
    corner = stop_entry("stop-corner", "Corner", 55.0 + dlat, -1.6 + dlon)
    inside = stop_entry("stop-inside", "Inside", 55.0, -1.6)
    wire.respond([corner, inside])
    result = geocode.nearby_stops(55.0, -1.6, radius=1000)
    min_lat, min_lon, max_lat, max_lon = bbox_from_wire(wire)
    assert min_lat <= corner["lat"] <= max_lat and min_lon <= corner["lon"] <= max_lon
    assert [stop["stop_id"] for stop in result["stops"]] == ["stop-inside"]


def test_a_stop_just_inside_the_radius_is_returned_and_one_just_outside_is_not(wire):
    """999.8 m in, 1000.2 m out — an inside/outside pair, not the exact boundary.

    The comparison is ``<=``, but a mathematically exact boundary is decided by
    floating point: a stop placed at a nominal 1000.000 m computes 1000.0000000000668 m
    on the great circle here and is excluded. So this test claims the two sides, not
    boundary inclusion.
    """
    wire.respond(stops_offset_north(55.0, -1.6, [999.8, 1000.2]))
    result = geocode.nearby_stops(55.0, -1.6, radius=1000)
    ids = [stop["stop_id"] for stop in result["stops"]]
    assert ids == ["stop-00"]
    assert result["stops"][0]["distance_m"] == pytest.approx(999.8, abs=0.2)


def test_distance_is_great_circle_metres_not_a_flat_approximation(wire):
    """One degree of latitude is 111 194.9 m on the great circle, at any latitude."""
    wire.respond(stops_offset_north(55.0, -1.6, [1000.0, 2000.0, 3000.0]))
    result = geocode.nearby_stops(55.0, -1.6, radius=5000)
    assert [stop["distance_m"] for stop in result["stops"]] == pytest.approx([1000.0, 2000.0, 3000.0], abs=0.2)


def test_a_longitude_offset_at_the_equator_measures_the_same_degree(wire):
    offset = 3000.0 / METRES_PER_DEGREE_LAT
    wire.respond([stop_entry("stop-east", "East", 0.0, offset)])
    result = geocode.nearby_stops(0.0, 0.0, radius=5000)
    assert result["stops"][0]["distance_m"] == pytest.approx(3000.0, abs=0.2)


def test_every_nearby_stop_carries_exactly_the_approved_field_set(wire):
    wire.respond([stop_entry("stop-1", "Stop 1", 55.0, -1.6)])
    stop = geocode.nearby_stops(55.0, -1.6)["stops"][0]
    assert tuple(stop) == geocode.NEARBY_STOP_FIELDS
    assert stop["stop_id"] == "stop-1"
    assert stop["name"] == "Stop 1"
    assert stop["timezone"] == "Europe/London"
    assert stop["vertex_type"] == "TRANSIT"
    assert stop["modes"] == ["BUS"]
    assert isinstance(stop["distance_m"], float)
    assert stop["parent_id"] is None


def test_parent_id_is_copied_from_the_payload_when_it_is_supplied(wire):
    wire.respond([stop_entry("stop-1", "Stop 1", 55.0, -1.6, parentId="stop-parent")])
    stop = geocode.nearby_stops(55.0, -1.6)["stops"][0]
    assert stop["parent_id"] == "stop-parent"


def test_no_enrichment_request_is_made_and_no_parent_is_invented(wire):
    wire.respond([
        stop_entry("stop-1", "Stop 1", 55.0, -1.6),
        stop_entry("stop-2", "Stop 2", 55.0, -1.601),
    ])
    result = geocode.nearby_stops(55.0, -1.6)
    assert [stop["parent_id"] for stop in result["stops"]] == [None, None]
    assert len(wire.requests) == 1
    assert wire.path() == "/api/v6/map/stops"


def test_the_recorded_stop_place_reports_its_metadata_and_no_parent(wire):
    """``/api/v6/stop`` returns a pinned ``Place``; this one genuinely has no parentId."""
    place = fs.load("stop_metro_central.json")["place"]
    assert "parentId" not in place
    wire.respond([place])
    stop = geocode.nearby_stops(54.969116, -1.6161327)["stops"][0]
    assert stop["stop_id"] == "gb-great-britain_940GZZTWCST"
    assert stop["name"] == "Central Station"
    assert stop["timezone"] == "Europe/London"
    assert stop["modes"] == ["SUBWAY", "BUS"]
    assert stop["vertex_type"] == "TRANSIT"
    assert stop["parent_id"] is None


@pytest.mark.parametrize("extra", [
    {"tz": None}, {"tz": "   "}, {"modes": []}, {"modes": None},
    {"vertexType": None}, {"parentId": None},
])
def test_absent_optional_metadata_is_null_rather_than_invented(wire, extra):
    entry = stop_entry("stop-1", "Stop 1", 55.0, -1.6)
    entry.update(extra)
    wire.respond([entry])
    stop = geocode.nearby_stops(55.0, -1.6)["stops"][0]
    if "tz" in extra:
        assert stop["timezone"] is None
    if "modes" in extra:
        assert stop["modes"] == extra["modes"]
    if "vertexType" in extra:
        assert stop["vertex_type"] is None
    if "parentId" in extra:
        assert stop["parent_id"] is None


def test_an_empty_stop_without_a_stop_id_is_reported_rather_than_dropped(wire):
    entry = stop_entry("stop-1", "Stop 1", 55.0, -1.6)
    entry.pop("stopId")
    wire.respond([entry])
    result = geocode.nearby_stops(55.0, -1.6)
    assert result["count"] == 1
    assert result["stops"][0]["stop_id"] is None


@pytest.mark.parametrize("body", [{"stops": []}, None, 5, "text", [1, 2], [{"lat": 55.0, "lon": -1.6}]])
def test_a_malformed_stop_body_is_not_an_empty_success(wire, body):
    wire.respond(body)
    with pytest.raises(errors.ToolError) as caught:
        geocode.nearby_stops(55.0, -1.6)
    assert_typed_failure(caught, errors.UNEXPECTED_SHAPE, errors.MALFORMED_UPSTREAM)


def test_an_empty_stop_array_is_an_honest_empty_success(wire):
    wire.respond([])
    result = geocode.nearby_stops(55.0, -1.6)
    assert_ok(result)
    assert result["count"] == 0
    assert result["stops"] == []
    assert result["truncated"] is False


def test_a_polar_center_still_produces_a_valid_bounding_box(wire):
    wire.respond([])
    result = geocode.nearby_stops(90.0, 0.0, radius=5000)
    min_lat, min_lon, max_lat, max_lon = bbox_from_wire(wire)
    assert min_lat == pytest.approx(89.95495495495496)
    assert max_lat == 90.0
    assert (min_lon, max_lon) == (-180.0, 180.0)
    assert result["center"] == [90.0, 0.0]
    assert -90.0 <= min_lat < max_lat <= 90.0
