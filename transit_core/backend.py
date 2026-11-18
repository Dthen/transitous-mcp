"""The protocol-independent application boundary for the Transitous operations.

This module is what the stateless MCP adapter and any future native CLI share. It
owns one ordered inventory of canonical operations, the complete argument mapping
for each, and the one translation from a classified :class:`errors.ToolError` to
the constant error result. It deliberately contains no JSON-RPC message, era
envelope, ``isError`` flag or tool registry, and it never imports ``server`` or a
test framework.

Shape of the boundary
    ``run(operation, arguments)`` accepts a raw mapping and returns the existing
    result object — the canonical envelope for the mapped tools, or the decoded
    raw provider payload for ``health``, ``refresh`` and ``rentals``. Only
    ``ToolError`` becomes an error result; an unexpected programming failure
    propagates unchanged, so a defect is never reported as a user-input or
    upstream fault. Canonical operation names carry no ``transit_`` prefix; the
    MCP adapter translates its tool names onto this inventory.

Request assembly
    ``health``, ``refresh`` and ``rentals`` were previously assembled inside
    ``server.py``. They live here now, next to the other operations, with their
    paths, query names, defaults and validation unchanged. Each accepts the same
    explicit ``transport`` seam as :func:`transit_core.http.api_get`, so a test can
    observe the assembled request without a socket.

Raw-output exception
    ``health``, ``refresh`` and ``rentals`` return their successful provider body
    unchanged — no ``outcome``, no wrapper, no normalisation. Failures still use
    the canonical four-field error object, because the failure taxonomy is shared
    by every operation.
"""

from . import boards, errors, geocode, http, onetomany, plan

#: Paths of the three operations assembled here. Unchanged from ``server.py``.
HEALTH_PATH = "/api/v1/health"
REFRESH_PATH = "/api/v6/refresh-itinerary"
RENTALS_PATH = "/api/v1/rentals"

#: The rentals endpoint's own default when ``radius`` is omitted or JSON null.
DEFAULT_RENTALS_RADIUS_M = 1000


# ── small shared helpers ────────────────────────────────────────────


def bounded_value(value, limit=80):
    """A short, printable rendering of a caller-supplied value."""
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def arguments_error(value):
    """The canonical result for a tool argument bag that is not an object."""
    return errors.error_result(
        errors.VALUE_OUT_OF_RANGE,
        "tool arguments must be an object",
        {"argument": "arguments", "value": bounded_value(value), "expected": "object"},
    )


def _required_id(value, argument):
    """A raw endpoint's identifier argument: a non-empty string, verbatim."""
    if not isinstance(value, str) or not value.strip():
        errors.fail(
            errors.VALUE_OUT_OF_RANGE,
            f"{argument} must be a non-empty string",
            {"argument": argument, "value": bounded_value(value), "expected": "non-empty string"},
        )
    return value


def validate_rentals_radius(radius):
    """The rentals search radius: an integer number of metres, else an error.

    ``None`` — the argument omitted or JSON null — keeps the endpoint default
    :data:`DEFAULT_RENTALS_RADIUS_M`. This endpoint publishes no 1..5000 window
    (that bound belongs to ``transit_nearby_stops``), so no positivity or maximum
    rule is invented here: any integer is forwarded verbatim. Booleans are not
    integers, and strings, floats and containers are rejected before any request
    rather than reaching the wire as Python ``str``/``repr`` text.
    """
    if radius is None:
        return DEFAULT_RENTALS_RADIUS_M
    if isinstance(radius, bool) or not isinstance(radius, int):
        errors.fail(
            errors.VALUE_OUT_OF_RANGE,
            "radius must be an integer number of metres",
            {"argument": "radius", "value": bounded_value(radius), "expected": "integer"},
        )
    return radius


# ── the three extracted raw endpoints ───────────────────────────────


def health(*, transport=None):
    """``transit_health``: the decoded provider health payload, unchanged."""
    return http.api_get(HEALTH_PATH, transport=transport)


def refresh(itinerary_id, *, transport=None):
    """``transit_refresh``: one itinerary's decoded provider payload, unchanged."""
    return http.api_get(
        REFRESH_PATH,
        {"itineraryId": _required_id(itinerary_id, "itinerary_id")},
        transport=transport,
    )


def rentals(lat, lon, radius=None, *, transport=None):
    """``transit_rentals``: the decoded sharing-system payload, unchanged."""
    lat = geocode.validate_latitude(lat, argument="lat")
    lon = geocode.validate_longitude(lon, argument="lon")
    radius = validate_rentals_radius(radius)
    return http.api_get(
        RENTALS_PATH,
        {"point": f"{lat},{lon}", "radius": radius},
        transport=transport,
    )


# ── argument mappings ───────────────────────────────────────────────
#
# Each handler forwards the complete argument map the adapter used to build. The
# calls are written out rather than generated from a table so the mapping stays
# inspectable: an argument added or removed in one place is a visible edit.


def _plan(args, transport):
    return plan.plan(args, transport=transport)


def _geocode(args, transport):
    return geocode.geocode(
        args.get("query"),
        types=args.get("types"),
        modes=args.get("modes"),
        near=args.get("near"),
        near_bias=args.get("near_bias"),
        num_results=args.get("num_results"),
        language=args.get("language"),
        transport=transport,
    )


