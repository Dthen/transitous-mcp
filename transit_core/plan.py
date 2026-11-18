"""``transit_plan`` request contract, response mapping, caps and continuation.

This module owns one tool's whole contract (§4.4–§4.6). It is the only place that
assembles a ``/api/v6/plan`` request, the only place that decides what a plan body
means, and the only place that binds a page cursor to the query that produced it.
Everything below the module boundary — the outcome taxonomy, the GET helper, time
classification, zone authority, the timestamp object, the duration arithmetic and
the place/cancellation mapper — belongs to ``transit_core``'s foundations and is
consumed here rather than re-implemented.

Public API (T5 wires this into ``server.py``; see the slice handoff)::

    plan(args, *, transport=None, now=None) -> dict
        The tool entry point. ``args`` is the caller's **raw** argument mapping —
        not a pre-defaulted namespace — because the continuation rules must know
        which arguments the caller actually supplied and which are wrapper
        defaults (§4.6.1: "Defaults do not count as separately supplied
        overrides"). Returns a success object or the constant error object; it
        never raises for caller input.

    parse_request(args) -> PlanRequest
        Stage 1: every metadata-independent check, plus continuation coupling.
        No I/O, so a rejected argument cannot cost a request.

    resolve_time(request, *, transport=None, now=None) -> PlanTime
        Stage 2: the interpretation frame and DST validity. At most one
        discovery request, and only for a naive/date-only clock whose frame was
        not supplied by the caller.

    request_params(request, plan_time) -> dict
        The exact query parameters, in a stable order, for
        ``http.encode_params`` (arrays stay arrays; the helper comma-joins them).

    map_response(data, *, request, plan_time, status=200) -> dict
        Stage 3: shape classification, the no-routes predicate, the zone
        disagreement check, and the itinerary/leg/place mapping.

    frozen_query(request, plan_time) -> dict
        The canonical effective query embedded in ``continuation``.

Continuation
    ``continuation`` is ``{cursor, query}`` and nothing else: no ``page_cursor``
    alias, no second cursor input, no replay side channel, no server state. The
    embedded query is the authority; a separately supplied argument that
    contradicts it is ``invalid_input`` / ``cursor_without_identical_query`` with
    every differing field named. The comparison is typed (strings exactly,
    numbers as numbers, booleans as booleans, ``null`` equal to absent, arrays
    element-wise with ``transit_modes`` order significant, ``time`` compared as
    the instant). This is consistency checking, not tamper resistance: the query
    travels in the caller's request and is not signed.

Decisions the specification left to the implementation are stated where they are
made and listed in the slice handoff, so a reviewer can disagree with one without
having to reverse-engineer it.
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Optional

from . import durations, errors, http, modes, places, rendering, timeparse, timezones

UTC = timezones.UTC

#: The single plan endpoint. Exactly one GET is issued per call, except for the
#: one bounded zone-discovery request §4.3 allows for a naive clock.
PATH = "/api/v6/plan"

# ── §4.4 defaults ───────────────────────────────────────────────────

DEFAULT_MAX_TRANSFERS = 5
DEFAULT_NUM_ITINERARIES = 5
DEFAULT_SEARCH_WINDOW_SECONDS = 900
DEFAULT_MAX_ACCESS_WALK_MINUTES = 15
DEFAULT_MAX_EGRESS_WALK_MINUTES = 15
DEFAULT_MIN_TRANSFER_MINUTES = 0
DEFAULT_INCLUDE_DIRECT = True
DEFAULT_TRANSIT_MODES = ("TRANSIT",)

#: ``name -> (minimum, maximum, default, nullable)`` for every integer argument
#: and every integer member of the frozen query (§4.4, §4.6.1).
INTEGER_BOUNDS = {
    "max_transfers": (0, 10, DEFAULT_MAX_TRANSFERS, False),
    "num_itineraries": (1, 20, DEFAULT_NUM_ITINERARIES, False),
    "max_itineraries": (1, 50, None, True),
    "max_results": (1, 50, None, True),
    "search_window_seconds": (60, 86400, DEFAULT_SEARCH_WINDOW_SECONDS, False),
    "max_access_walk_minutes": (0, 120, DEFAULT_MAX_ACCESS_WALK_MINUTES, False),
    "max_egress_walk_minutes": (0, 120, DEFAULT_MAX_EGRESS_WALK_MINUTES, False),
    "min_transfer_minutes": (0, 120, DEFAULT_MIN_TRANSFER_MINUTES, False),
}

# ── §4.4.1 modes ────────────────────────────────────────────────────

#: The pinned ``Mode`` enum members that denote transit service. This is the
#: **request** enum, owned by :mod:`transit_core.modes`; response classification is
#: ``durations.TRANSIT_MODES``, which owns the street/dummy-walk decision and the
#: deprecated aliases.
ACCEPTED_MODES = modes.TRANSIT_REQUEST_MODES

#: Street modes the caller may not ask a transit plan for (§4.4.1).
REJECTED_STREET_MODES = modes.REJECTED_STREET_MODES

DEBUG_MODE_PREFIX = modes.DEBUG_MODE_PREFIX

# ── bounds on the continuation payload (§4.6.1) ─────────────────────

MAX_CONTINUATION_BYTES = 32768
MAX_CURSOR_BYTES = 16384

#: Longest name sample echoed per leg (§4.5.4).
MAX_INTERMEDIATE_STOP_NAMES = 12

# ── vocabulary ──────────────────────────────────────────────────────

FORM_STOP_ID = "stop_id"
FORM_COORDINATE = "coordinate"
ENDPOINT_FORMS = (FORM_STOP_ID, FORM_COORDINATE)

CONTROL_DEPART_AFTER = timeparse.CONTROL_DEPART_AFTER
CONTROL_ARRIVE_BY = timeparse.CONTROL_ARRIVE_BY
CONTROLS = timeparse.CONTROLS

NO_ROUTES_DETERMINED = "not_determined"
NO_ROUTES_DIRECT = "street_only_alternatives_returned"
NO_ROUTES_REASONS = (NO_ROUTES_DETERMINED, NO_ROUTES_DIRECT)

TRUNCATION_NONE = "none"
TRUNCATION_MAX_RESULTS = "max_results"

REALTIME_UNAVAILABLE = "realtime_unavailable"
DIRECT_ALTERNATIVES_ONLY = "direct_alternatives_only"
NO_TRANSIT_LEG = durations.NO_TRANSIT_LEG

#: The closed argument set of §4.4. ``page_cursor`` is deliberately absent: it is
#: not an alias, and accepting it would silently answer a different page.
ARGUMENTS = frozenset(
    {
        "from_stop_id",
        "to_stop_id",
        "from_lat",
        "from_lon",
        "to_lat",
        "to_lon",
        "time",
        "arrive_by",
        "timezone",
        "origin_timezone",
        "destination_timezone",
        "max_transfers",
        "transit_modes",
        "num_itineraries",
        "max_itineraries",
        "max_results",
        "search_window_seconds",
        "continuation",
        "include_direct",
        "max_access_walk_minutes",
        "max_egress_walk_minutes",
        "min_transfer_minutes",
    }
)

CONTINUATION_FIELDS = frozenset({"cursor", "query"})

#: The frozen effective query's closed field set and canonical order (§4.6.1).
FROZEN_FIELD_ORDER = (
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
)

FROZEN_FIELDS = frozenset(FROZEN_FIELD_ORDER)

#: Constraints echoed as ``constraints_active`` (§4.2), in a stable order.
CONSTRAINT_FIELDS = (
    "max_transfers",
    "max_access_walk_minutes",
    "max_egress_walk_minutes",
)

#: The plan place object's closed field set (§4.5.2). ``places.map_place`` supplies
#: the payload mapping; this module projects it to the documented plan shape and
#: adds the two endpoint-presentation fields the plan contract names.
PLACE_FIELDS = (
    "name",
    "stop_id",
    "zone",
    "modes",
    "lat",
    "lon",
    "track",
    "platform",
    "stop_cancelled",
    "source_cancelled",
    "source_cancelled_origin",
)

_ABSENT = object()


# ── small helpers ───────────────────────────────────────────────────


def _bounded(value, limit=200):
    """A caller-supplied value, bounded for an error detail."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _zone_text(value):
    """An IANA id when the payload supplied a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value.strip() else None


def _json_size(payload):
    """UTF-8 byte length of the compact JSON form of ``payload``."""
    return len(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
    )


def _rotation(control):
    """The endpoint that is relevant to ``control`` (§2.2)."""
    return "origin" if control == CONTROL_DEPART_AFTER else "destination"


def _other_endpoint(label):
    return "destination" if label == "origin" else "origin"


# ── the request ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Endpoint:
    """One endpoint in the form it is sent in.

    ``value`` is what goes on the wire: the stop id verbatim, or ``lat,lon``.
    ``lat``/``lon`` are the numbers the coordinate form was built from, kept so a
    frozen query can be compared numerically rather than textually.
    """

    form: str
    value: str
    lat: Optional[float] = None
    lon: Optional[float] = None

    def to_object(self):
        return {"form": self.form, "value": self.value}


@dataclass(frozen=True)
class Continuation:
    """A page cursor together with the frozen query that produced it."""

    cursor: str
    query: dict = field(default_factory=dict)

    def to_object(self):
        return {"cursor": self.cursor, "query": json.loads(json.dumps(self.query))}


@dataclass(frozen=True)
class PlanRequest:
    """A validated plan request: everything needed to build the query."""

    origin: Endpoint
    destination: Endpoint
    control: str
    parsed_time: "timeparse.ParsedTime"
    caller_timezone: Optional[str] = None
    origin_timezone: Optional[str] = None
    destination_timezone: Optional[str] = None
    max_transfers: int = DEFAULT_MAX_TRANSFERS
    transit_modes: tuple = DEFAULT_TRANSIT_MODES
    num_itineraries: int = DEFAULT_NUM_ITINERARIES
    max_itineraries: Optional[int] = None
    max_results: Optional[int] = None
    search_window_seconds: int = DEFAULT_SEARCH_WINDOW_SECONDS
    include_direct: bool = DEFAULT_INCLUDE_DIRECT
    max_access_walk_minutes: int = DEFAULT_MAX_ACCESS_WALK_MINUTES
    max_egress_walk_minutes: int = DEFAULT_MAX_EGRESS_WALK_MINUTES
    min_transfer_minutes: int = DEFAULT_MIN_TRANSFER_MINUTES
    continuation: Optional[Continuation] = None
    supplied: frozenset = frozenset()

    @property
    def replay(self):
        return self.continuation is not None

    def endpoint(self, label):
        return self.origin if label == "origin" else self.destination

    def zone_declaration(self, label):
        return self.origin_timezone if label == "origin" else self.destination_timezone


@dataclass(frozen=True)
class PlanTime:
    """The classified time plus the discovery that established its frame, if any."""

    frame: "timeparse.TimeFrame"
    zone_resolution: Optional[timezones.ZoneResolution] = None
    endpoint_label: Optional[str] = None


# ── argument validation ─────────────────────────────────────────────


def _fail(reason, message, details=None):
    errors.fail(reason, message, details)


def _int_argument(source, name, *, field_label=None, allow_null=True):
    """A bounded integer argument, with no coercion (§4.4).

    On a caller-facing call ``null`` means "not supplied", exactly like an absent
    key (§2.6), so the default applies. ``allow_null=False`` is used for the frozen
    query's non-nullable members, where a ``null`` cannot mean "absent" because the
    query is complete by definition.
    """
    minimum, maximum, default, _nullable = INTEGER_BOUNDS[name]
    label = field_label or name
    value = source.get(name, _ABSENT)
    if value is _ABSENT:
        return default
    if value is None:
        if allow_null:
            return default
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{label} may not be null; the frozen query is complete",
            {
                "field": label,
                "value": None,
                "minimum": minimum,
                "maximum": maximum,
                "expected": "integer",
            },
        )
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{label} must be an integer, not {type(value).__name__}",
            {
                "field": label,
                "value": _bounded(value),
                "expected": "integer",
                "minimum": minimum,
                "maximum": maximum,
            },
        )
    if not minimum <= value <= maximum:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{label} must be between {minimum} and {maximum}, got {value}",
            {
                "field": label,
                "value": value,
                "minimum": minimum,
                "maximum": maximum,
                "remedy": f"supply an integer between {minimum} and {maximum}",
            },
        )
    return value


def _bool_argument(source, name, *, default, field_label=None):
    label = field_label or name
    value = source.get(name, _ABSENT)
    if value is _ABSENT or value is None:
        return default
    if not isinstance(value, bool):
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{label} must be a boolean, not {type(value).__name__}",
            {"field": label, "value": _bounded(value), "expected": "boolean"},
        )
    return value


def _number(value, label):
    """A finite number, with no string coercion and booleans rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{label} must be a number, not {type(value).__name__}",
            {"field": label, "value": _bounded(value), "expected": "number"},
        )
    if isinstance(value, float) and not math.isfinite(value):
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{label} must be a finite number",
            {"field": label, "value": _bounded(value), "expected": "finite number"},
        )
    return value


