"""Place mapping, cancellation tri-state and dummy-walk provenance (§4.13, §2.6).

The upstream marks a street leg ``cancelled: true`` when street routing found no
path and emits a dummy leg, setting the same flag on both endpoint places and
leaving the geometry empty. That flag is **not** a cancelled service and says
nothing about the stop.

Dummy-walk predicate: ``mode == "WALK"``, ``cancelled is true``, and either the
geometry is absent or ``legGeometry.points`` is the empty string. The predicate
tests the *points*, not the truthiness of the geometry object: the retained dummy
objects are ``{"points": "", "precision": 6, "length": 0}`` and are themselves
non-empty. A geometry of the wrong type is malformed, not evidence of a dummy walk.
The predicate is evaluated per leg and its endpoint attribution is carried through
the whole itinerary, so a place that is an endpoint of a dummy walk anywhere in the
itinerary never reports ``stop_cancelled: true`` — it reports ``null`` with
``source_cancelled`` and ``source_cancelled_origin`` preserved.

Mapping is honest by construction:

===============================  ===========================================================
raw condition                    reported
===============================  ===========================================================
dummy-walk street leg            walking_route_unavailable true, duration_is_estimate true,
                                 cancelled null, note about the estimated duration
transit leg ``cancelled: true``  cancelled true
place cancelled + dummy endpoint stop_cancelled null, source_cancelled true,
                                 source_cancelled_origin "walking_route_unavailable"
place cancelled elsewhere        stop_cancelled true, source_cancelled true, origin "service"
place cancelled explicitly false stop_cancelled false
place without ``cancelled``      stop_cancelled null, source_cancelled null
===============================  ===========================================================
"""

from . import durations, errors, rendering

WALKING_ROUTE_UNAVAILABLE = "walking_route_unavailable"
SOURCE_ORIGIN_WALKING = "walking_route_unavailable"
SOURCE_ORIGIN_SERVICE = "service"

WALKING_NOTE = "the walking route could not be calculated and the duration is an estimate"

PLACE_FIELDS = (
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
)


def tristate(value):
    """``True``/``False`` only when the payload supplied a boolean, else ``None``.

    No coercion: ``"true"``, ``1``, ``0`` and ``{}`` are all ``None``. Absent never
    means false and never means "on time".
    """
    return value if isinstance(value, bool) else None


def leg_geometry_points(raw_leg):
    """The decoded geometry's ``points`` string.

    Returns ``None`` when no geometry (or no ``points`` member) is present — that is
    the "absent" half of the dummy-walk predicate. Raises ``malformed_upstream`` /
    ``unexpected_shape`` when a geometry object is present but incorrectly typed.
    """
    if not isinstance(raw_leg, dict):
        errors.fail(errors.UNEXPECTED_SHAPE, "leg is not an object", {"leg": repr(raw_leg)[:120]})
    if "legGeometry" not in raw_leg:
        return None
    geometry = raw_leg.get("legGeometry")
    if geometry is None:
        return None
    if not isinstance(geometry, dict):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "legGeometry is not an object",
            {"legGeometry_type": type(geometry).__name__},
        )
    if "points" not in geometry:
        return None
    points = geometry.get("points")
    if not isinstance(points, str):
        errors.fail(
            errors.UNEXPECTED_SHAPE,
            "legGeometry.points is not a string",
            {"points_type": type(points).__name__},
        )
    return points


def is_dummy_walk(raw_leg):
    """True only for the upstream's failed-street-route placeholder leg."""
    if not isinstance(raw_leg, dict):
        errors.fail(errors.UNEXPECTED_SHAPE, "leg is not an object", {"leg": repr(raw_leg)[:120]})
    if raw_leg.get("mode") != "WALK":
        return False
    if tristate(raw_leg.get("cancelled")) is not True:
        return False
    points = leg_geometry_points(raw_leg)
    return points is None or points == ""


def place_key(raw_place):
    """Identity of a place across legs: name, stop id and coordinate."""
    if not isinstance(raw_place, dict):
        return (None, None, None, None)
    return (
        raw_place.get("name"),
        raw_place.get("stopId"),
        raw_place.get("lat"),
        raw_place.get("lon"),
    )


def dummy_walk_endpoints(raw_legs):
    """Keys of every place that terminates a dummy-walk leg in this itinerary."""
    keys = set()
    for leg in raw_legs or ():
        if not isinstance(leg, dict):
            continue
        if is_dummy_walk(leg):
            keys.add(place_key(leg.get("from")))
            keys.add(place_key(leg.get("to")))
    return frozenset(keys)


