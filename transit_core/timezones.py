"""Zone authority, bounded discovery and local-validity classification (§2.2, §4.3).

This is the only module that touches ``zoneinfo``. Everything here answers one of
three questions:

*Is this local wall clock valid in this zone?*
    :func:`classify_local` returns ``valid``, ``ambiguous`` (the repeated hour, with
    both candidate offsets and instants) or ``nonexistent`` (the spring gap, with
    the gap's local start and end). The verdict comes from a roundtrip through UTC,
    never from the host zone.

*Which zone owns this endpoint?*
    :func:`resolve_endpoint_zone` applies the authority order — caller declaration,
    then stop metadata, then a bounded coordinate inference — and costs at most one
    request. A coordinate is only accepted when the nearest stop is at most 500 m
    away, inclusive, and the accepted stop id and distance are recorded.

*Do two like authorities disagree?*
    :func:`check_zone_agreement` compares a pre-request zone with the response's
    zone and raises ``unsupported`` / ``timezone_disagreement`` only when both come
    from like authorities. A caller declaration and a caller ``timezone`` are never
    flagged: they are deliberate authority, not a claim about the endpoint.

No function here reads the host time zone.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import NoReturn, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import errors, http

UTC = timezone.utc

#: A coordinate's inferred zone is accepted only within this many metres, inclusive.
MAX_INFERENCE_DISTANCE_M = 500.0

#: Mean Earth radius used by the great-circle distance (metres).
EARTH_RADIUS_M = 6371008.8

# ── frame-source vocabulary (§2.1 frame_source) ─────────────────────

CALLER_TIMEZONE = "caller_timezone"
CALLER_ENDPOINT = "caller_endpoint_timezone"
STOP_INFO = "endpoint_stop_info"
REVERSE_GEOCODE = "endpoint_reverse_geocode"
RESPONSE = "endpoint_response"
NOT_APPLICABLE = "not_applicable"
UNAVAILABLE = "unavailable"

#: Sources that describe the endpoint's own zone, and so may be compared with a
#: response zone. Caller declarations and caller timezones are excluded.
COMPARABLE_SOURCES = (STOP_INFO, REVERSE_GEOCODE)

REMEDY_UNRESOLVED = (
    "supply an explicit offset instant (for example 2026-10-27T09:00:00Z), "
    "a caller timezone, or this endpoint's zone declaration"
)
REMEDY_AMBIGUOUS = (
    "supply an explicit offset that selects the intended occurrence, or a timezone "
    "whose clock is unambiguous at that reading"
)
REMEDY_NONEXISTENT = (
    "supply a valid local clock, or an explicit instant that carries its own offset"
)
REMEDY_DISAGREEMENT = (
    "declare the endpoint zone explicitly; an endpoint-zone declaration outranks both "
    "the stop metadata and the plan response"
)


# ── zone resolution and validation ─────────────────────────────────


def zone_or_none(name):
    """Resolve an IANA zone id, or return ``None`` when it is not usable."""
    if not isinstance(name, str):
        return None
    candidate = name.strip()
    if not candidate:
        return None
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def resolve_zone_name(name):
    """Validate a caller-supplied zone id and return it canonicalised.

    Raises ``invalid_input`` / ``unknown_timezone`` for anything ``zoneinfo`` cannot
    resolve. A caller timezone is a deliberate frame and is never compared with an
    endpoint zone.
    """
    resolved = zone_or_none(name)
    if resolved is None:
        errors.fail(
            errors.UNKNOWN_TIMEZONE,
            f"unknown timezone: {name!r}",
            {"zone": name, "remedy": "supply an IANA timezone id such as Europe/London"},
        )
    return resolved.key


def _zone(zone):
    if isinstance(zone, ZoneInfo):
        return zone
    resolved = zone_or_none(zone)
    if resolved is None:
        errors.fail(errors.UNKNOWN_TIMEZONE, f"unknown timezone: {zone!r}", {"zone": zone})
    return resolved


def zone_name(zone):
    return zone.key if isinstance(zone, ZoneInfo) else str(zone)


def offset_seconds(moment):
    """UTC offset of ``moment``'s own zone, in seconds."""
    offset = moment.utcoffset()
    if offset is None:
        raise ValueError("offset_seconds requires an aware datetime")
    return int(offset.total_seconds())


# ── instant parsing ────────────────────────────────────────────────

