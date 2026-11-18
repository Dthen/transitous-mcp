"""The one owner of the pinned request-mode vocabulary (§4.4.1).

Three modules validate caller-supplied modes and must agree exactly: ``plan`` and
``onetomany`` for transit requests, and ``geocode`` for the stop-mode filter.
Each used to carry its own literal copy of the same members, so a value could be
added to one and forgotten in another. The sets live here; the other modules
import them and keep their historical public names as aliases.

This is the **request** vocabulary only. ``durations.TRANSIT_MODES`` classifies
response legs, ``onetomany.STREET_PROFILES`` names the three street profiles of
``transit_one_to_many``, and the integer/boolean validators stay where they are.
Merging any of those into this module would change a contract, not just a
spelling.

``TRANSIT_REQUEST_MODE_ORDER`` and ``REJECTED_STREET_MODE_ORDER`` exist because
``geocode`` sends the caller's order on the wire and echoes the published order
in ``accepted_modes`` / ``street_modes`` diagnostics. The frozensets serve the
membership checks in ``plan`` and ``onetomany``.
"""

#: Every request ``Mode`` member in specification order (§4.4.1), including
#: aliases the provider still understands. This tuple is the authoritative list;
#: ``geocode`` also uses it for ordered diagnostics.
TRANSIT_REQUEST_MODE_ORDER = (
    "TRANSIT", "TRAM", "SUBWAY", "METRO", "BUS", "COACH", "RAIL",
    "HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "REGIONAL_FAST_RAIL",
    "REGIONAL_RAIL", "SUBURBAN", "FUNICULAR", "AERIAL_LIFT", "AREAL_LIFT",
    "CABLE_CAR", "FERRY", "AIRPLANE", "OTHER", "ODM", "RIDE_SHARING", "FLEX",
)

#: Membership view derived from the ordered vocabulary, never a second list.
TRANSIT_REQUEST_MODES = frozenset(TRANSIT_REQUEST_MODE_ORDER)

#: The rejected street modes in their published order, for the geocode filter's
#: ordered ``street_modes`` diagnostic.
REJECTED_STREET_MODE_ORDER = (
    "WALK", "BIKE", "RENTAL", "CAR", "HGV", "CAR_PARKING", "CAR_DROPOFF",
)

#: Street-mode membership is derived from its authoritative ordered vocabulary.
REJECTED_STREET_MODES = frozenset(REJECTED_STREET_MODE_ORDER)

#: Every ``DEBUG_*`` spelling is a street-mode alias, never a transit request.
DEBUG_MODE_PREFIX = "DEBUG_"


__all__ = [
    "TRANSIT_REQUEST_MODES",
    "TRANSIT_REQUEST_MODE_ORDER",
    "REJECTED_STREET_MODES",
    "REJECTED_STREET_MODE_ORDER",
    "DEBUG_MODE_PREFIX",
]