def map_place(raw, *, dummy_endpoint=False):
    """Map a pinned ``Place`` to the canonical place object.

    ``dummy_endpoint`` must be the itinerary-wide verdict for this place (see
    :func:`dummy_walk_endpoints`), not a per-leg guess.
    """
    if not isinstance(raw, dict):
        errors.fail(errors.UNEXPECTED_SHAPE, "place is not an object", {"place": repr(raw)[:120]})
    raw_cancelled = tristate(raw.get("cancelled"))

    if raw_cancelled is None:
        stop_cancelled = None
        source_cancelled = None
        origin = None
    elif raw_cancelled is False:
        stop_cancelled = False
        source_cancelled = False
        origin = None
    elif dummy_endpoint:
        stop_cancelled = None
        source_cancelled = True
        origin = SOURCE_ORIGIN_WALKING
    else:
        stop_cancelled = True
        source_cancelled = True
        origin = SOURCE_ORIGIN_SERVICE

    zone = raw.get("tz")
    if not isinstance(zone, str) or not zone.strip():
        zone = None

    modes = raw.get("modes")
    stop_id = raw.get("stopId")
    parent_id = raw.get("parentId")
    return {
        "name": raw.get("name"),
        "stop_id": stop_id if isinstance(stop_id, str) else None,
        "parent_id": parent_id if isinstance(parent_id, str) else None,
        "lat": raw.get("lat"),
        "lon": raw.get("lon"),
        "zone": zone,
        "zone_source": rendering.RESPONSE_PLACE if zone else rendering.UNAVAILABLE,
        "vertex_type": raw.get("vertexType"),
        "modes": list(modes) if isinstance(modes, list) else None,
        "stop_code": raw.get("stopCode"),
        "stop_cancelled": stop_cancelled,
        "source_cancelled": source_cancelled,
        "source_cancelled_origin": origin,
    }


def map_itinerary_places(raw_legs):
    """Map every leg's two places, with itinerary-wide dummy-endpoint attribution.

    Returns one ``(from_place, to_place)`` pair per leg, aligned with ``raw_legs``.
    """
    legs = list(raw_legs or ())
    dummy = dummy_walk_endpoints(legs)
    pairs = []
    for leg in legs:
        leg_from = leg.get("from") if isinstance(leg, dict) else None
        leg_to = leg.get("to") if isinstance(leg, dict) else None
        pairs.append(
            (
                map_place(leg_from or {}, dummy_endpoint=place_key(leg_from) in dummy),
                map_place(leg_to or {}, dummy_endpoint=place_key(leg_to) in dummy),
            )
        )
    return tuple(pairs)


def leg_cancellation(raw_leg):
    """Cancellation and walking-failure flags for one leg.

    A transit leg reports the payload's ``cancelled`` tri-state. A street leg never
    reports a cancelled service; a failed street route is reported as an unroutable
    estimate and is never dropped.
    """
    dummy = is_dummy_walk(raw_leg)
    if dummy:
        return {
            "cancelled": None,
            "walking_route_unavailable": True,
            "duration_is_estimate": True,
            "note": WALKING_NOTE,
        }
    mode = raw_leg.get("mode") if isinstance(raw_leg, dict) else None
    if not durations.is_transit_mode(mode):
        return {
            "cancelled": None,
            "walking_route_unavailable": False,
            "duration_is_estimate": False,
            "note": None,
        }
    return {
        "cancelled": tristate(raw_leg.get("cancelled")),
        "walking_route_unavailable": False,
        "duration_is_estimate": False,
        "note": None,
    }


def map_stop_time_cancellation(raw_stop_time):
    """``stop_cancelled`` / ``trip_cancelled`` tri-state from a pinned ``StopTime``.

    Only the supplied values are mapped. ``StopTime.realTime``, ``cancelled`` and
    ``tripCancelled`` are required members of the pinned schema; a body that omits
    one is a shape failure for the board mapper, not evidence of a scheduled or
    running service.
    """
    if not isinstance(raw_stop_time, dict):
        errors.fail(
            errors.UNEXPECTED_SHAPE, "stop time is not an object", {"stop_time": repr(raw_stop_time)[:120]}
        )
    return {
        "stop_cancelled": tristate(raw_stop_time.get("cancelled")),
        "trip_cancelled": tristate(raw_stop_time.get("tripCancelled")),
    }


__all__ = [
    "WALKING_ROUTE_UNAVAILABLE", "SOURCE_ORIGIN_WALKING", "SOURCE_ORIGIN_SERVICE",
    "WALKING_NOTE", "PLACE_FIELDS",
    "tristate", "leg_geometry_points", "is_dummy_walk", "place_key",
    "dummy_walk_endpoints", "map_place", "map_itinerary_places", "leg_cancellation",
    "map_stop_time_cancellation",
]
