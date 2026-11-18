"""Journey semantics for the Transitous MCP tools — standard library only.

The package exists so each rule lives in exactly one place:

``errors``      outcome enum, named reasons, the one error constructor
``http``        the single GET boundary: array serialisation, timeout, taxonomy
``modes``       the one request-mode vocabulary shared by plan/geocode/one-to-many
``backend``     the protocol-independent 13-operation entry point (MCP and CLI)
``timezones``   zone authority, bounded discovery, local-validity classification
``timeparse``   the five input forms, frame selection, DST enforcement
``rendering``   the one canonical timestamp object and its display strings
``durations``   decomposition, conservation, day offset, overnight
``places``      place mapping, cancellation tri-state, dummy-walk provenance

Nothing here imports ``server`` or ``pytest``, importing any module has no stream,
network or loop side effect, and no production path branches on a station name,
station code, operator or country. Real place names appear only in fixtures and
their assertions.

Typical use inside a tool::

    from transit_core import errors, http, timeparse

    try:
        frame = timeparse.classify(args.get("time"), control="depart_after",
                                   endpoint_zone=zone, endpoint_zone_source=source)
        data = http.api_get("/api/v6/plan", {"time": frame.wire_value, ...})
    except errors.ToolError as exc:
        return exc.to_result()
"""

from . import durations, errors, http, modes, places, rendering, timeparse, timezones
from . import backend
from .errors import ToolError, error_result, fail, success_result

#: The single public application version. ``server.py`` uses it for
#: ``serverInfo``; ``__version__`` below is an alias, never a second source. This
#: unreleased tree previously carried an internal ``0.1.0`` core version; it is
#: reconciled here to the already-published application contract, which is not
#: bumped by this preparation.
APPLICATION_VERSION = "3.0.0"

__all__ = [
    "APPLICATION_VERSION",
    "backend",
    "durations",
    "errors",
    "http",
    "modes",
    "places",
    "rendering",
    "timeparse",
    "timezones",
    "ToolError",
    "error_result",
    "success_result",
    "fail",
]

#: Alias of :data:`APPLICATION_VERSION`; the core no longer carries its own
#: version number.
__version__ = APPLICATION_VERSION
