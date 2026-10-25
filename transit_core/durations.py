"""Duration decomposition, conservation, day offset and overnight (§3.1, §3.2).

Seven quantities, all relative to the first and last **transit** legs rather than
to leg positions:

``elapsed_seconds``            end instant minus start instant (door to door)
``access_seconds``             first transit departure minus itinerary start
``egress_seconds``             itinerary end minus last transit arrival
``transfer_walk_seconds``      sum of non-transit legs strictly between those two
``in_transit_seconds``         sum of transit-leg durations
``station_to_station_seconds`` elapsed − access − egress (transfer walking stays in)
``waiting_seconds``            elapsed − access − egress − transfer walk − in transit

A component whose inputs are absent is ``null``, never ``0``, and forces
``waiting_seconds`` to ``null`` with ``duration_components_incomplete``. A negative
remainder is reported as ``null`` with ``duration_decomposition_inconsistent``; a
negative value is never emitted and no component is silently adjusted.

Transfer walking is summed leg by leg. It is never a residual of the total — a
missing second internal walk must not reconcile arithmetically while
misdescribing the journey.

Calendar facts are separate from elapsed time: ``arrival_date_offset_days`` is a
local-date difference, ``has_overnight`` is computed from the departure instant,
the origin zone and the elapsed seconds, and a negative offset (westbound date-line
crossing) is lawful, warned, and never clamped or converted into an overnight.

Every function here takes already-fetched legs; nothing reads a clock or a network.
Legs must already be ordered and shape-validated by the caller's normalizer (§3.1);
this module does not re-check order.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from . import timezones

#: Street modes from the pinned OpenAPI ``Mode`` enumeration.
STREET_MODES = frozenset(
    {"WALK", "BIKE", "RENTAL", "CAR", "CAR_PARKING", "CAR_DROPOFF", "ODM", "RIDE_SHARING", "FLEX"}
)

#: Transit **response** modes: the "=== Transit ===" block of the pinned OpenAPI
#: ``Mode`` enum (``openapi-v2.10.2.yaml`` lines 3440-3460). This is payload
#: classification, not the tool's caller-facing ``transit_modes`` request enum
#: (``server.py`` owns that). The four deprecated aliases the provider may still
#: emit — ``REGIONAL_FAST_RAIL``, ``AREAL_LIFT``, ``METRO``, ``CABLE_CAR`` — are
#: included with ``OTHER``, which is current.
#:
#: ``ODM`` and ``RIDE_SHARING`` are marked ``# Transit | Street`` in that enum, a
#: shared upstream classification. They stay in :data:`STREET_MODES` so a leg's
#: street decision (dummy-walk exclusion, no transit components) never depends on
#: a mode whose class is ambiguous. No alias is invented for either.
TRANSIT_MODES = frozenset(
    {
        "TRANSIT", "TRAM", "SUBWAY", "FERRY", "AIRPLANE", "BUS", "COACH", "RAIL",
        "HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "REGIONAL_FAST_RAIL",
        "REGIONAL_RAIL", "SUBURBAN", "FUNICULAR", "AERIAL_LIFT", "OTHER",
        "AREAL_LIFT", "METRO", "CABLE_CAR",
    }
)

DURATION_COMPONENTS_INCOMPLETE = "duration_components_incomplete"
DURATION_DECOMPOSITION_INCONSISTENT = "duration_decomposition_inconsistent"
DATE_LINE_CROSSING = "date_line_crossing"
CALENDAR_INPUTS_INCOMPLETE = "calendar_inputs_incomplete"
ENDPOINT_ZONE_UNAVAILABLE = "endpoint_zone_unavailable"
NO_TRANSIT_LEG = "no_transit_leg"

BASIS_TRANSIT = "transit_itinerary"
BASIS_STREET_ONLY = "street_only"
BASIS_INCOMPLETE = "incomplete"

QUANTITY_FIELDS = (
    "elapsed_seconds",
    "access_seconds",
    "egress_seconds",
    "transfer_walk_seconds",
    "in_transit_seconds",
    "station_to_station_seconds",
    "waiting_seconds",
)


def is_transit_mode(mode):
    """True for a service leg. Mode is payload metadata, never a name heuristic."""
    return mode in TRANSIT_MODES


def _parse(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return timezones.parse_rfc3339(value)
    except ValueError:
        return None


def _delta(start, end):
    if start is None or end is None:
        return None
    return int((end - start).total_seconds())


@dataclass(frozen=True)
class LegTiming:
    """The only leg view this module needs: mode, instants, duration."""

    mode: str = ""
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    duration: Optional[int] = None

    @property
    def is_transit(self):
        return is_transit_mode(self.mode)

    @property
    def seconds(self):
        """The leg's duration: its declared duration, else its instants."""
        if self.duration is not None:
            return self.duration
        return _delta(self.start, self.end)

    @classmethod
    def from_leg(cls, raw):
        if isinstance(raw, LegTiming):
            return raw
        if not isinstance(raw, dict):
            raise ValueError(f"not a leg: {raw!r}")
        mode = raw.get("mode")
        if not isinstance(mode, str):
            mode = ""
        start = _parse(raw.get("startTime"))
        if start is None:
            start = _parse((raw.get("from") or {}).get("departure"))
        end = _parse(raw.get("endTime"))
        if end is None:
            end = _parse((raw.get("to") or {}).get("arrival"))
        duration = raw.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            duration = None
        elif isinstance(duration, float) and not math.isfinite(duration):
            # JSON's NaN/Infinity parse as floats and are not a quantity (§3.1
            # rule 1). Only floats are tested, so a giant Python int never meets a
            # float conversion and cannot raise OverflowError.
            duration = None
        else:
            duration = int(duration)
        return cls(mode=mode, start=start, end=end, duration=duration)