def _coordinate_component(value, label, limit):
    number = _number(value, label)
    if not -limit <= number <= limit:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{label} must be between {-limit} and {limit}, got {number!r}",
            {
                "field": label,
                "value": _bounded(value),
                "minimum": -limit,
                "maximum": limit,
                "remedy": f"supply a {label.rsplit('_', 1)[-1]} in degrees within ±{limit}",
            },
        )
    return number


def _coordinate_pair(lat, lon, *, lat_label, lon_label):
    return (
        _coordinate_component(lat, lat_label, 90),
        _coordinate_component(lon, lon_label, 180),
    )


def _parse_coordinate_value(text, field_label):
    """``lat,lon`` from a frozen query, validated as a coordinate."""
    parts = text.split(",")
    if len(parts) != 2:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{field_label} must be a 'lat,lon' pair",
            {"field": field_label, "value": _bounded(text)},
        )
    try:
        lat = float(parts[0])
        lon = float(parts[1])
    except ValueError:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{field_label} must be a numeric 'lat,lon' pair",
            {"field": field_label, "value": _bounded(text)},
        )
    if not (math.isfinite(lat) and math.isfinite(lon)):
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{field_label} must be finite",
            {"field": field_label, "value": _bounded(text)},
        )
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{field_label} coordinate {text!r} is out of range",
            {"field": field_label, "value": _bounded(text), "minimum": [-90, -180], "maximum": [90, 180]},
        )
    return lat, lon


def _endpoint(source, prefix, label):
    """Exactly one form per endpoint (§4.4 endpoint-form policy)."""
    stop_id = source.get(f"{prefix}_stop_id")
    lat = source.get(f"{prefix}_lat")
    lon = source.get(f"{prefix}_lon")

    stop_supplied = stop_id is not None
    lat_supplied = lat is not None
    lon_supplied = lon is not None
    forms = []
    if stop_supplied:
        forms.append(FORM_STOP_ID)
    if lat_supplied or lon_supplied:
        forms.append(FORM_COORDINATE)

    if (not forms) or len(forms) > 1 or (forms == [FORM_COORDINATE] and not (lat_supplied and lon_supplied)):
        missing = []
        if forms == [FORM_COORDINATE]:
            missing = [name for name, present in ((f"{prefix}_lat", lat_supplied), (f"{prefix}_lon", lon_supplied)) if not present]
        _fail(
            errors.MIXED_ENDPOINT_FORM,
            f"the {label} endpoint must use exactly one form: "
            f"{prefix}_stop_id, or {prefix}_lat and {prefix}_lon",
            {
                "endpoint": label,
                "forms_supplied": forms,
                "missing": missing,
                "remedy": f"supply either {prefix}_stop_id or both {prefix}_lat and {prefix}_lon",
            },
        )

    if forms[0] == FORM_STOP_ID:
        if not isinstance(stop_id, str) or not stop_id.strip():
            _fail(
                errors.VALUE_OUT_OF_RANGE,
                f"{prefix}_stop_id must be a non-empty string",
                {"field": f"{prefix}_stop_id", "value": _bounded(stop_id), "expected": "non-empty string"},
            )
        return Endpoint(form=FORM_STOP_ID, value=stop_id)

    lat, lon = _coordinate_pair(lat, lon, lat_label=f"{prefix}_lat", lon_label=f"{prefix}_lon")
    return Endpoint(form=FORM_COORDINATE, value=f"{lat},{lon}", lat=lat, lon=lon)


def _zone_argument(source, name, *, field_label=None):
    """An optional caller zone id, validated eagerly (§2.7 stage 1)."""
    label = field_label or name
    value = source.get(name, _ABSENT)
    if value is _ABSENT or value is None:
        return None
    try:
        return timezones.resolve_zone_name(value)
    except errors.ToolError:
        if field_label is None:
            raise
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{label} is not a resolvable IANA timezone",
            {"field": label, "value": _bounded(value), "remedy": "supply an IANA timezone id"},
        )


