"""Geocode and stop metadata mapping, ``Match``-versus-``Place`` identity (§4.7, §4.8).

Three contracts live here, and each has exactly one place it could be got wrong.

Identity
    ``stop_id`` is populated **only** for a ``STOP`` match, and is then the
    payload ``id`` copied verbatim — the same value as ``match_id``, which is also
    copied verbatim. No blank-to-null rule runs on this path: a payload that
    supplies a blank stop id yields a blank ``stop_id``, because §4.7 defines it as
    the same value as ``match_id`` and says nothing about normalising it.
    An ``ADDRESS`` or ``PLACE`` match is a geocoder result and never a planner
    stop: its ``stop_id`` is ``null`` and ``match_id`` is its only identifier,
    whatever shape the id has. The nearby ``Place`` mapper is the path that
    normalises — an absent or blank ``stopId``/``parentId`` there is ``null``,
    never invented.

Grouping
    The pinned ``Match`` schema has no parent member, so a geocode or
    reverse-geocode result carries no ``parent_id`` key and no enrichment request is
    issued to invent one. The pinned ``Place`` schema does carry ``parentId``, so a
    nearby stop passes it through and reports ``null`` when the payload omits it.

Filters and bounds
    Every argument is validated before any request is assembled (§2.7 stage 1):
    query text, coordinate ranges, count, radius, the exact §4.4.1 transit-mode enum
    and the location-type enum. ``near`` is a ranking bias — it is sent as
    ``place``/``placeBias`` and filters nothing; no result is dropped by distance.

Failure classification
    A body that contradicts the pinned schema is ``malformed_upstream`` /
    ``unexpected_shape``, never an empty success: a missing array proves nothing.
    An empty array the upstream really returned is an ``ok`` result with
    ``count: 0`` — ``no_routes`` belongs to the §4.2 plan predicate and is not
    claimed here.

Decisions the specification left to the implementation (pinned by tests):

* an empty ``types``/``modes`` list is ambiguous (no filter, or nothing allowed) and
  is rejected with ``invalid_input`` / ``value_out_of_range`` rather than silently
  dropped;
* ``num_results`` must be a real integer — ``true``/``false`` and floats are
  rejected, because the schema declares an integer and a coerced count is a guess;
* ``language`` accepts one tag or a sequence of tags (§4.2 lists ``language`` among
  the array parameters) and is sent as one comma-joined value;
* in the nearby ``Place`` mapper an absent or blank ``stopId``/``parentId``/``tz``
  is ``null`` and a present non-string one is a shape contradiction; a geocode
  ``Match`` follows §4.7 instead — ``id`` is copied verbatim into ``match_id``
  and, for a ``STOP``, into ``stop_id``, blank included; descriptive strings are
  copied verbatim on both paths;
* the nearby bounding box keeps the approved inherited 111 000 m-per-degree
  geometry, with a guarded longitude span at the poles and clamped edges; it is an
  approximation of the circle, not a superset of it, so selection by great-circle
  distance guarantees circular membership only among the stops the requested
  window returned;
* ``truncated`` reports whether the nearest-20 limit actually cut the set.
"""

import math
from typing import NoReturn

from . import errors, http, modes, timezones

#: The three endpoint paths this module owns.
GEOCODE_PATH = "/api/v1/geocode"
REVERSE_GEOCODE_PATH = "/api/v1/reverse-geocode"
MAP_STOPS_PATH = "/api/v6/map/stops"

#: The pinned ``LocationType`` enum — a closed set, so a fourth value is a shape
#: contradiction rather than an unknown-but-live type.
MATCH_TYPES = ("ADDRESS", "PLACE", "STOP")

#: §4.4.1 accepted transit modes, in the specification's order, owned by
#: :mod:`transit_core.modes`. Sent in the caller's order, never sorted.
GEOCODE_MODES = modes.TRANSIT_REQUEST_MODE_ORDER

#: §4.4.1 street modes: never a valid filter for a stop search.
STREET_MODES = modes.REJECTED_STREET_MODE_ORDER

DEBUG_MODE_PREFIX = modes.DEBUG_MODE_PREFIX

