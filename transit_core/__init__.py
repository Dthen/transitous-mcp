"""Journey semantics for the Transitous MCP tools — standard library only.

The package exists so each rule lives in exactly one place:

``errors``      outcome enum, named reasons, the one error constructor
``http``        the single GET boundary: array serialisation, timeout, taxonomy
``timezones``   zone authority, bounded discovery, local-validity classification
``timeparse``   the five input forms, frame selection, DST enforcement
``rendering``   the one canonical timestamp object and its display strings
``durations``   decomposition, conservation, day offset, overnight
``places``      place mapping, cancellation tri-state, dummy-walk provenance

Nothing here imports ``server`` or ``pytest``, and no production path branches on
a station name, station code, operator or country. Real place names appear only in
fixtures and their assertions.

Typical use inside a tool::

    from transit_core import errors, http, timeparse

    try:
        frame = timeparse.classify(args.get("time"), control="depart_after",
                                   endpoint_zone=zone, endpoint_zone_source=source)
        data = http.api_get("/api/v6/plan", {"time": frame.wire_value, ...})
    except errors.ToolError as exc:
        return exc.to_result()
"""

from . import durations, errors, http, places, rendering, timeparse, timezones
from .errors import ToolError, error_result, fail, success_result

__all__ = [
    "durations",
    "errors",
    "http",
    "places",
    "rendering",
    "timeparse",
    "timezones",
    "ToolError",
    "error_result",
    "success_result",
    "fail",
]

__version__ = "0.1.0"