def _modes_argument(source, name, *, field_label=None):
    """An ordered array of accepted transit modes, sent in the caller's order."""
    label = field_label or name
    value = source.get(name, _ABSENT)
    if value is _ABSENT or value is None:
        return DEFAULT_TRANSIT_MODES
    if not isinstance(value, (list, tuple)):
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{label} must be an array of mode strings",
            {"field": label, "value": _bounded(value), "expected": "array of strings"},
        )
    if not value:
        # An empty array means "no transit" upstream, and the GET helper omits an
        # empty array entirely — which would silently ask for the default instead.
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{label} may not be empty: a transit plan needs at least one transit mode",
            {
                "field": label,
                "value": [],
                "remedy": "supply at least one transit mode, or use a street-routing tool",
            },
        )
    modes = []
    for item in value:
        if not isinstance(item, str):
            _fail(
                errors.VALUE_OUT_OF_RANGE,
                f"{label} entries must be strings",
                {"field": label, "value": _bounded(item), "expected": "string"},
            )
        if item in REJECTED_STREET_MODES or item.startswith(DEBUG_MODE_PREFIX):
            _fail(
                errors.STREET_MODE_NOT_TRANSIT,
                f"{item!r} is not a transit mode for a transit plan",
                {
                    "mode": item,
                    "field": label,
                    "remedy": "ask for a transit mode such as TRANSIT, RAIL, BUS or TRAM; "
                    "street modes are not transit service",
                },
            )
        if item not in ACCEPTED_MODES:
            _fail(
                errors.UNKNOWN_MODE,
                f"unknown transit mode {item!r}",
                {
                    "mode": item,
                    "field": label,
                    "accepted_modes": sorted(ACCEPTED_MODES),
                    "remedy": "use an exact pinned Mode value; no aliases are accepted",
                },
            )
        modes.append(item)
    return tuple(modes)


# ── continuation parsing ───────────────────────────────────────────


def _parse_continuation(raw):
    """``{cursor, query}`` with both size bounds applied before any I/O."""
    if not isinstance(raw, Mapping):
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            "continuation must be an object with a cursor and the frozen query",
            {
                "field": "continuation",
                "value": _bounded(raw),
                "expected": sorted(CONTINUATION_FIELDS),
            },
        )
    try:
        size = _json_size(raw)
    except (TypeError, ValueError):
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            "continuation is not JSON-serialisable",
            {"field": "continuation", "value": _bounded(raw)},
        )
    if size > MAX_CONTINUATION_BYTES:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"continuation is {size} bytes, over the {MAX_CONTINUATION_BYTES}-byte limit",
            {
                "field": "continuation",
                "bytes": size,
                "max_bytes": MAX_CONTINUATION_BYTES,
                "remedy": "pass the continuation object from the previous result unmodified",
            },
        )

    unknown = sorted(set(raw) - CONTINUATION_FIELDS)
    if unknown:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"continuation has unknown member(s): {', '.join(unknown)}",
            {
                "field": "continuation",
                "unknown_members": unknown,
                "accepted_members": sorted(CONTINUATION_FIELDS),
                "remedy": "a continuation is exactly {cursor, query}",
            },
        )
    missing = [name for name in ("cursor", "query") if name not in raw]
    if missing:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"continuation is missing member(s): {', '.join(missing)}",
            {
                "field": "continuation",
                "missing_members": missing,
                "accepted_members": sorted(CONTINUATION_FIELDS),
            },
        )

    cursor = raw.get("cursor")
    if not isinstance(cursor, str) or not cursor.strip():
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            "continuation.cursor must be a non-empty string",
            {"field": "continuation.cursor", "value": _bounded(cursor), "expected": "non-empty string"},
        )
    cursor_bytes = len(cursor.encode("utf-8"))
    if cursor_bytes > MAX_CURSOR_BYTES:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"continuation.cursor is {cursor_bytes} bytes, over the {MAX_CURSOR_BYTES}-byte limit",
            {
                "field": "continuation.cursor",
                "bytes": cursor_bytes,
                "max_bytes": MAX_CURSOR_BYTES,
            },
        )

    return Continuation(cursor=cursor, query=_parse_frozen_query(raw.get("query")))


def _frozen_endpoint(raw, label, name):
    field_label = f"continuation.query.{name}"
    if not isinstance(raw, Mapping):
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{field_label} must be an object with form and value",
            {"field": field_label, "value": _bounded(raw), "expected": ["form", "value"]},
        )
    unknown = sorted(set(raw) - {"form", "value"})
    if unknown:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{field_label} has unknown member(s): {', '.join(unknown)}",
            {"field": field_label, "unknown_members": unknown, "accepted_members": ["form", "value"]},
        )
    if "form" not in raw or "value" not in raw:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{field_label} needs both form and value",
            {"field": field_label, "missing_members": [n for n in ("form", "value") if n not in raw]},
        )
    form = raw.get("form")
    value = raw.get("value")
    if form not in ENDPOINT_FORMS:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{field_label}.form must be one of {list(ENDPOINT_FORMS)}",
            {"field": field_label, "value": _bounded(form), "accepted": list(ENDPOINT_FORMS)},
        )
    if not isinstance(value, str) or not value.strip():
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{field_label}.value must be a non-empty string",
            {"field": field_label, "value": _bounded(value)},
        )
    if form == FORM_STOP_ID:
        return Endpoint(form=form, value=value)
    lat, lon = _parse_coordinate_value(value, field_label)
    return Endpoint(form=form, value=value, lat=lat, lon=lon)


def _parse_frozen_query(raw):
    """Validate the embedded query with the same rules as an initial call."""
    if not isinstance(raw, Mapping):
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            "continuation.query must be the frozen effective query object",
            {"field": "continuation.query", "value": _bounded(raw)},
        )
    unknown = sorted(set(raw) - FROZEN_FIELDS)
    if unknown:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"continuation.query has unknown member(s): {', '.join(unknown)}",
            {
                "field": "continuation.query",
                "unknown_members": unknown,
                "accepted_members": list(FROZEN_FIELD_ORDER),
            },
        )
    missing = [name for name in FROZEN_FIELD_ORDER if name not in raw]
    if missing:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"continuation.query is missing member(s): {', '.join(missing)}",
            {
                "field": "continuation.query",
                "missing_members": missing,
                "accepted_members": list(FROZEN_FIELD_ORDER),
            },
        )

    frozen = {}
    frozen["from"] = _frozen_endpoint(raw["from"], "origin", "from").to_object()
    frozen["to"] = _frozen_endpoint(raw["to"], "destination", "to").to_object()

    control = raw["control"]
    if control not in CONTROLS:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            f"continuation.query.control must be one of {list(CONTROLS)}",
            {"field": "continuation.query.control", "value": _bounded(control)},
        )
    frozen["control"] = control

    time_value = raw["time"]
    parsed = None
    if isinstance(time_value, str):
        try:
            parsed = timeparse.classify_syntax(time_value)
        except errors.ToolError:
            parsed = None
    if parsed is None or parsed.form != timeparse.FORM_INSTANT:
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            "continuation.query.time must be an offset-bearing RFC 3339 instant",
            {
                "field": "continuation.query.time",
                "value": _bounded(time_value),
                "remedy": "a frozen query always carries an explicit instant; "
                "re-run the first call if the query is unavailable",
            },
        )
    frozen["time"] = time_value

    for name in ("timezone", "origin_timezone", "destination_timezone"):
        frozen[name] = _zone_argument(raw, name, field_label=f"continuation.query.{name}")

    for name in INTEGER_BOUNDS:
        nullable = name in ("max_itineraries", "max_results")
        frozen[name] = _int_argument(
            raw,
            name,
            field_label=f"continuation.query.{name}",
            allow_null=nullable,
        )

    frozen["transit_modes"] = list(
        _modes_argument(raw, "transit_modes", field_label="continuation.query.transit_modes")
    )
    frozen["include_direct"] = _bool_argument(
        raw, "include_direct", default=DEFAULT_INCLUDE_DIRECT, field_label="continuation.query.include_direct"
    )

    return {name: frozen[name] for name in FROZEN_FIELD_ORDER}


# ── request construction ───────────────────────────────────────────


def parse_request(args):
    """Validate the caller's raw arguments and return a :class:`PlanRequest`.

    Pure: no request, no clock and no zone lookup, so §2.7's stage 1 is complete
    before anything can be spent on the network.
    """
    if args is None:
        args = {}
    if not isinstance(args, Mapping):
        _fail(
            errors.VALUE_OUT_OF_RANGE,
            "plan arguments must be an object",
            {"argument": "args", "value": _bounded(args), "expected": "object"},
        )

    unknown = sorted(set(args) - ARGUMENTS)
    if unknown:
        name = unknown[0]
        details = {
            "argument": name,
            "unknown_arguments": unknown,
            "accepted_arguments": sorted(ARGUMENTS),
        }
        if name == "page_cursor":
            details["remedy"] = (
                "there is no page_cursor argument; pass the continuation object "
                "{cursor, query} returned by the previous result"
            )
        else:
            details["remedy"] = "remove the argument; the transit_plan argument set is closed"
        _fail(errors.VALUE_OUT_OF_RANGE, f"unknown transit_plan argument: {name}", details)

    continuation = args.get("continuation")
    if continuation is not None:
        return _continuation_request(args, continuation)
    return _initial_request(args)