DEFAULT_NUM_RESULTS = 10
MIN_NUM_RESULTS = 1
MAX_NUM_RESULTS = 40

DEFAULT_NEARBY_RADIUS_M = 1000
MIN_NEARBY_RADIUS_M = 1
MAX_NEARBY_RADIUS_M = 5000
NEARBY_LIMIT = 20

#: The approved inherited bounding-box geometry: 111 000 m per degree on both axes,
#: with the longitude span widened by 1/cos(latitude) and the edges clamped to the
#: coordinate ranges. It is an approximation, not a guaranteed superset of the
#: circle. Away from the poles, and away from the +/-180 clamp, it is wider than the
#: circle and can only over-include; for a 5 km radius above about 89.56 deg of
#: latitude it is narrower than the circle, and at the clamp a window can end before
#: the circle does. The great-circle test therefore decides membership among the
#: stops the window returned -- it is not a global completeness guarantee.
METRES_PER_DEGREE = 111000.0
_POLAR_COS_EPSILON = 1e-12

_LAT_LIMIT = 90.0
_LON_LIMIT = 180.0

#: §4.7 result fields, in the specification's order. No ``parent_id``: the ``Match``
#: schema carries no parent grouping.
GEOCODE_MATCH_FIELDS = (
    "name", "match_type", "match_id", "stop_id", "lat", "lon", "modes",
    "importance", "country", "timezone", "score", "category", "street",
    "house_number", "zip", "level", "areas",
)

#: §4.8 stop fields. ``parent_id`` is present (``Place`` carries ``parentId``) and is
#: ``null`` when the payload supplies none.
NEARBY_STOP_FIELDS = (
    "stop_id", "name", "lat", "lon", "distance_m", "modes", "timezone",
    "parent_id", "vertex_type",
)

_REQUIRED_MATCH_MEMBERS = ("type", "name", "id", "lat", "lon", "tokens", "areas", "score")
_REQUIRED_PLACE_MEMBERS = ("name", "lat", "lon")

_STREET_MODE_REMEDY = (
    "a stop search takes transit modes only; RAIL expands to the rail-ish classes "
    "including SUBWAY, and REGIONAL_RAIL, LONG_DISTANCE, METRO and SUBWAY are the "
    "values that distinguish service classes"
)


# ── small helpers ───────────────────────────────────────────────────


def _bounded(value, limit=120):
    """A short, printable rendering of a caller- or payload-supplied value."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _details(index=None, **extra):
    details = {key: value for key, value in extra.items() if value is not None}
    if index is not None:
        details["index"] = index
    return details


def _shape_failure(message, **details) -> NoReturn:
    errors.fail(errors.UNEXPECTED_SHAPE, message, details)


def _value_failure(message, **details) -> NoReturn:
    errors.fail(errors.VALUE_OUT_OF_RANGE, message, details)


def _is_number(value):
    """A finite real number. Booleans are not numbers, however much Python agrees."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:                      # an int too large for a float
        return False


def _is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _number_text(value):
    """Wire text for a coordinate: ``55`` stays ``55``, ``55.0`` stays ``55.0``."""
    return str(value)


def _place_pair(lat, lon):
    return f"{_number_text(lat)},{_number_text(lon)}"