def legs_from(raw_legs):
    """Normalise raw legs (mappings or :class:`LegTiming`) into a tuple."""
    return tuple(LegTiming.from_leg(leg) for leg in (raw_legs or ()))


@dataclass(frozen=True)
class Breakdown:
    """The seven quantities plus the case that applied."""

    elapsed_seconds: Optional[int] = None
    access_seconds: Optional[int] = None
    egress_seconds: Optional[int] = None
    transfer_walk_seconds: Optional[int] = None
    in_transit_seconds: Optional[int] = None
    station_to_station_seconds: Optional[int] = None
    waiting_seconds: Optional[int] = None
    duration_basis: str = BASIS_INCOMPLETE
    warnings: tuple = ()
    reason: Optional[str] = None

    @property
    def complete(self):
        return all(getattr(self, name) is not None for name in QUANTITY_FIELDS)

    @property
    def conserves(self):
        """All components known and summing exactly to ``elapsed_seconds``."""
        if not self.complete:
            return False
        total = (
            self.access_seconds
            + self.egress_seconds
            + self.transfer_walk_seconds
            + self.in_transit_seconds
            + self.waiting_seconds
        )
        return total == self.elapsed_seconds

    def to_object(self):
        payload = {name: getattr(self, name) for name in QUANTITY_FIELDS}
        payload["duration_basis"] = self.duration_basis
        payload["warnings"] = list(self.warnings)
        return payload


def decompose(raw_legs, *, start=None, end=None):
    """Decompose one itinerary's legs.

    ``start`` / ``end`` are the itinerary's own instants; when omitted they are the
    first leg's departure and the last leg's arrival. Pass them explicitly whenever
    the itinerary carries ``startTime`` / ``endTime``, which is the canonical case.

    ``raw_legs`` is a **precondition**: legs must already be ordered in time and
    shape-validated by the caller's normalizer (§3.1 "already validated as
    ordered"). This function does not re-check order, does not sort, and does not
    invent a duration for an unordered or malformed leg.
    """
    legs = legs_from(raw_legs)
    if start is None and legs:
        start = legs[0].start
    if end is None and legs:
        end = legs[-1].end

    transit_indexes = [index for index, leg in enumerate(legs) if leg.is_transit]
    if not transit_indexes:
        return Breakdown(
            elapsed_seconds=_delta(start, end),
            in_transit_seconds=0,
            duration_basis=BASIS_STREET_ONLY,
            reason=NO_TRANSIT_LEG,
        )

    first, last = transit_indexes[0], transit_indexes[-1]
    elapsed = _delta(start, end)
    access = _delta(start, legs[first].start)
    egress = _delta(legs[last].end, end)

    internal_walks = [
        legs[index] for index in range(first + 1, last) if not legs[index].is_transit
    ]
    internal_seconds = [leg.seconds for leg in internal_walks]
    transfer_walk = None if any(value is None for value in internal_seconds) else sum(internal_seconds)

    transit_seconds = [legs[index].seconds for index in transit_indexes]
    in_transit = None if any(value is None for value in transit_seconds) else sum(transit_seconds)

    station_to_station = (
        None if None in (elapsed, access, egress) else elapsed - access - egress
    )

    warnings = []
    if None in (elapsed, access, egress, transfer_walk, in_transit):
        waiting = None
        basis = BASIS_INCOMPLETE
        warnings.append(DURATION_COMPONENTS_INCOMPLETE)
    else:
        basis = BASIS_TRANSIT
        remainder = elapsed - access - egress - transfer_walk - in_transit
        if remainder < 0:
            waiting = None
            warnings.append(DURATION_DECOMPOSITION_INCONSISTENT)
        else:
            waiting = remainder

    return Breakdown(
        elapsed_seconds=elapsed,
        access_seconds=access,
        egress_seconds=egress,
        transfer_walk_seconds=transfer_walk,
        in_transit_seconds=in_transit,
        station_to_station_seconds=station_to_station,
        waiting_seconds=waiting,
        duration_basis=basis,
        warnings=tuple(warnings),
    )