def _supplied_names(args):
    return frozenset(
        name for name, value in args.items() if value is not None and name != "continuation"
    )


def _initial_request(args):
    origin = _endpoint(args, "from", "origin")
    destination = _endpoint(args, "to", "destination")
    parsed = timeparse.classify_syntax(args.get("time"))
    arrive_by = _bool_argument(args, "arrive_by", default=False)

    request = PlanRequest(
        origin=origin,
        destination=destination,
        control=CONTROL_ARRIVE_BY if arrive_by else CONTROL_DEPART_AFTER,
        parsed_time=parsed,
        caller_timezone=_zone_argument(args, "timezone"),
        origin_timezone=_zone_argument(args, "origin_timezone"),
        destination_timezone=_zone_argument(args, "destination_timezone"),
        max_transfers=_int_argument(args, "max_transfers"),
        transit_modes=_modes_argument(args, "transit_modes"),
        num_itineraries=_int_argument(args, "num_itineraries"),
        max_itineraries=_int_argument(args, "max_itineraries"),
        max_results=_int_argument(args, "max_results"),
        search_window_seconds=_int_argument(args, "search_window_seconds"),
        include_direct=_bool_argument(args, "include_direct", default=DEFAULT_INCLUDE_DIRECT),
        max_access_walk_minutes=_int_argument(args, "max_access_walk_minutes"),
        max_egress_walk_minutes=_int_argument(args, "max_egress_walk_minutes"),
        min_transfer_minutes=_int_argument(args, "min_transfer_minutes"),
        supplied=_supplied_names(args),
    )
    return request


def _continuation_request(args, raw):
    """Build the request from the frozen query and reject contradictions."""
    continuation = _parse_continuation(raw)
    frozen = continuation.query

    parsed = timeparse.classify_syntax(frozen["time"])
    request = PlanRequest(
        origin=_frozen_endpoint(frozen["from"], "origin", "from"),
        destination=_frozen_endpoint(frozen["to"], "destination", "to"),
        control=frozen["control"],
        parsed_time=parsed,
        caller_timezone=frozen["timezone"],
        origin_timezone=frozen["origin_timezone"],
        destination_timezone=frozen["destination_timezone"],
        max_transfers=frozen["max_transfers"],
        transit_modes=tuple(frozen["transit_modes"]),
        num_itineraries=frozen["num_itineraries"],
        max_itineraries=frozen["max_itineraries"],
        max_results=frozen["max_results"],
        search_window_seconds=frozen["search_window_seconds"],
        include_direct=frozen["include_direct"],
        max_access_walk_minutes=frozen["max_access_walk_minutes"],
        max_egress_walk_minutes=frozen["max_egress_walk_minutes"],
        min_transfer_minutes=frozen["min_transfer_minutes"],
        continuation=continuation,
        supplied=_supplied_names(args),
    )

    mismatches = _compare_supplied(args, request)
    if mismatches:
        _fail(
            errors.CURSOR_WITHOUT_IDENTICAL_QUERY,
            "a supplied argument contradicts the continuation's frozen query: "
            + ", ".join(item["field"] for item in mismatches),
            {
                "fields": [item["field"] for item in mismatches],
                "mismatches": mismatches,
                "remedy": "omit the differing argument(s), or supply the frozen value; "
                "the continuation query is the authority",
            },
        )
    return request


def _endpoints_equal(supplied, frozen):
    if supplied.form != frozen.form:
        return False
    if supplied.form == FORM_STOP_ID:
        return supplied.value == frozen.value
    return supplied.lat == frozen.lat and supplied.lon == frozen.lon


def _compare_supplied(args, request):
    """Every separately supplied argument that contradicts the frozen query.

    Typed comparison, in the frozen query's field order: strings exactly, numbers
    as numbers, booleans as booleans, arrays element-wise in order, ``time`` as the
    instant. ``None`` means "not supplied" and so never contradicts anything.
    """
    mismatches = []

    def record(field_name, supplied, frozen, remedy=None):
        item = {"field": field_name, "supplied": supplied, "frozen": frozen}
        if remedy:
            item["remedy"] = remedy
        mismatches.append(item)

    supplied = request.supplied

    for label, prefix, name in (("origin", "from", "from"), ("destination", "to", "to")):
        if {f"{prefix}_stop_id", f"{prefix}_lat", f"{prefix}_lon"} & supplied:
            endpoint = _endpoint(args, prefix, label)
            frozen_endpoint = request.endpoint(label)
            if not _endpoints_equal(endpoint, frozen_endpoint):
                record(
                    name,
                    {"form": endpoint.form, "value": _bounded(endpoint.value)},
                    {"form": frozen_endpoint.form, "value": _bounded(frozen_endpoint.value)},
                )

    if "arrive_by" in supplied:
        control = (
            CONTROL_ARRIVE_BY
            if _bool_argument(args, "arrive_by", default=False)
            else CONTROL_DEPART_AFTER
        )
        if control != request.control:
            record("control", control, request.control)

    if "time" in supplied:
        supplied_value = args.get("time")
        parsed = None
        try:
            parsed = timeparse.classify_syntax(supplied_value)
        except errors.ToolError:
            parsed = None
        frozen_instant = request.parsed_time.instant
        if parsed is None or parsed.form != timeparse.FORM_INSTANT:
            record(
                "time",
                {"as_given": _bounded(supplied_value), "form": getattr(parsed, "form", "malformed")},
                {"as_given": request.parsed_time.as_given, "instant": timezones.iso_z(frozen_instant)},
                remedy=(
                    "omit time, or supply the matching explicit instant "
                    f"{timezones.iso_z(frozen_instant)}"
                ),
            )
        elif parsed.instant != frozen_instant:
            record(
                "time",
                timezones.iso_z(parsed.instant),
                timezones.iso_z(frozen_instant),
                remedy=f"omit time, or supply the matching explicit instant {timezones.iso_z(frozen_instant)}",
            )

    for name, attribute in (
        ("timezone", "caller_timezone"),
        ("origin_timezone", "origin_timezone"),
        ("destination_timezone", "destination_timezone"),
    ):
        if name in supplied:
            value = _zone_argument(args, name)
            frozen_value = getattr(request, attribute)
            if value != frozen_value:
                record(name, value, frozen_value)

    for name in INTEGER_BOUNDS:
        if name in supplied:
            value = _int_argument(args, name)
            if value != getattr(request, name):
                record(name, value, getattr(request, name))

    if "transit_modes" in supplied:
        value = list(_modes_argument(args, "transit_modes"))
        if value != list(request.transit_modes):
            record("transit_modes", value, list(request.transit_modes))

    if "include_direct" in supplied:
        value = _bool_argument(args, "include_direct", default=DEFAULT_INCLUDE_DIRECT)
        if value != request.include_direct:
            record("include_direct", value, request.include_direct)

    order = {name: position for position, name in enumerate(FROZEN_FIELD_ORDER)}
    mismatches.sort(key=lambda item: order[item["field"]])
    return mismatches


# ── time resolution ────────────────────────────────────────────────


def resolve_time(request, *, transport=None, now=None):
    """Resolve the interpretation frame and enforce DST validity (§2.7 stage 2).

    A continuation carries an explicit instant, so it needs no frame and issues no
    discovery request. So does an explicit instant or an omitted time on a first
    call. Only a naive or date-only clock that the caller did not frame with
    ``timezone`` costs the one discovery request §4.3 budgets.
    """
    control = request.control
    parsed = request.parsed_time
    caller_timezone = request.caller_timezone

    if parsed.needs_frame:
        if caller_timezone is not None:
            choice = timeparse.FrameChoice(caller_timezone, timezones.CALLER_TIMEZONE)
            frame = timeparse.interpret(parsed, control=control, frame=choice, display_zone=choice.zone)
            return PlanTime(frame=frame)
        label = _rotation(control)
        endpoint = request.endpoint(label)
        resolution = timezones.resolve_endpoint_zone(
            timezones.EndpointQuery(
                label=label,
                stop_id=endpoint.value if endpoint.form == FORM_STOP_ID else None,
                lat=endpoint.lat,
                lon=endpoint.lon,
                declared_zone=request.zone_declaration(label),
            ),
            transport=transport,
        )
        choice = timeparse.FrameChoice(resolution.zone, resolution.source)
        frame = timeparse.interpret(parsed, control=control, frame=choice, display_zone=choice.zone)
        return PlanTime(frame=frame, zone_resolution=resolution, endpoint_label=label)

    # An explicit instant, an omitted time, or a frozen instant: no frame is needed,
    # so nothing is discovered. A caller timezone or a declared endpoint zone is
    # still used for the display rendering of the interpreted instant, with its own
    # provenance.
    label = _rotation(control)
    declared = request.zone_declaration(label)
    display_zone = caller_timezone if caller_timezone is not None else declared
    if display_zone is None:
        choice = None
        source = None
    else:
        source = (
            timezones.CALLER_TIMEZONE if caller_timezone is not None else timezones.CALLER_ENDPOINT
        )
        choice = timeparse.FrameChoice(display_zone, source)
    frame = timeparse.interpret(
        parsed, control=control, frame=choice, display_zone=display_zone, now=now
    )
    return PlanTime(frame=frame, endpoint_label=label)