def _identifier(value):
    """An identifier or zone id: a non-blank string, else ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


# ── argument validation (§2.7 stage 1: before any request) ──────────


def validate_query(query):
    """The search text, trimmed. A blank or non-string query is ``invalid_input``."""
    if not isinstance(query, str):
        _value_failure(
            "query must be a non-empty string",
            argument="query",
            value=_bounded(query),
            constraint="non-empty string",
        )
    text = query.strip()
    if not text:
        _value_failure(
            "query must not be empty after trimming",
            argument="query",
            value=_bounded(query),
            constraint="non-empty after trimming",
        )
    return text


def validate_latitude(value, argument="lat"):
    if not _is_number(value):
        _value_failure(
            f"{argument} must be a finite number", argument=argument, value=_bounded(value)
        )
    if not -_LAT_LIMIT <= value <= _LAT_LIMIT:
        _value_failure(
            f"{argument} is outside -90..90 degrees",
            argument=argument,
            value=_bounded(value),
            minimum=-_LAT_LIMIT,
            maximum=_LAT_LIMIT,
        )
    return value


def validate_longitude(value, argument="lon"):
    if not _is_number(value):
        _value_failure(
            f"{argument} must be a finite number", argument=argument, value=_bounded(value)
        )
    if not -_LON_LIMIT <= value <= _LON_LIMIT:
        _value_failure(
            f"{argument} is outside -180..180 degrees",
            argument=argument,
            value=_bounded(value),
            minimum=-_LON_LIMIT,
            maximum=_LON_LIMIT,
        )
    return value


def validate_types(types):
    """The match kinds to keep, as a list, or ``None`` for every kind."""
    if types is None:
        return None
    if isinstance(types, str) or not isinstance(types, (list, tuple)):
        _value_failure(
            "types must be an array of ADDRESS, PLACE or STOP",
            argument="types",
            value=_bounded(types),
            accepted=list(MATCH_TYPES),
        )
    if not types:
        _value_failure(
            "types must name at least one kind when it is supplied; omit it to accept every kind",
            argument="types",
            value=_bounded(types),
            accepted=list(MATCH_TYPES),
        )
    for item in types:
        if not isinstance(item, str) or item not in MATCH_TYPES:
            _value_failure(
                "every types member must be one of ADDRESS, PLACE or STOP",
                argument="types",
                value=_bounded(item),
                accepted=list(MATCH_TYPES),
            )
    return list(types)


def validate_modes(modes):
    """The transport-mode filter, as a list, or ``None`` for no filter."""
    if modes is None:
        return None
    if isinstance(modes, str) or not isinstance(modes, (list, tuple)):
        _value_failure(
            "modes must be an array of transit modes",
            argument="modes",
            value=_bounded(modes),
            accepted=list(GEOCODE_MODES),
        )
    if not modes:
        _value_failure(
            "modes must name at least one mode when it is supplied; omit it for no mode filter",
            argument="modes",
            value=_bounded(modes),
            accepted=list(GEOCODE_MODES),
        )
    for item in modes:
        if not isinstance(item, str):
            _value_failure(
                "every modes member must be a transit mode name",
                argument="modes",
                value=_bounded(item),
                accepted=list(GEOCODE_MODES),
            )
        if item in STREET_MODES or item.startswith(DEBUG_MODE_PREFIX):
            errors.fail(
                errors.STREET_MODE_NOT_TRANSIT,
                f"{item!r} is a street mode, not a transit mode: {_STREET_MODE_REMEDY}",
                {
                    "argument": "modes",
                    "value": item,
                    "street_modes": list(STREET_MODES),
                    "remedy": _STREET_MODE_REMEDY,
                },
            )
        if item not in GEOCODE_MODES:
            errors.fail(
                errors.UNKNOWN_MODE,
                f"{item!r} is not a transit mode; no friendly aliases are accepted",
                {
                    "argument": "modes",
                    "value": item,
                    "accepted_modes": list(GEOCODE_MODES),
                },
            )
    return list(modes)


def validate_near(near):
    """The ranking-bias coordinate pair, as ``[lat, lon]``, or ``None``."""
    if near is None:
        return None
    if not isinstance(near, (list, tuple)) or len(near) != 2:
        _value_failure(
            "near must be a pair [lat, lon]",
            argument="near",
            value=_bounded(near),
            constraint="[lat, lon]",
        )
    return [
        validate_latitude(near[0], argument="near[0]"),
        validate_longitude(near[1], argument="near[1]"),
    ]


def validate_near_bias(near_bias):
    """The ranking-bias strength in ``0..1`` inclusive, or ``None``.

    Zero is a supplied value and is sent, not discarded: a truthiness test here
    would silently drop the caller's weakest bias.
    """
    if near_bias is None:
        return None
    if not _is_number(near_bias):
        _value_failure(
            "near_bias must be a number between 0 and 1",
            argument="near_bias",
            value=_bounded(near_bias),
            minimum=0,
            maximum=1,
        )
    if not 0 <= near_bias <= 1:
        _value_failure(
            "near_bias must be between 0 and 1 inclusive",
            argument="near_bias",
            value=_bounded(near_bias),
            minimum=0,
            maximum=1,
        )
    return near_bias


def validate_num_results(num_results, default=DEFAULT_NUM_RESULTS):
    """The result count, or ``default`` when omitted. Integers inside 1..40 only."""
    if num_results is None:
        return default
    if not _is_integer(num_results):
        _value_failure(
            "num_results must be an integer",
            argument="num_results",
            value=_bounded(num_results),
            minimum=MIN_NUM_RESULTS,
            maximum=MAX_NUM_RESULTS,
        )
    if not MIN_NUM_RESULTS <= num_results <= MAX_NUM_RESULTS:
        _value_failure(
            f"num_results must be between {MIN_NUM_RESULTS} and {MAX_NUM_RESULTS}",
            argument="num_results",
            value=_bounded(num_results),
            minimum=MIN_NUM_RESULTS,
            maximum=MAX_NUM_RESULTS,
        )
    return num_results


def validate_language(language):
    """One language tag or a sequence of them, as a non-empty list, or ``None``."""
    if language is None:
        return None
    if isinstance(language, str):
        candidates = [language]
    elif isinstance(language, (list, tuple)):
        candidates = list(language)
    else:
        _value_failure(
            "language must be a language tag or an array of them",
            argument="language",
            value=_bounded(language),
            constraint="non-empty string",
        )
    if not candidates:
        _value_failure(
            "language must not be an empty array",
            argument="language",
            value=_bounded(language),
            constraint="non-empty string",
        )
    tags = []
    for item in candidates:
        if not isinstance(item, str):
            _value_failure(
                "every language tag must be a string",
                argument="language",
                value=_bounded(item),
                constraint="non-empty string",
            )
        text = item.strip()
        if not text:
            _value_failure(
                "a language tag must not be empty",
                argument="language",
                value=_bounded(item),
                constraint="non-empty string",
            )
        tags.append(text)
    return tags


def validate_radius(radius, default=DEFAULT_NEARBY_RADIUS_M):
    """The search radius in metres: an integer inside 1..5000, else ``invalid_input``."""
    if radius is None:
        return default
    if not _is_integer(radius):
        _value_failure(
            "radius must be an integer number of metres",
            argument="radius",
            value=_bounded(radius),
            minimum=MIN_NEARBY_RADIUS_M,
            maximum=MAX_NEARBY_RADIUS_M,
        )
    if not MIN_NEARBY_RADIUS_M <= radius <= MAX_NEARBY_RADIUS_M:
        _value_failure(
            f"radius must be between {MIN_NEARBY_RADIUS_M} and {MAX_NEARBY_RADIUS_M} metres",
            argument="radius",
            value=_bounded(radius),
            minimum=MIN_NEARBY_RADIUS_M,
            maximum=MAX_NEARBY_RADIUS_M,
        )
    return radius


# ── response-shape validation ───────────────────────────────────────


def _require_member(raw, member, index):
    if member not in raw:
        _shape_failure(
            f"payload object is missing the required member {member!r}",
            **_details(index, missing=[member]),
        )


def _required_text(raw, member, index):
    value = raw[member]
    if not isinstance(value, str):
        _shape_failure(
            f"payload member {member!r} is not a string",
            **_details(index, member=member, value=_bounded(value)),
        )
    return value


def _required_number(raw, member, index):
    value = raw[member]
    if not _is_number(value):
        _shape_failure(
            f"payload member {member!r} is not a finite number",
            **_details(index, member=member, value=_bounded(value)),
        )
    return value


def _required_list(raw, member, index):
    value = raw[member]
    if not isinstance(value, list):
        _shape_failure(
            f"payload member {member!r} is not an array",
            **_details(index, member=member, value=_bounded(value)),
        )
    return value


def _required_coordinate(raw, member, index, limit):
    value = _required_number(raw, member, index)
    if not -limit <= value <= limit:
        _shape_failure(
            f"payload member {member!r} is outside its coordinate range",
            **_details(index, member=member, value=_bounded(value)),
        )
    return value


def _optional_text(raw, member, index):
    if member not in raw or raw[member] is None:
        return None
    value = raw[member]
    if not isinstance(value, str):
        _shape_failure(
            f"payload member {member!r} is not a string",
            **_details(index, member=member, value=_bounded(value)),
        )
    return value


def _optional_number(raw, member, index):
    if member not in raw or raw[member] is None:
        return None
    value = raw[member]
    if not _is_number(value):
        _shape_failure(
            f"payload member {member!r} is not a finite number",
            **_details(index, member=member, value=_bounded(value)),
        )
    return value


def _optional_string_list(raw, member, index):
    if member not in raw or raw[member] is None:
        return None
    value = raw[member]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _shape_failure(
            f"payload member {member!r} is not an array of strings",
            **_details(index, member=member, value=_bounded(value)),
        )
    return list(value)


def _optional_identifier(raw, member, index):
    """An optional identifier or zone id: absent or blank is ``null``, not invented.

    A present non-string value is a shape contradiction, not a missing identifier:
    silently nulling it would hide a payload change behind a plausible result.
    """
    if member not in raw or raw[member] is None:
        return None
    value = raw[member]
    if not isinstance(value, str):
        _shape_failure(
            f"payload member {member!r} is not a string",
            **_details(index, member=member, value=_bounded(value)),
        )
    return _identifier(value)


def _match_array(body):
    """The geocoder's array of matches. Anything else contradicts the pinned schema."""
    if not isinstance(body, list):
        _shape_failure(
            "geocode response is not an array of matches",
            body_type=type(body).__name__,
            body=_bounded(body),
        )
    return body


