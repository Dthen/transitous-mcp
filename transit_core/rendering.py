"""The one canonical timestamp object (§2.5).

Every reported time in every tool is this object. There is no second ISO field, no
parallel display field and no bare clock string anywhere in a result.

    >>> from datetime import datetime, timezone
    >>> from transit_core import rendering
    >>> rendering.timestamp(datetime(2026, 10, 23, 16, 36, tzinfo=timezone.utc),
    ...                     zone="Europe/London")["iso"]
    '2026-10-23T17:36:00+01:00'

A time whose zone is genuinely unavailable is still rendered in its true offset,
with ``zone: null``, ``zone_source: "unavailable"`` and the explicit
``zone unavailable`` marker. A zone is never invented, and a UTC instant is never
passed off as a local rendering of a zone nobody supplied.

``zone_source`` is provenance, not a default: an explicit ``unavailable`` stays
``unavailable`` even when a zone is present, because a zone whose origin nobody
can name is not evidence that a response supplied it. Callers that know the origin
pass it; a caller mapping a response payload's own ``tz`` passes
``response_place``, which is also what the ``None`` default asserts for that path.
Any other value — an unknown string, a frame-vocabulary token such as
``endpoint_stop_info``, or a non-string — is ``unavailable``: provenance is never
upgraded to a claim the caller did not make.
"""

from datetime import datetime

from . import timezones

#: ``zone_source`` vocabulary.
RESPONSE_PLACE = "response_place"
CALLER_ENDPOINT = "caller_endpoint"
CALLER_TIMEZONE = "caller_timezone"
INFERRED_NEARBY_STOP = "inferred_nearby_stop"
UNAVAILABLE = "unavailable"

ZONE_SOURCES = (
    RESPONSE_PLACE,
    CALLER_ENDPOINT,
    CALLER_TIMEZONE,
    INFERRED_NEARBY_STOP,
    UNAVAILABLE,
)

_FRAME_TO_ZONE_SOURCE = {
    timezones.STOP_INFO: RESPONSE_PLACE,
    timezones.REVERSE_GEOCODE: INFERRED_NEARBY_STOP,
    timezones.CALLER_ENDPOINT: CALLER_ENDPOINT,
    timezones.CALLER_TIMEZONE: CALLER_TIMEZONE,
    timezones.RESPONSE: RESPONSE_PLACE,
    timezones.NOT_APPLICABLE: UNAVAILABLE,
    timezones.UNAVAILABLE: UNAVAILABLE,
}


def zone_source_for_frame(frame_source):
    """Map a ``time_frame.frame_source`` to this object's ``zone_source``."""
    return _FRAME_TO_ZONE_SOURCE.get(frame_source, UNAVAILABLE)


def format_offset(seconds):
    """``+01:00`` / ``-05:00`` / ``+05:30`` for an offset in seconds."""
    sign = "+" if seconds >= 0 else "-"
    total = abs(int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def _require_aware(instant):
    if not isinstance(instant, datetime):
        raise ValueError(f"not a datetime: {instant!r}")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("a timestamp requires an aware datetime; the host zone is never assumed")


def timestamp(instant, zone=None, zone_source=None):
    """Build the canonical timestamp object for ``instant`` as seen in ``zone``.

    ``zone`` is the IANA id of the display zone, or ``None`` when the payload
    supplied none. An unresolvable id is treated as no zone: the instant is still
    rendered in its own true offset and marked ``zone unavailable``.

    ``zone_source`` must name where that zone came from. Any member of
    :data:`ZONE_SOURCES` is preserved as-is. The ``None`` default is the
    response-mapping path's assertion that the payload's own ``tz`` supplied the
    zone; every other value is ``unavailable``. An out-of-vocabulary value is
    therefore never upgraded to ``response_place`` just because a zone resolved —
    a resolved zone is not evidence of where it came from. Membership is tested
    against a tuple, so an unhashable value cannot raise here.
    """
    _require_aware(instant)
    resolved = timezones.zone_or_none(zone) if zone is not None else None
    if resolved is None:
        local = instant
        zone_name = None
        source = UNAVAILABLE
    else:
        local = instant.astimezone(resolved)
        zone_name = resolved.key
        if zone_source is None:
            source = RESPONSE_PLACE
        elif zone_source in ZONE_SOURCES:
            source = zone_source
        else:
            source = UNAVAILABLE
    if zone_name is None:
        source = UNAVAILABLE

    local = local.replace(microsecond=0)
    offset = int(local.utcoffset().total_seconds())
    display_zone = zone_name if zone_name is not None else "zone unavailable"
    return {
        "iso": local.isoformat(timespec="seconds"),
        "epoch_seconds": int(local.timestamp()),
        "local": local.strftime("%Y-%m-%dT%H:%M:%S"),
        "local_date": local.strftime("%Y-%m-%d"),
        "utc_offset_seconds": offset,
        "zone": zone_name,
        "zone_source": source,
        "display": f"{local.strftime('%Y-%m-%d %H:%M')} {format_offset(offset)} ({display_zone})",
    }


__all__ = [
    "RESPONSE_PLACE", "CALLER_ENDPOINT", "CALLER_TIMEZONE", "INFERRED_NEARBY_STOP",
    "UNAVAILABLE", "ZONE_SOURCES", "zone_source_for_frame", "format_offset", "timestamp",
]