def frozen_query(request, plan_time):
    """The canonical effective query embedded in ``continuation`` (§4.6.1).

    Defaults are frozen as the values actually sent, ``time`` is the explicit
    instant the request was built from, and presentation-only values are absent.
    """
    return {
        "from": request.origin.to_object(),
        "to": request.destination.to_object(),
        "control": request.control,
        "time": plan_time.frame.wire_value,
        "timezone": request.caller_timezone,
        "origin_timezone": request.origin_timezone,
        "destination_timezone": request.destination_timezone,
        "max_transfers": request.max_transfers,
        "num_itineraries": request.num_itineraries,
        "max_itineraries": request.max_itineraries,
        "max_results": request.max_results,
        "search_window_seconds": request.search_window_seconds,
        "max_access_walk_minutes": request.max_access_walk_minutes,
        "max_egress_walk_minutes": request.max_egress_walk_minutes,
        "min_transfer_minutes": request.min_transfer_minutes,
        "transit_modes": list(request.transit_modes),
        "include_direct": request.include_direct,
    }


def request_params(request, plan_time, *, cursor=None):
    """The query parameters, in a stable order, for ``http.encode_params``.

    ``transitModes`` stays a tuple so the GET helper comma-joins it into one value
    (§4.2); ``max_results``/``include_direct`` are wrapper concerns and are never
    sent. The cursor, when replaying, is the continuation's own cursor.
    """
    if cursor is None and request.continuation is not None:
        cursor = request.continuation.cursor
    params = {
        "fromPlace": request.origin.value,
        "toPlace": request.destination.value,
        "time": plan_time.frame.wire_value,
        "arriveBy": request.control == CONTROL_ARRIVE_BY,
        "numItineraries": request.num_itineraries,
    }
    if request.max_itineraries is not None:
        params["maxItineraries"] = request.max_itineraries
    params.update(
        {
            "maxTransfers": request.max_transfers,
            "transitModes": tuple(request.transit_modes),
            "searchWindow": request.search_window_seconds,
            "maxPreTransitTime": request.max_access_walk_minutes * 60,
            "maxPostTransitTime": request.max_egress_walk_minutes * 60,
            "minTransferTime": request.min_transfer_minutes * 60,
        }
    )
    if cursor is not None:
        params["pageCursor"] = cursor
    return params


# ── the GET, with the status the no-routes predicate needs ─────────


def _response_status(response):
    """The response's HTTP status.

    Mirrors ``http._status_of`` because ``api_get`` returns the parsed body and not
    the status: a response with no status attribute is treated as 200 exactly as
    the GET helper treats it. The plan contract needs the status itself, because
    §4.2's no-routes predicate requires HTTP **200** rather than any 2xx.
    """
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else 200
    return int(status)


def _plan_get(params, *, transport=None):
    """One ``api_get`` call, plus the status observed on the same response."""
    observed = {}
    inner = http._resolve_transport(transport)

    def observing(request, timeout):
        response = inner(request, timeout)
        observed["status"] = _response_status(response)
        return response

    data = http.api_get(PATH, params, transport=observing)
    return data, observed.get("status", 200)


# ── response mapping ───────────────────────────────────────────────


def _warning(code, message, scope):
    return {"code": code, "message": message, "scope": scope}


def _add_warnings(warnings, codes, scope):
    for code in codes:
        warnings.append(_warning(code, _warning_message(code), scope))


_WARNING_MESSAGES = {
    durations.DURATION_COMPONENTS_INCOMPLETE: "a duration component could not be derived from the payload",
    durations.DURATION_DECOMPOSITION_INCONSISTENT: "the duration components do not add up to the elapsed time",
    durations.DATE_LINE_CROSSING: "the itinerary crosses the date line westbound",
    durations.ENDPOINT_ZONE_UNAVAILABLE: "an endpoint display zone is unavailable",
    durations.CALENDAR_INPUTS_INCOMPLETE: "a calendar quantity could not be derived from the payload",
    NO_TRANSIT_LEG: "this itinerary has no transit leg",
    REALTIME_UNAVAILABLE: "no returned leg carries realtime data; times are scheduled, not live",
    DIRECT_ALTERNATIVES_ONLY: "no transit itineraries were returned; street-only direct alternatives were",
}


def _warning_message(code):
    return _WARNING_MESSAGES.get(code, code.replace("_", " "))


def _dedupe(warnings):
    seen = set()
    unique = []
    for warning in warnings:
        key = (warning["code"], warning["scope"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(warning)
    return unique


def _instant(value, element, option, scope_kind="itinerary"):
    """A required, parseable itinerary instant, located on its own alternative.

    ``scope_kind`` is the key the accepted model uses for this alternative
    (``itinerary`` or ``direct``), so a rejection names the object the caller can
    find in the response rather than a fixed vocabulary.
    """
    if not isinstance(value, str):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"{scope_kind} {option} {element} is missing or not a timestamp string",
            {scope_kind: option, "element": element, "value": _bounded(value)},
        )
    try:
        return timezones.parse_rfc3339(value)
    except ValueError:
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"{scope_kind} {option} {element} is not an RFC 3339 timestamp",
            {scope_kind: option, "element": element, "value": _bounded(value)},
        )


def _optional_instant(value, element, option, index, scope_kind="itinerary"):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return timezones.parse_rfc3339(value)
        except ValueError:
            pass
    errors.fail(
        errors.UNEXPECTED_SHAPE,
        f"{scope_kind} {option} leg {index} {element} is not an RFC 3339 timestamp",
        {scope_kind: option, "leg": index, "element": element, "value": _bounded(value)},
    )


def _leg_instants(leg, option, index, scope_kind="itinerary"):
    """The leg start/end the consumers actually use (§3.1).

    ``durations.LegTiming.from_leg`` and the rendered leg both read ``startTime``
    and fall back to the endpoint's own ``from.departure`` when it is absent; the
    end does the same through ``to.arrival``. The validator must order the same
    pair, not just the top-level fields. A supplied value that is present but
    unparseable is rejected here rather than treated as absent, so the fallback is
    reached only for a genuinely missing instant. Any rejection carries the
    caller's ``scope_kind`` and ``option``, so the same parse is located the same
    way wherever it runs.
    """
    start_field = "startTime"
    start = _optional_instant(leg.get(start_field), start_field, option, index, scope_kind)
    if start is None:
        start_field = "departure"
        start = _optional_instant(
            (leg.get("from") or {}).get("departure"), start_field, option, index, scope_kind
        )
    end_field = "endTime"
    end = _optional_instant(leg.get(end_field), end_field, option, index, scope_kind)
    if end is None:
        end_field = "arrival"
        end = _optional_instant(
            (leg.get("to") or {}).get("arrival"), end_field, option, index, scope_kind
        )
    return start, start_field, end, end_field