def _place_array(body):
    if not isinstance(body, list):
        _shape_failure(
            "map/stops response is not an array of places",
            body_type=type(body).__name__,
            body=_bounded(body),
        )
    return body


# ── mapping ─────────────────────────────────────────────────────────


def map_match(raw, index=None):
    """Map one pinned ``Match`` to the canonical geocode result.

    ``stop_id`` exists only for a ``STOP`` match, where §4.7 makes it the same
    value as ``match_id``: the payload ``id`` is copied verbatim into both, with no
    blank-to-null normalisation, so a blank payload id surfaces as a blank
    ``stop_id``. (The nearby ``Place`` mapper, :func:`map_nearby_stop`, is the path
    that reports a blank identifier as ``null``.) Every other type is a geocoder
    result and keeps ``match_id`` as its sole identifier with ``stop_id: null``; an
    ``ADDRESS`` or ``PLACE`` id is never presented as a stop. No ``parent_id`` is
    produced — the ``Match`` schema carries no parent grouping.
    """
    if not isinstance(raw, dict):
        _shape_failure(
            "geocode match is not an object",
            **_details(index, value=_bounded(raw)),
        )
    for member in _REQUIRED_MATCH_MEMBERS:
        _require_member(raw, member, index)

    match_type = raw["type"]
    if match_type not in MATCH_TYPES:
        _shape_failure(
            "geocode match type is not one of the pinned location types",
            **_details(index, member="type", value=_bounded(match_type), accepted=list(MATCH_TYPES)),
        )

    match_id = _required_text(raw, "id", index)
    _required_list(raw, "tokens", index)

    return {
        "name": _required_text(raw, "name", index),
        "match_type": match_type,
        "match_id": match_id,
        "stop_id": match_id if match_type == "STOP" else None,
        "lat": _required_coordinate(raw, "lat", index, _LAT_LIMIT),
        "lon": _required_coordinate(raw, "lon", index, _LON_LIMIT),
        "modes": _optional_string_list(raw, "modes", index),
        "importance": _optional_number(raw, "importance", index),
        "country": _optional_text(raw, "country", index),
        "timezone": _optional_identifier(raw, "tz", index),
        "score": _required_number(raw, "score", index),
        "category": _optional_text(raw, "category", index),
        "street": _optional_text(raw, "street", index),
        "house_number": _optional_text(raw, "houseNumber", index),
        "zip": _optional_text(raw, "zip", index),
        "level": _optional_number(raw, "level", index),
        "areas": list(_required_list(raw, "areas", index)),
    }