def decompose_itinerary(itinerary):
    """Decompose a pinned ``Itinerary`` object (``legs`` + ``startTime``/``endTime``)."""
    if not isinstance(itinerary, dict):
        raise ValueError(f"not an itinerary: {itinerary!r}")
    legs = itinerary.get("legs")
    if not isinstance(legs, list):
        legs = []
    return decompose(
        legs,
        start=_parse(itinerary.get("startTime")),
        end=_parse(itinerary.get("endTime")),
    )


# ── calendar facts ─────────────────────────────────────────────────


@dataclass(frozen=True)
class CalendarFacts:
    arrival_date_offset_days: Optional[int] = None
    has_overnight: Optional[bool] = None
    warnings: tuple = ()

    def to_object(self):
        return {
            "arrival_date_offset_days": self.arrival_date_offset_days,
            "has_overnight": self.has_overnight,
            "warnings": list(self.warnings),
        }


def calendar_facts(start, end, origin_zone, destination_zone):
    """Local-date offset and overnight flag, from instants and named zones only.

    Each quantity depends only on the inputs it actually needs (§3.2):
    ``has_overnight`` needs the departure instant and the origin zone, while
    ``arrival_date_offset_days`` needs both zones. An absent or unresolvable
    display zone is a payload condition (§4.3), so only the affected quantity is
    ``null``, with ``endpoint_zone_unavailable`` — never a caller-facing
    ``unknown_timezone`` error, because a caller-declared id is validated by the
    input resolver before it reaches this mapper.

    A naive instant is not an instant: it is never read in the host zone, and both
    quantities are ``null`` with ``calendar_inputs_incomplete``.
    """
    origin = timezones.zone_or_none(origin_zone)
    destination = timezones.zone_or_none(destination_zone)

    warnings = []
    offset_days = None
    has_overnight = None
    if (
        start is None
        or end is None
        or start.utcoffset() is None
        or end.utcoffset() is None
    ):
        warnings.append(CALENDAR_INPUTS_INCOMPLETE)
    else:
        if origin is not None and destination is not None:
            offset_days = (
                end.astimezone(destination).date() - start.astimezone(origin).date()
            ).days
            if offset_days < 0:
                warnings.append(DATE_LINE_CROSSING)
        if origin is not None:
            origin_start = start.astimezone(origin)
            elapsed = int((end - start).total_seconds())
            origin_arrival = (start + timedelta(seconds=elapsed)).astimezone(origin)
            has_overnight = origin_arrival.date() > origin_start.date()
    if origin is None or destination is None:
        warnings.append(ENDPOINT_ZONE_UNAVAILABLE)

    return CalendarFacts(
        arrival_date_offset_days=offset_days,
        has_overnight=has_overnight,
        warnings=tuple(warnings),
    )


__all__ = [
    "STREET_MODES", "TRANSIT_MODES", "is_transit_mode",
    "DURATION_COMPONENTS_INCOMPLETE", "DURATION_DECOMPOSITION_INCONSISTENT",
    "DATE_LINE_CROSSING", "CALENDAR_INPUTS_INCOMPLETE", "ENDPOINT_ZONE_UNAVAILABLE",
    "NO_TRANSIT_LEG",
    "BASIS_TRANSIT", "BASIS_STREET_ONLY", "BASIS_INCOMPLETE", "QUANTITY_FIELDS",
    "LegTiming", "legs_from", "Breakdown", "decompose", "decompose_itinerary",
    "CalendarFacts", "calendar_facts",
]