def _validate_legs(legs, option, scope_kind="itinerary"):
    """Validate leg containers, typed fields and temporal ordering.

    ``durations.decompose`` deliberately assumes ordered, shape-validated legs
    (T1 consumer precondition 4); this is the normalizer that makes that true. The
    ordering is checked on the same instants the consumers use, including the
    endpoint fallbacks (``startTime`` else ``from.departure``, ``endTime`` else
    ``to.arrival``). A leg that carries no instant at all is tolerated — its
    duration then falls back to whatever the payload does support, and a component
    that cannot be derived is ``null`` with a warning rather than a fabricated
    zero (§3.1 rule 1). A declared duration is a quantity, so a negative one is
    contradictory shape (§3.1: a negative value is never emitted).

    Every rejection is located on the alternative being validated: ``scope_kind``
    (``itinerary`` or ``direct``) plus the 1-based ``option`` and the 0-based
    ``leg``, in the details and in the message.
    """
    previous_start = None
    for index, leg in enumerate(legs):
        if not isinstance(leg, dict):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                f"{scope_kind} {option} leg {index} is not an object",
                {scope_kind: option, "leg": index},
            )
        mode = leg.get("mode")
        if not isinstance(mode, str) or not mode:
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                f"{scope_kind} {option} leg {index} mode is not a non-empty string",
                {scope_kind: option, "leg": index, "element": "mode", "value": _bounded(mode)},
            )
        for side in ("from", "to"):
            if not isinstance(leg.get(side), dict):
                errors.fail(
                    errors.UNEXPECTED_SHAPE,
                    f"{scope_kind} {option} leg {index} {side} is not an object",
                    {scope_kind: option, "leg": index, "element": side},
                )
        start, start_field, end, end_field = _leg_instants(leg, option, index, scope_kind)
        if start is not None and end is not None and end < start:
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                f"{scope_kind} {option} leg {index} ends before it starts",
                {scope_kind: option, "leg": index, "element": end_field},
            )
        duration = leg.get("duration")
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or (isinstance(duration, float) and not math.isfinite(duration))
        ):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                f"{scope_kind} {option} leg {index} duration is not a number",
                {scope_kind: option, "leg": index, "element": "duration", "value": _bounded(duration)},
            )
        if duration is not None and duration < 0:
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                f"{scope_kind} {option} leg {index} duration is negative",
                {scope_kind: option, "leg": index, "element": "duration", "value": _bounded(duration)},
            )
        if start is not None:
            if previous_start is not None and start < previous_start:
                errors.fail(
                    errors.UNEXPECTED_SHAPE,
                    f"{scope_kind} {option} leg {index} starts before the previous leg",
                    {scope_kind: option, "leg": index, "element": start_field},
                )
            previous_start = start


def _validate_itinerary_bounds(legs, *, start, end, option, scope_kind="itinerary"):
    """Reject transit instants that contradict the itinerary's own bounds (§3.1).

    The §3.1 quantities fix which relations are provable, and only those.
    ``access_seconds`` is the first transit departure minus the itinerary start,
    ``egress_seconds`` is the itinerary end minus the last transit arrival, and
    ``station_to_station_seconds`` is their difference — that is, the last transit
    arrival minus the first transit departure. Each of those three terms is a
    component that must never be emitted negative, so all three must be ordered
    before decomposition; and each of the two instants they use must lie inside
    the itinerary it belongs to, since an itinerary cannot depart after it ends or
    arrive before it starts.

    The relations are checked on the instants that are *available* — the effective
    pair from ``_leg_instants``, endpoint fallbacks included — and only those. A
    missing instant skips the relations that need it, so the itinerary stays
    partial (``null`` component plus ``duration_components_incomplete``) instead of
    being filled, clamped or guessed. A known instant is never ignored because a
    *different* timestamp is absent: a body may supply the first departure and the
    last arrival while the last leg's own start is missing, and if those two
    contradict each other ``station_to_station_seconds`` would be -1200.
    """
    transit = []
    for index, leg in enumerate(legs):
        if not durations.is_transit_mode(leg.get("mode")):
            continue
        leg_start, start_field, leg_end, end_field = _leg_instants(leg, option, index)
        transit.append((index, leg_start, start_field, leg_end, end_field))
    if not transit:
        return

    first_index, first_start, first_field, _, _ = transit[0]
    last_index, _, _, last_end, last_field = transit[-1]

    def reject(leg_index, field, boundary, message):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            message,
            {scope_kind: option, "leg": leg_index, "element": field, "boundary": boundary},
        )

    if first_start is not None:
        if first_start < start:
            reject(
                first_index,
                first_field,
                "startTime",
                f"{scope_kind} {option} first transit leg departs before the itinerary start",
            )
        elif first_start > end:
            reject(
                first_index,
                first_field,
                "endTime",
                f"{scope_kind} {option} first transit leg departs after the itinerary end",
            )
    if last_end is not None:
        if last_end > end:
            reject(
                last_index,
                last_field,
                "endTime",
                f"{scope_kind} {option} last transit leg arrives after the itinerary end",
            )
        elif last_end < start:
            reject(
                last_index,
                last_field,
                "startTime",
                f"{scope_kind} {option} last transit leg arrives before the itinerary start",
            )
    if first_start is not None and last_end is not None and last_end < first_start:
        reject(
            last_index,
            last_field,
            "firstDeparture",
            f"{scope_kind} {option} last transit leg arrives before the first transit leg departs",
        )


def _map_place(raw, *, request, dummy_endpoint):
    """The §4.5.2 place object, built on T1's place/cancellation mapper."""
    mapped = places.map_place(raw, dummy_endpoint=dummy_endpoint)
    name = mapped["name"]
    if name == "START" and request.origin.form == FORM_COORDINATE:
        name = request.origin.value
    elif name == "END" and request.destination.form == FORM_COORDINATE:
        name = request.destination.value
    raw_track = raw.get("track") if isinstance(raw, dict) else None
    raw_platform = raw.get("platform") if isinstance(raw, dict) else None
    return {
        "name": name,
        "stop_id": mapped["stop_id"],
        "zone": mapped["zone"],
        "modes": mapped["modes"],
        "lat": mapped["lat"],
        "lon": mapped["lon"],
        "track": raw_track if isinstance(raw_track, str) and raw_track else None,
        "platform": raw_platform if isinstance(raw_platform, str) and raw_platform else None,
        "stop_cancelled": mapped["stop_cancelled"],
        "source_cancelled": mapped["source_cancelled"],
        "source_cancelled_origin": mapped["source_cancelled_origin"],
    }


def _leg_duration(leg):
    duration = leg.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return None
    if isinstance(duration, float) and not math.isfinite(duration):
        return None
    return int(duration)


def _intermediate_stops(leg, *, option, index, scope_kind="itinerary"):
    """The leg's intermediate stops, or a locator-carrying shape rejection.

    A malformed container or entry is a fault on one leg of one alternative, so
    both details and message name that alternative and that leg; an entry fault
    also names the 0-based position inside the array.
    """
    if "intermediateStops" not in leg:
        return None, None
    stops = leg.get("intermediateStops")
    if stops is None:
        return None, None
    if not isinstance(stops, list):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"{scope_kind} {option} leg {index} intermediateStops is not an array",
            {
                scope_kind: option,
                "leg": index,
                "element": "intermediateStops",
                "received": type(stops).__name__,
            },
        )
    names = []
    for position, stop in enumerate(stops):
        if not isinstance(stop, dict):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                f"{scope_kind} {option} leg {index} intermediateStops entry {position} "
                "is not an object",
                {
                    scope_kind: option,
                    "leg": index,
                    "element": "intermediateStops",
                    "stop": position,
                    "received": type(stop).__name__,
                },
            )
        name = stop.get("name")
        if isinstance(name, str):
            names.append(name)
    return len(stops), names[:MAX_INTERMEDIATE_STOP_NAMES]


def _leg_object(raw, *, index, from_place, to_place, option, scope_kind="itinerary"):
    cancellation = places.leg_cancellation(raw)
    points = places.leg_geometry_points(raw)
    if points is None:
        geometry_present = None
    else:
        geometry_present = points != ""
    departure, _, arrival, _ = _leg_instants(raw, option, index, scope_kind)
    scheduled_departure = _optional_instant(
        raw.get("scheduledStartTime"), "scheduledStartTime", option, index, scope_kind
    )
    scheduled_arrival = _optional_instant(
        raw.get("scheduledEndTime"), "scheduledEndTime", option, index, scope_kind
    )
    count, names = _intermediate_stops(
        raw, option=option, index=index, scope_kind=scope_kind
    )
    route = raw.get("routeShortName")
    if not isinstance(route, str) or not route:
        route = raw.get("routeLongName")
    if not isinstance(route, str) or not route:
        route = None
    headsign = raw.get("headsign")
    trip_id = raw.get("tripId")
    agency_name = raw.get("agencyName")
    return {
        "index": index,
        "mode": raw.get("mode"),
        "from": from_place,
        "to": to_place,
        "departure": _timestamp_or_none(departure, from_place["zone"]),
        "arrival": _timestamp_or_none(arrival, to_place["zone"]),
        "scheduled_departure": _timestamp_or_none(scheduled_departure, from_place["zone"]),
        "scheduled_arrival": _timestamp_or_none(scheduled_arrival, to_place["zone"]),
        "realtime": places.tristate(raw.get("realTime")),
        "scheduled": places.tristate(raw.get("scheduled")),
        "duration_seconds": _leg_duration(raw),
        "route": route,
        "headsign": headsign if isinstance(headsign, str) else None,
        "trip_id": trip_id if isinstance(trip_id, str) else None,
        "agency_name": agency_name if isinstance(agency_name, str) else None,
        "cancelled": cancellation["cancelled"],
        "walking_route_unavailable": cancellation["walking_route_unavailable"],
        "duration_is_estimate": cancellation["duration_is_estimate"],
        "note": cancellation["note"],
        "intermediate_stop_count": count,
        "intermediate_stop_names": names,
        "geometry_present": geometry_present,
    }