def map_nearby_stop(raw, index=None):
    """Map one pinned ``Place`` to the canonical nearby-stop result.

    ``distance_m`` is filled in by the caller from the great-circle distance; every
    other field comes from the payload. Identity is normalised here, unlike the
    ``Match`` copy in :func:`map_match`: an absent, null **or blank** ``stopId`` is
    reported as ``stop_id: null`` rather than passed through, and ``parent_id`` is
    ``null`` when the payload supplies none — it is never derived from the stop id.
    """
    if not isinstance(raw, dict):
        _shape_failure(
            "map/stops entry is not an object",
            **_details(index, value=_bounded(raw)),
        )
    for member in _REQUIRED_PLACE_MEMBERS:
        _require_member(raw, member, index)

    return {
        "stop_id": _optional_identifier(raw, "stopId", index),
        "name": _required_text(raw, "name", index),
        "lat": _required_coordinate(raw, "lat", index, _LAT_LIMIT),
        "lon": _required_coordinate(raw, "lon", index, _LON_LIMIT),
        "distance_m": None,
        "modes": _optional_string_list(raw, "modes", index),
        "timezone": _optional_identifier(raw, "tz", index),
        "parent_id": _optional_identifier(raw, "parentId", index),
        "vertex_type": _optional_text(raw, "vertexType", index),
    }