def _departures(args, transport):
    return boards.departures(
        args.get("stop_id"),
        count=args.get("count"),
        time=args.get("time"),
        window=args.get("window"),
        timezone=args.get("timezone"),
        endpoint_timezone=args.get("endpoint_timezone"),
        transport=transport,
    )


def _nearby_stops(args, transport):
    return geocode.nearby_stops(
        args.get("lat"), args.get("lon"), args.get("radius"), transport=transport
    )


def _reverse_geocode(args, transport):
    return geocode.reverse_geocode(
        args.get("lat"),
        args.get("lon"),
        types=args.get("types"),
        num_results=args.get("num_results"),
        transport=transport,
    )


def _trip(args, transport):
    return boards.trip(args.get("trip_id"), transport=transport)


def _health(args, transport):
    return health(transport=transport)


def _reachable(args, transport):
    return onetomany.reachable(
        args.get("lat"),
        args.get("lon"),
        max_time_min=args.get("max_time_min"),
        time=args.get("time"),
        arrive_by=args.get("arrive_by"),
        timezone=args.get("timezone"),
        endpoint_timezone=args.get("endpoint_timezone"),
        transport=transport,
    )


def _refresh(args, transport):
    return refresh(args.get("itinerary_id"), transport=transport)


def _rentals(args, transport):
    return rentals(
        args.get("lat"), args.get("lon"), args.get("radius"), transport=transport
    )


def _one_to_many(args, transport):
    # §4.12.2 publishes `mode` as optional with default WALK. Forward the keyword
    # only when the caller supplied it, so an omitted key leaves the argument slot
    # unfilled and the core applies its single WALK default. A present value --
    # including JSON null -- is forwarded exactly and stays the core's unknown_mode
    # rejection. Never coalesce to WALK here: that would invent a second default
    # and accept an explicit null.
    supplied = {"mode": args["mode"]} if "mode" in args else {}
    return onetomany.one_to_many(
        args.get("from_lat"),
        args.get("from_lon"),
        args.get("destinations"),
        max_sec=args.get("max_sec"),
        arrive_by=args.get("arrive_by"),
        transport=transport,
        **supplied,
    )


def _one_to_many_transit(args, transport):
    return onetomany.one_to_many_transit(
        args.get("from_lat"),
        args.get("from_lon"),
        args.get("destinations"),
        time=args.get("time"),
        arrive_by=args.get("arrive_by"),
        max_transfers=args.get("max_transfers"),
        transit_modes=args.get("transit_modes"),
        max_access_walk_minutes=args.get("max_access_walk_minutes"),
        max_egress_walk_minutes=args.get("max_egress_walk_minutes"),
        max_travel_minutes=args.get("max_travel_minutes"),
        timezone=args.get("timezone"),
        endpoint_timezone=args.get("endpoint_timezone"),
        transport=transport,
    )


def _map_trips(args, transport):
    return boards.map_trips(
        args.get("min_lat"),
        args.get("min_lon"),
        args.get("max_lat"),
        args.get("max_lon"),
        args.get("start_time"),
        args.get("end_time"),
        zoom=args.get("zoom"),
        timezone=args.get("timezone"),
        transport=transport,
    )


_HANDLERS = {
    "plan": _plan,
    "geocode": _geocode,
    "departures": _departures,
    "nearby_stops": _nearby_stops,
    "reverse_geocode": _reverse_geocode,
    "trip": _trip,
    "health": _health,
    "reachable": _reachable,
    "refresh": _refresh,
    "rentals": _rentals,
    "one_to_many": _one_to_many,
    "one_to_many_transit": _one_to_many_transit,
    "map_trips": _map_trips,
}

#: The ordered public inventory comes from the actual handlers, not a second
#: manually maintained list. The adapter maps ``transit_<operation>`` to it.
OPERATIONS = tuple(_HANDLERS)


# ── the one entry point ─────────────────────────────────────────────


def run(operation, arguments, *, transport=None):
    """Run one canonical operation and return its existing result object.

    ``arguments`` is the caller's raw mapping; ``transport`` is the same explicit
    ``http.api_get`` seam the core functions accept. A classified ``ToolError``
    becomes the canonical four-field error result; every other exception
    propagates, because an unexpected failure is a programming fault, not a user
    or upstream error. An unmapped operation name is a closed-set value fault,
    like an unknown MCP tool name in the adapter.
    """
    if not isinstance(arguments, dict):
        return arguments_error(arguments)
    handler = _HANDLERS.get(operation) if isinstance(operation, str) else None
    if handler is None:
        return errors.error_result(
            errors.VALUE_OUT_OF_RANGE,
            f"Unknown operation: {operation}",
            {
                "argument": "name",
                "value": bounded_value(operation),
                "accepted": list(OPERATIONS),
            },
        )
    try:
        return handler(arguments, transport)
    except errors.ToolError as exc:
        return exc.to_result()


__all__ = [
    "HEALTH_PATH",
    "REFRESH_PATH",
    "RENTALS_PATH",
    "DEFAULT_RENTALS_RADIUS_M",
    "OPERATIONS",
    "bounded_value",
    "arguments_error",
    "validate_rentals_radius",
    "health",
    "refresh",
    "rentals",
    "run",
]