_ISO_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[Tt ]"
    r"(?P<hour>\d{2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2})(?:\.(?P<fraction>\d{1,9}))?)?"
    r"(?P<offset>Z|z|[+-]\d{2}:\d{2}|[+-]\d{4})?$"
)


def parse_rfc3339(text, default_zone=None):
    """Parse a payload timestamp into an aware datetime.

    Upstream payloads always carry ``Z``; ``default_zone`` exists only for a
    strictly-local input that genuinely has no offset. A naive string without a
    default is a programming error, not a wall clock to guess at.
    """
    if not isinstance(text, str):
        raise ValueError(f"not a timestamp: {text!r}")
    match = _ISO_RE.match(text.strip())
    if match is None:
        raise ValueError(f"not an RFC 3339 timestamp: {text!r}")
    fraction = match.group("fraction") or ""
    micro = int((fraction + "000000")[:6]) if fraction else 0
    base = datetime(
        int(match.group("date")[0:4]),
        int(match.group("date")[5:7]),
        int(match.group("date")[8:10]),
        int(match.group("hour")),
        int(match.group("minute")),
        int(match.group("second") or 0),
        micro,
    )
    offset = match.group("offset")
    if offset in ("Z", "z"):
        return base.replace(tzinfo=UTC)
    if offset:
        sign = 1 if offset[0] == "+" else -1
        digits = offset[1:].replace(":", "")
        hours, minutes = int(digits[0:2]), int(digits[2:4])
        if hours > 23 or minutes > 59:
            raise ValueError(f"offset out of range: {offset!r}")
        return base.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))
    if default_zone is None:
        raise ValueError(f"timestamp has no offset: {text!r}")
    return base.replace(tzinfo=_zone(default_zone))