# ── bounding box ────────────────────────────────────────────────────


def nearby_bbox(lat, lon, radius=None):
    """The approved bounding box for a radius: ``{"min": [lat, lon], "max": [...]}``.

    This is the inherited rectangular approximation — 111 000 m per degree on both
    axes, the longitude span divided by ``cos(latitude)``, the edges clamped to the
    coordinate ranges — not a covering of the great circle. At ordinary latitudes it
    is wider than the circle, so the later distance test only removes candidates.
    Near a pole it can be narrower than the circle, and at a clamped edge the window
    can end before the circle does, so a stop inside the radius can lie outside the
    window that was requested. At a pole the longitude span is the full range,
    because ``cos(90 deg)`` is zero and a finite span there would exclude every
    neighbour.
    """
    lat = validate_latitude(lat, argument="lat")
    lon = validate_longitude(lon, argument="lon")
    radius = validate_radius(radius)

    latitude_span = radius / METRES_PER_DEGREE
    cos_lat = math.cos(math.radians(lat))
    if cos_lat <= _POLAR_COS_EPSILON:
        longitude_span = _LON_LIMIT
    else:
        longitude_span = radius / (METRES_PER_DEGREE * cos_lat)

    min_lat = max(-_LAT_LIMIT, lat - latitude_span)
    max_lat = min(_LAT_LIMIT, lat + latitude_span)
    min_lon = max(-_LON_LIMIT, lon - longitude_span)
    max_lon = min(_LON_LIMIT, lon + longitude_span)
    return {"min": [min_lat, min_lon], "max": [max_lat, max_lon]}


# ── the three tools ─────────────────────────────────────────────────


def geocode(
    query,
    types=None,
    modes=None,
    near=None,
    near_bias=None,
    num_results=None,
    language=None,
    *,
    transport=None,
):
    """Resolve a place name, address or stop (§4.7).

    ``near`` biases the upstream ranking toward a coordinate and removes nothing;
    ``types`` and ``modes`` restrict the match kinds and the stop modes. Raises
    :class:`transit_core.errors.ToolError` for every failure; the caller converts it
    with :meth:`transit_core.errors.ToolError.to_result`.
    """
    text = validate_query(query)
    types = validate_types(types)
    modes = validate_modes(modes)
    near = validate_near(near)
    near_bias = validate_near_bias(near_bias)
    num_results = validate_num_results(num_results)
    language = validate_language(language)

    params: dict = {"text": text}
    if types:
        params["type"] = list(types)
    if modes:
        params["mode"] = list(modes)
    if near is not None:
        params["place"] = _place_pair(near[0], near[1])
    if near_bias is not None:
        params["placeBias"] = near_bias
    params["numResults"] = num_results
    if language:
        params["language"] = list(language)

    body = http.api_get(GEOCODE_PATH, params, transport=transport)
    results = [map_match(raw, index) for index, raw in enumerate(_match_array(body))]
    return errors.success_result(
        effective_query={
            "text": text,
            "types": types,
            "modes": modes,
            "near": near,
            "near_bias": near_bias,
            "num_results": num_results,
            "language": language,
        },
        count=len(results),
        results=results,
    )