def _timestamp_or_none(instant, zone):
    if instant is None:
        return None
    return rendering.timestamp(instant, zone=zone)


def _itinerary_zones(legs, origin_zone, destination_zone):
    """The endpoint display zones of one itinerary.

    The response's ``from``/``to`` place is the endpoint for a stop-ID query, but
    for a coordinate query it is the query's pseudo-place (``START``/``END``) and
    carries no ``tz``. The terminal leg's place is then the endpoint place the
    payload actually supplied, so it supplies the display zone. Both are payload
    values; neither is inferred.

    A leg place with no ``tz`` (the ``START``/``END`` pseudo-place again) is
    skipped rather than ending the search: the first place the payload gives a
    zone for at the origin end, and the last at the destination end, are that
    endpoint's display zone. The response's own endpoint place is the last resort.
    """
    origin = None
    for leg in legs:
        origin = _zone_text((leg.get("from") or {}).get("tz"))
        if origin:
            break
    destination = None
    for leg in reversed(legs):
        destination = _zone_text((leg.get("to") or {}).get("tz"))
        if destination:
            break
    return (origin or origin_zone, destination or destination_zone)


def _map_itinerary(
    raw,
    *,
    option,
    origin_zone,
    destination_zone,
    places_by_leg,
    warnings,
    scope_kind="itinerary",
):
    scope = f"{scope_kind}:{option}"
    if not isinstance(raw, dict):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"{scope_kind} {option} is not an object",
            {scope_kind: option},
        )
    legs = raw.get("legs")
    if not isinstance(legs, list):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"{scope_kind} {option} legs is not an array",
            {scope_kind: option, "element": "legs", "received": type(legs).__name__},
        )
    _validate_legs(legs, option, scope_kind)

    start = _instant(raw.get("startTime"), "startTime", option, scope_kind)
    end = _instant(raw.get("endTime"), "endTime", option, scope_kind)
    if end < start:
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"{scope_kind} {option} ends before it starts",
            {scope_kind: option, "element": "endTime"},
        )
    itinerary_id = raw.get("id")
    if not isinstance(itinerary_id, str):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"{scope_kind} {option} has no string id",
            {scope_kind: option, "element": "id"},
        )
    transfers = raw.get("transfers")
    if isinstance(transfers, bool) or not isinstance(transfers, int):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            f"{scope_kind} {option} transfers is not an integer",
            {scope_kind: option, "element": "transfers"},
        )

    origin_zone, destination_zone = _itinerary_zones(legs, origin_zone, destination_zone)

    _validate_itinerary_bounds(
        legs, start=start, end=end, option=option, scope_kind=scope_kind
    )
    breakdown = durations.decompose_itinerary(raw)
    facts = durations.calendar_facts(start, end, origin_zone, destination_zone)
    _add_warnings(warnings, breakdown.warnings, scope)
    _add_warnings(warnings, facts.warnings, scope)
    if breakdown.reason == durations.NO_TRANSIT_LEG:
        _add_warnings(warnings, (NO_TRANSIT_LEG,), scope)
    if origin_zone is None or destination_zone is None:
        _add_warnings(warnings, (durations.ENDPOINT_ZONE_UNAVAILABLE,), scope)

    mapped_legs = [
        _leg_object(
            raw_leg,
            index=index,
            from_place=from_place,
            to_place=to_place,
            option=option,
            scope_kind=scope_kind,
        )
        for index, (raw_leg, (from_place, to_place)) in enumerate(zip(legs, places_by_leg))
    ]
    transit_leg_count = sum(1 for leg in legs if durations.is_transit_mode(leg.get("mode")))
    realtime_present = any(leg["realtime"] is True for leg in mapped_legs)

    return {
        "option": option,
        "itinerary_id": itinerary_id,
        "departure": rendering.timestamp(start, zone=origin_zone),
        "arrival": rendering.timestamp(end, zone=destination_zone),
        "elapsed_seconds": breakdown.elapsed_seconds,
        "duration_basis": breakdown.duration_basis,
        "access_seconds": breakdown.access_seconds,
        "egress_seconds": breakdown.egress_seconds,
        "transfer_walk_seconds": breakdown.transfer_walk_seconds,
        "in_transit_seconds": breakdown.in_transit_seconds,
        "waiting_seconds": breakdown.waiting_seconds,
        "station_to_station_seconds": breakdown.station_to_station_seconds,
        "changes": transfers if transit_leg_count else None,
        "transit_leg_count": transit_leg_count,
        "arrival_date_offset_days": facts.arrival_date_offset_days,
        "has_overnight": facts.has_overnight,
        "realtime_present": realtime_present,
        "legs": mapped_legs,
    }


def _map_places(raw_legs, *, request):
    """Project every leg's two places, with itinerary-wide dummy attribution."""
    pairs = places.map_itinerary_places(raw_legs)
    dummy = places.dummy_walk_endpoints(raw_legs)
    mapped = []
    for leg, (from_place, to_place) in zip(raw_legs, pairs):
        raw_from = leg.get("from") or {}
        raw_to = leg.get("to") or {}
        mapped.append(
            (
                _map_place(raw_from, request=request, dummy_endpoint=places.place_key(raw_from) in dummy),
                _map_place(raw_to, request=request, dummy_endpoint=places.place_key(raw_to) in dummy),
            )
        )
    return mapped


def _map_itineraries(raw_list, *, request, origin_zone, destination_zone, warnings, scope_kind="itinerary"):
    """Validate, map and number a list of itineraries in the payload's order."""
    mapped = []
    for position, raw in enumerate(raw_list):
        if not isinstance(raw, dict):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                f"{scope_kind} {position + 1} is not an object",
                {scope_kind: position + 1},
            )
        legs = raw.get("legs")
        if not isinstance(legs, list):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                f"{scope_kind} {position + 1} legs is not an array",
                {scope_kind: position + 1, "element": "legs"},
            )
        _validate_legs(legs, position + 1, scope_kind)
        places_by_leg = _map_places(legs, request=request)
        mapped.append(
            _map_itinerary(
                raw,
                option=position + 1,
                origin_zone=origin_zone,
                destination_zone=destination_zone,
                places_by_leg=places_by_leg,
                warnings=warnings,
                scope_kind=scope_kind,
            )
        )
    return mapped


def _response_zones(from_raw, to_raw):
    origin = _zone_text(from_raw.get("tz")) if isinstance(from_raw, dict) else None
    destination = _zone_text(to_raw.get("tz")) if isinstance(to_raw, dict) else None
    return origin, destination


def _query_object(
    request,
    plan_time,
    *,
    data,
    returned_count,
    upstream_count,
    direct_count,
    realtime_present,
):
    interpreted = plan_time.frame.to_object()["interpreted_instant"]
    truncated = upstream_count > returned_count
    return {
        "control": request.control,
        "requested_instant": dict(interpreted),
        "effective_instant": dict(interpreted),
        "search_window_seconds": request.search_window_seconds,
        "minimum_requested": request.num_itineraries,
        "upstream_count": upstream_count,
        "returned_count": returned_count,
        "max_results": request.max_results,
        "truncated": truncated,
        "truncation_reason": TRUNCATION_MAX_RESULTS if truncated else TRUNCATION_NONE,
        "dropped_count": upstream_count - returned_count,
        "previous_page_cursor": data.get("previousPageCursor"),
        "next_page_cursor": data.get("nextPageCursor"),
        "direct_count": direct_count,
        "requested_modes": list(request.transit_modes),
        "applied_modes": ",".join(request.transit_modes),
        "realtime_present": realtime_present,
    }


def _continuation_object(data, frozen):
    def direction(key):
        cursor = data.get(key)
        if not isinstance(cursor, str) or not cursor:
            return None
        return {"cursor": cursor, "query": json.loads(json.dumps(frozen))}

    return {
        "previous": direction("previousPageCursor"),
        "next": direction("nextPageCursor"),
    }