def iso_z(moment):
    """RFC 3339 in UTC with a ``Z`` suffix — the unambiguous rendering of an instant."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── local validity ─────────────────────────────────────────────────


@dataclass(frozen=True)
class LocalValidity:
    """The verdict for one naive wall clock in one zone."""

    status: str                       # "valid" | "ambiguous" | "nonexistent"
    zone: str
    naive: datetime
    candidates: tuple = ()            # ((utc_offset_seconds, aware instant), ...)
    gap_start: Optional[datetime] = None
    gap_end: Optional[datetime] = None

    @property
    def is_valid(self):
        return self.status == "valid"

    def error_details(self):
        if self.status == "valid":
            return {}
        if self.status == "ambiguous":
            return {
                "zone": self.zone,
                "local": self.naive.isoformat(),
                "candidates": [
                    {"utc_offset_seconds": offset, "instant": iso_z(instant)}
                    for offset, instant in self.candidates
                ],
                "remedy": REMEDY_AMBIGUOUS,
            }
        return {
            "zone": self.zone,
            "local": self.naive.isoformat(),
            "gap": {
                "start": self.gap_start.isoformat(),
                "end": self.gap_end.isoformat(),
            },
            "remedy": REMEDY_NONEXISTENT,
        }


def _roundtrip(naive, zi, fold):
    aware = naive.replace(tzinfo=zi, fold=fold)
    return aware.astimezone(UTC).astimezone(zi).replace(tzinfo=None)


def _offset_at(instant, zi):
    return int(instant.astimezone(zi).utcoffset().total_seconds())


def _transition_instant(earlier, later, zi, after_offset):
    """Smallest whole-second instant in ``(earlier, later]`` whose offset is ``after_offset``."""
    lo = int(math.floor(earlier.timestamp()))
    hi = int(math.ceil(later.timestamp()))
    while lo < hi:
        mid = (lo + hi) // 2
        if _offset_at(datetime.fromtimestamp(mid, tz=UTC), zi) == after_offset:
            hi = mid
        else:
            lo = mid + 1
    return datetime.fromtimestamp(lo, tz=UTC)


def classify_local(naive, zone):
    """Classify a naive wall clock in ``zone`` without consulting the host zone."""
    if naive.tzinfo is not None:
        raise ValueError("classify_local requires a naive datetime")
    zi = _zone(zone)
    name = zi.key
    round0 = _roundtrip(naive, zi, 0)
    round1 = _roundtrip(naive, zi, 1)
    aware0 = naive.replace(tzinfo=zi, fold=0)
    aware1 = naive.replace(tzinfo=zi, fold=1)
    offset0 = offset_seconds(aware0)
    offset1 = offset_seconds(aware1)

    if round0 == naive and round1 == naive and offset0 != offset1:
        return LocalValidity(
            status="ambiguous",
            zone=name,
            naive=naive,
            candidates=(
                (offset0, aware0.astimezone(UTC)),
                (offset1, aware1.astimezone(UTC)),
            ),
        )
    if round0 != naive or round1 != naive:
        first = min(aware0.astimezone(UTC), aware1.astimezone(UTC))
        second = max(aware0.astimezone(UTC), aware1.astimezone(UTC))
        before = _offset_at(first, zi)
        after = _offset_at(second, zi)
        transition = _transition_instant(first, second, zi, after)
        return LocalValidity(
            status="nonexistent",
            zone=name,
            naive=naive,
            gap_start=(transition + timedelta(seconds=before)).replace(tzinfo=None),
            gap_end=(transition + timedelta(seconds=after)).replace(tzinfo=None),
        )
    return LocalValidity(
        status="valid",
        zone=name,
        naive=naive,
        candidates=((offset0, aware0.astimezone(UTC)),),
    )


def to_instant(naive, zone):
    """The single instant a valid naive clock denotes. Never picks a fold silently."""
    validity = classify_local(naive, zone)
    if validity.status != "valid":
        errors.fail(
            errors.AMBIGUOUS_LOCAL_TIME if validity.status == "ambiguous" else errors.NONEXISTENT_LOCAL_TIME,
            f"{validity.status} local time {naive.isoformat()} in {validity.zone}",
            validity.error_details(),
        )
    return validity.candidates[0][1]


# ── great-circle distance ──────────────────────────────────────────


def distance_meters(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres (haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


# ── endpoint zone discovery ────────────────────────────────────────


@dataclass(frozen=True)
class EndpointQuery:
    """One endpoint whose zone may need resolving.

    ``declared_zone`` is the caller's endpoint-zone declaration; when present it is
    the authority and no request is issued.
    """

    label: str
    stop_id: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    declared_zone: Optional[str] = None

    @classmethod
    def stop(cls, label, stop_id, declared_zone=None):
        return cls(label=label, stop_id=stop_id, declared_zone=declared_zone)

    @classmethod
    def coordinate(cls, label, lat, lon, declared_zone=None):
        return cls(label=label, lat=lat, lon=lon, declared_zone=declared_zone)

    @property
    def form(self):
        return "stop" if self.stop_id else "coordinate"


@dataclass(frozen=True)
class ZoneResolution:
    zone: Optional[str] = None
    source: str = UNAVAILABLE
    requests_issued: int = 0
    details: dict = field(default_factory=dict)

    @property
    def resolved(self):
        return self.zone is not None


def _unresolved(query, requests_issued, extra=None) -> NoReturn:
    details = {
        "endpoint": query.label,
        "form": query.form,
        "requests_issued": requests_issued,
        "remedy": REMEDY_UNRESOLVED,
    }
    if extra:
        details.update(extra)
    errors.fail(
        errors.TIMEZONE_UNRESOLVED,
        f"could not resolve the timezone of the {query.label} endpoint ({query.form})",
        details,
    )


def _resolved_payload_zone(query, raw_zone, requests_issued, extra=None):
    """Resolve a zone the upstream supplied, without blaming the caller for it.

    §4.3 reserves ``unknown_timezone`` for caller-declared ids (steps 1 and 2).
    A metadata or inference value that is present but not an IANA id means the
    discovery source did not yield a usable zone, which is step 5's
    ``timezone_unresolved`` with the endpoint and its remedy.
    """
    resolved = zone_or_none(raw_zone)
    if resolved is None:
        details = dict(extra or {})
        if isinstance(raw_zone, str) and raw_zone.strip():
            # bounded and credential-redacted: this value is upstream body content
            details["payload_zone"] = http.excerpt(raw_zone)
        _unresolved(query, requests_issued, details)
    return resolved.key


def resolve_endpoint_zone(query, *, transport=None):
    """Zone of one endpoint, by the authority order of §4.3.

    Steps, stopping at the first success: caller declaration (no request), stop
    metadata (one request), bounded coordinate inference (one request), else
    ``invalid_input`` / ``timezone_unresolved``. At most one request is issued.
    """
    if query.declared_zone is not None:
        return ZoneResolution(
            zone=resolve_zone_name(query.declared_zone),
            source=CALLER_ENDPOINT,
            requests_issued=0,
            details={"endpoint": query.label},
        )

    if query.stop_id:
        data = http.api_get("/api/v6/stop", {"stopId": query.stop_id}, transport=transport)
        if not isinstance(data, dict) or not isinstance(data.get("place"), dict):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "stop metadata response did not carry a place object",
                {"endpoint": query.label, "stop_id": query.stop_id},
            )
        zone = data["place"].get("tz")
        if not isinstance(zone, str) or not zone.strip():
            _unresolved(query, 1, {"stop_id": query.stop_id})
        return ZoneResolution(
            zone=_resolved_payload_zone(query, zone, 1, {"stop_id": query.stop_id}),
            source=STOP_INFO,
            requests_issued=1,
            details={"endpoint": query.label, "stop_id": query.stop_id},
        )

    if query.lat is not None and query.lon is not None:
        data = http.api_get(
            "/api/v1/reverse-geocode",
            {"place": f"{query.lat},{query.lon}", "type": ["STOP"], "numResults": 1},
            transport=transport,
        )
        if not isinstance(data, list):
            errors.fail(
                errors.UNEXPECTED_SHAPE,
                "reverse-geocode response was not an array",
                {"endpoint": query.label, "coordinate": [query.lat, query.lon]},
            )
        if not data or not isinstance(data[0], dict):
            _unresolved(query, 1, {"coordinate": [query.lat, query.lon]})
        first = data[0]
        zone = first.get("tz")
        if not isinstance(zone, str) or not zone.strip():
            _unresolved(query, 1, {"coordinate": [query.lat, query.lon]})
        stop_lat, stop_lon = first.get("lat"), first.get("lon")
        if not isinstance(stop_lat, (int, float)) or not isinstance(stop_lon, (int, float)):
            _unresolved(query, 1, {"coordinate": [query.lat, query.lon]})
        distance = distance_meters(query.lat, query.lon, float(stop_lat), float(stop_lon))
        if distance > MAX_INFERENCE_DISTANCE_M:
            _unresolved(
                query,
                1,
                {
                    "coordinate": [query.lat, query.lon],
                    "distance_m": round(distance, 1),
                    "max_distance_m": MAX_INFERENCE_DISTANCE_M,
                },
            )
        return ZoneResolution(
            zone=_resolved_payload_zone(
                query, zone, 1, {"stop_id": first.get("id"), "distance_m": round(distance, 1)}
            ),
            source=REVERSE_GEOCODE,
            requests_issued=1,
            details={
                "endpoint": query.label,
                "stop_id": first.get("id"),
                "distance_m": round(distance, 1),
            },
        )

    _unresolved(query, 0)


def check_zone_agreement(resolution, response_zone, *, endpoint_label=None):
    """Raise ``unsupported`` / ``timezone_disagreement`` for like authorities only.

    Only a pre-request zone established from stop metadata or coordinate inference
    is compared, and only against a response-supplied zone. A caller declaration, a
    caller ``timezone``, an unresolved zone or a response without a zone is silent.
    """
    if not resolution or not resolution.zone or not response_zone:
        return None
    if resolution.source not in COMPARABLE_SOURCES:
        return None
    if resolution.zone == response_zone:
        return None
    errors.fail(
        errors.TIMEZONE_DISAGREEMENT,
        f"the {resolution.source} zone {resolution.zone} disagrees with the response zone "
        f"{response_zone}",
        {
            "endpoint": endpoint_label or resolution.details.get("endpoint"),
            "request_zone": resolution.zone,
            "request_source": resolution.source,
            "response_zone": response_zone,
            "response_source": "response_place",
            "remedy": REMEDY_DISAGREEMENT,
        },
    )


__all__ = [
    "MAX_INFERENCE_DISTANCE_M", "EARTH_RADIUS_M",
    "CALLER_TIMEZONE", "CALLER_ENDPOINT", "STOP_INFO", "REVERSE_GEOCODE", "RESPONSE",
    "NOT_APPLICABLE", "UNAVAILABLE", "COMPARABLE_SOURCES",
    "REMEDY_UNRESOLVED", "REMEDY_AMBIGUOUS", "REMEDY_NONEXISTENT", "REMEDY_DISAGREEMENT",
    "zone_or_none", "resolve_zone_name", "zone_name", "offset_seconds",
    "parse_rfc3339", "iso_z",
    "LocalValidity", "classify_local", "to_instant",
    "distance_meters", "EndpointQuery", "ZoneResolution",
    "resolve_endpoint_zone", "check_zone_agreement",
]