def reverse_geocode(lat, lon, types=None, num_results=None, *, transport=None):
    """Resolve coordinates to the closest places, stops or addresses (§4.3 step 4).

    Identity is the ``Match`` rule, so ``stop_id`` appears only for a ``STOP``
    result, and no ``parent_id`` is produced. ``types`` and ``num_results`` are
    omitted from the request unless the caller supplies them, leaving the upstream's
    documented defaults in force.
    """
    lat = validate_latitude(lat, argument="lat")
    lon = validate_longitude(lon, argument="lon")
    types = validate_types(types)
    if num_results is not None:
        num_results = validate_num_results(num_results)

    place = _place_pair(lat, lon)
    params: dict = {"place": place}
    if types:
        params["type"] = list(types)
    if num_results is not None:
        params["numResults"] = num_results

    body = http.api_get(REVERSE_GEOCODE_PATH, params, transport=transport)
    results = [map_match(raw, index) for index, raw in enumerate(_match_array(body))]
    return errors.success_result(
        center=[lat, lon],
        effective_query={"place": place, "types": types, "num_results": num_results},
        count=len(results),
        results=results,
    )


def nearby_stops(lat, lon, radius=None, *, transport=None):
    """The nearest stops inside a radius (§4.8).

    The radius is enforced before any request, and again by great-circle distance
    for each stop the window returns: at ordinary latitudes the rectangular box is
    wider than the circle, and its corners are up to 1.4 radii from the centre, so
    the distance test removes them. What that guarantees is circular membership
    **within the requested window**: at a clamped ``+/-180`` edge, or close enough
    to a pole that the box is narrower than the circle, a stop inside the radius can
    fall outside the window the upstream was asked for, and a stop that is never
    returned cannot be reported. At most :data:`NEARBY_LIMIT` stops are returned,
    nearest first, and ``truncated`` says whether the limit cut the set.
    """
    lat = validate_latitude(lat, argument="lat")
    lon = validate_longitude(lon, argument="lon")
    radius = validate_radius(radius)
    box = nearby_bbox(lat, lon, radius)

    params: dict = {
        "min": _place_pair(box["min"][0], box["min"][1]),
        "max": _place_pair(box["max"][0], box["max"][1]),
    }
    body = http.api_get(MAP_STOPS_PATH, params, transport=transport)

    measured = []
    for index, raw in enumerate(_place_array(body)):
        stop = map_nearby_stop(raw, index)
        distance = timezones.distance_meters(lat, lon, stop["lat"], stop["lon"])
        if distance <= radius:
            measured.append((distance, stop))
    measured.sort(key=lambda item: (item[0], item[1]["stop_id"] or "", item[1]["name"] or ""))

    truncated = len(measured) > NEARBY_LIMIT
    stops = []
    for distance, stop in measured[:NEARBY_LIMIT]:
        stop["distance_m"] = round(distance, 1)
        stops.append(stop)

    return errors.success_result(
        center=[lat, lon],
        radius_m=radius,
        bbox=box,
        count=len(stops),
        truncated=truncated,
        stops=stops,
    )


__all__ = [
    "GEOCODE_PATH", "REVERSE_GEOCODE_PATH", "MAP_STOPS_PATH",
    "MATCH_TYPES", "GEOCODE_MODES", "STREET_MODES", "DEBUG_MODE_PREFIX",
    "DEFAULT_NUM_RESULTS", "MIN_NUM_RESULTS", "MAX_NUM_RESULTS",
    "DEFAULT_NEARBY_RADIUS_M", "MIN_NEARBY_RADIUS_M", "MAX_NEARBY_RADIUS_M",
    "NEARBY_LIMIT", "METRES_PER_DEGREE",
    "GEOCODE_MATCH_FIELDS", "NEARBY_STOP_FIELDS",
    "validate_query", "validate_latitude", "validate_longitude", "validate_types",
    "validate_modes", "validate_near", "validate_near_bias", "validate_num_results",
    "validate_language", "validate_radius",
    "map_match", "map_nearby_stop", "nearby_bbox",
    "geocode", "reverse_geocode", "nearby_stops",
]