def _constraints_active(request):
    """Constraints supplied that restrict the search (§4.2).

    On a first call only arguments the caller actually supplied count, so a
    default is never presented as the caller's restriction. Suppliedness is a
    presentation-only value and is deliberately not part of the frozen query, so a
    continuation page reports the frozen constraints that differ from the wrapper
    defaults — the same active conditions, visible beside a no-routes reason.
    """
    active = []
    for name in CONSTRAINT_FIELDS:
        value = getattr(request, name)
        if request.continuation is not None:
            default = INTEGER_BOUNDS[name][2]
            if value != default:
                active.append({"name": name, "value": value})
        elif name in request.supplied:
            active.append({"name": name, "value": value})
    if tuple(request.transit_modes) != DEFAULT_TRANSIT_MODES:
        active.append({"name": "transit_modes", "value": list(request.transit_modes)})
    return active


def _predicate_failures(data, status):
    """Which elements of §4.2's no-routes predicate failed, in table order."""
    failed = []
    if status != 200:
        failed.append("status_200")
    if not isinstance(data, dict):
        return failed + ["object"]
    if "requestParameters" not in data:
        failed.append("requestParameters")
    if "debugOutput" not in data:
        failed.append("debugOutput")
    if not isinstance(data.get("direct"), list):
        failed.append("direct")
    for name in ("from", "to"):
        place = data.get(name)
        if not isinstance(place, dict):
            failed.append(name)
        elif not isinstance(place.get("name"), str) or not place.get("name"):
            failed.append(name)
    if "previousPageCursor" not in data:
        failed.append("previousPageCursor")
    if "nextPageCursor" not in data:
        failed.append("nextPageCursor")
    return failed


def _check_agreement(request, plan_time, from_raw, to_raw):
    resolution = plan_time.zone_resolution
    if resolution is None or resolution.source not in timezones.COMPARABLE_SOURCES:
        return
    label = plan_time.endpoint_label or _rotation(request.control)
    raw = from_raw if label == "origin" else to_raw
    response_zone = raw.get("tz") if isinstance(raw, dict) else None
    timezones.check_zone_agreement(resolution, response_zone, endpoint_label=label)


def map_response(data, *, request, plan_time, status=200):
    """Classify and map one plan body (§2.7 stage 3, §4.2, §4.5)."""
    if not isinstance(data, dict):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "plan response is not a JSON object",
            {"status": status, "received": type(data).__name__},
        )
    from_raw = data.get("from")
    to_raw = data.get("to")
    _check_agreement(request, plan_time, from_raw, to_raw)

    itineraries_raw = data.get("itineraries")
    if not isinstance(itineraries_raw, list):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "plan response itineraries is not an array",
            {
                "element": "itineraries",
                "failed": ["itineraries"],
                "received": type(itineraries_raw).__name__,
            },
        )

    frozen = frozen_query(request, plan_time)
    warnings = []
    origin_zone, destination_zone = _response_zones(from_raw, to_raw)

    if not itineraries_raw:
        failed = _predicate_failures(data, status)
        if failed:
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "an empty itineraries array alone does not prove an empty result",
                {
                    "failed": failed,
                    "status": status,
                    "remedy": "the pinned schema requires every structural member; "
                    "an invalid body is never reported as no_routes",
                },
            )
        direct_raw = data.get("direct") or []
        direct_out = []
        if request.include_direct and direct_raw:
            direct_out = _map_itineraries(
                direct_raw,
                request=request,
                origin_zone=origin_zone,
                destination_zone=destination_zone,
                warnings=warnings,
                scope_kind="direct",
            )
            _add_warnings(warnings, (DIRECT_ALTERNATIVES_ONLY,), "response")
        query = _query_object(
            request,
            plan_time,
            data=data,
            returned_count=0,
            upstream_count=0,
            direct_count=len(direct_out),
            realtime_present=False,
        )
        payload = {
            "no_routes_reason": NO_ROUTES_DIRECT if direct_raw else NO_ROUTES_DETERMINED,
            "constraints_active": _constraints_active(request),
            "time_frame": plan_time.frame.to_object(),
            "query": query,
            "continuation": _continuation_object(data, frozen),
            "from": _map_place(from_raw, request=request, dummy_endpoint=False),
            "to": _map_place(to_raw, request=request, dummy_endpoint=False),
            "itineraries": [],
        }
        if direct_out:
            payload["direct"] = direct_out
        payload["warnings"] = _dedupe(warnings)
        return errors.success_result(errors.NO_ROUTES, **payload)

    for name, raw in (("from", from_raw), ("to", to_raw)):
        if not isinstance(raw, dict):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                f"plan response {name} is not an object",
                {"element": name, "received": type(raw).__name__},
            )
    direct_raw = data.get("direct", [])
    if not isinstance(direct_raw, list):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "plan response direct is not an array",
            {"element": "direct", "received": type(direct_raw).__name__},
        )

    cap = request.max_results
    selected = itineraries_raw if cap is None else itineraries_raw[:cap]
    itineraries = _map_itineraries(
        selected,
        request=request,
        origin_zone=origin_zone,
        destination_zone=destination_zone,
        warnings=warnings,
    )
    direct_out = []
    if request.include_direct and direct_raw:
        direct_out = _map_itineraries(
            direct_raw,
            request=request,
            origin_zone=origin_zone,
            destination_zone=destination_zone,
            warnings=warnings,
            scope_kind="direct",
        )
    if itineraries and not any(item["realtime_present"] for item in itineraries):
        _add_warnings(warnings, (REALTIME_UNAVAILABLE,), "query")

    query = _query_object(
        request,
        plan_time,
        data=data,
        returned_count=len(itineraries),
        upstream_count=len(itineraries_raw),
        direct_count=len(direct_out),
        realtime_present=any(item["realtime_present"] for item in itineraries),
    )

    payload = {
        "constraints_active": _constraints_active(request),
        "time_frame": plan_time.frame.to_object(),
        "query": query,
        "continuation": _continuation_object(data, frozen),
        "from": _map_place(from_raw, request=request, dummy_endpoint=False),
        "to": _map_place(to_raw, request=request, dummy_endpoint=False),
        "itineraries": itineraries,
    }
    if direct_out:
        payload["direct"] = direct_out
    payload["warnings"] = _dedupe(warnings)
    return errors.success_result(errors.OK, **payload)


# ── the tool entry point ───────────────────────────────────────────


def execute(request, *, transport=None, now=None):
    """Resolve the time, issue the one GET and map the result."""
    plan_time = resolve_time(request, transport=transport, now=now)
    params = request_params(request, plan_time)
    data, status = _plan_get(params, transport=transport)
    return map_response(data, request=request, plan_time=plan_time, status=status)


def plan(args, *, transport=None, now=None):
    """``transit_plan``: the callable boundary T5 wires into ``server.py``.

    ``args`` is the caller's raw argument mapping; ``transport`` is the
    ``http.api_get`` transport seam (``fn(request, timeout)``) and ``now`` is an
    aware datetime or a zero-argument callable, resolved exactly once.

    Returns a success object (``ok``/``no_routes``) or the constant four-field
    error object. A caller-input fault never raises out of here, and no request is
    issued for one.
    """
    try:
        request = parse_request(args)
        return execute(request, transport=transport, now=now)
    except errors.ToolError as exc:
        return exc.to_result()


__all__ = [
    "PATH",
    "DEFAULT_MAX_TRANSFERS", "DEFAULT_NUM_ITINERARIES", "DEFAULT_SEARCH_WINDOW_SECONDS",
    "DEFAULT_MAX_ACCESS_WALK_MINUTES", "DEFAULT_MAX_EGRESS_WALK_MINUTES",
    "DEFAULT_MIN_TRANSFER_MINUTES", "DEFAULT_INCLUDE_DIRECT", "DEFAULT_TRANSIT_MODES",
    "INTEGER_BOUNDS", "ACCEPTED_MODES", "REJECTED_STREET_MODES", "DEBUG_MODE_PREFIX",
    "MAX_CONTINUATION_BYTES", "MAX_CURSOR_BYTES", "MAX_INTERMEDIATE_STOP_NAMES",
    "FORM_STOP_ID", "FORM_COORDINATE", "ENDPOINT_FORMS",
    "CONTROL_DEPART_AFTER", "CONTROL_ARRIVE_BY", "CONTROLS",
    "NO_ROUTES_DETERMINED", "NO_ROUTES_DIRECT", "NO_ROUTES_REASONS",
    "TRUNCATION_NONE", "TRUNCATION_MAX_RESULTS",
    "REALTIME_UNAVAILABLE", "DIRECT_ALTERNATIVES_ONLY", "NO_TRANSIT_LEG",
    "ARGUMENTS", "CONTINUATION_FIELDS", "FROZEN_FIELD_ORDER", "FROZEN_FIELDS",
    "CONSTRAINT_FIELDS", "PLACE_FIELDS",
    "Endpoint", "Continuation", "PlanRequest", "PlanTime",
    "parse_request", "resolve_time", "frozen_query", "request_params",
    "map_response", "execute", "plan",
]
