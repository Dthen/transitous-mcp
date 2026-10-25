"""The one outcome taxonomy and the one error constructor (§2.4).

Every touched tool returns exactly one outcome from a fixed set. Error results have
a constant four-field shape, produced here and nowhere else, so the protocol's
``isError`` derivation (which keys on a non-empty ``error``) cannot drift between
tools. Reasons are named; the reason-to-outcome map is exhaustive for every reason
this implementation can raise.

Usage::

    from transit_core import errors

    if bad:
        errors.fail(errors.MALFORMED_TIME, "time '2026-02-30' is not a calendar date",
                    {"as_given": "2026-02-30"})

    try:
        ...
    except errors.ToolError as exc:
        return exc.to_result()
"""

from typing import NoReturn


OK = "ok"
NO_ROUTES = "no_routes"

INVALID_INPUT = "invalid_input"
UNSUPPORTED = "unsupported"
OUT_OF_RANGE = "out_of_range"
UPSTREAM_REJECTED = "upstream_rejected"
TRANSPORT_ERROR = "transport_error"
MALFORMED_UPSTREAM = "malformed_upstream"
CONSTRAINT_VIOLATION = "constraint_violation"

#: Every outcome a tool may return, in the specification's order.
OUTCOMES = (
    OK,
    NO_ROUTES,
    INVALID_INPUT,
    UNSUPPORTED,
    OUT_OF_RANGE,
    UPSTREAM_REJECTED,
    TRANSPORT_ERROR,
    MALFORMED_UPSTREAM,
    CONSTRAINT_VIOLATION,
)

#: Outcomes an error result may carry. ``ok`` and ``no_routes`` are successes.
ERROR_OUTCOMES = OUTCOMES[2:]

# ── named reasons ───────────────────────────────────────────────────

# time and zone authority
MALFORMED_TIME = "malformed_time"
AMBIGUOUS_LOCAL_TIME = "ambiguous_local_time"
NONEXISTENT_LOCAL_TIME = "nonexistent_local_time"
TIMEZONE_UNRESOLVED = "timezone_unresolved"
UNKNOWN_TIMEZONE = "unknown_timezone"
BBOX_WINDOW_REQUIRES_EXPLICIT_ZONE = "bbox_window_requires_explicit_zone"

# argument shape and value
MIXED_ENDPOINT_FORM = "mixed_endpoint_form"
UNKNOWN_MODE = "unknown_mode"
UNKNOWN_PREFERENCE = "unknown_preference"
STREET_MODE_NOT_TRANSIT = "street_mode_not_transit"
VALUE_OUT_OF_RANGE = "value_out_of_range"
MUTUALLY_EXCLUSIVE_ARGUMENTS = "mutually_exclusive_arguments"
CURSOR_WITHOUT_IDENTICAL_QUERY = "cursor_without_identical_query"
PREFERENCE_REQUIRES_TRANSIT_MODE = "preference_requires_transit_mode"
PREFERENCE_UNSUPPORTED_FOR_BEST = "preference_unsupported_for_best"

# requests that cannot be honoured or verified as asked
TIMEZONE_DISAGREEMENT = "timezone_disagreement"
AMBIGUOUS_LOCAL_ENCODING = "ambiguous_local_encoding"
ACCESSIBILITY_UNVERIFIABLE = "accessibility_unverifiable"
TEMPLATE_UNVERIFIED = "template_unverified"

# upstream said no
TIMETABLE_WINDOW = "timetable_window"
BAD_REQUEST = "bad_request"
AUTH_OR_BLOCKED = "auth_or_blocked"
RATE_LIMITED = "rate_limited"
SERVER_ERROR = "server_error"
NOT_FOUND = "not_found"
REQUEST_NOT_HONOURED = "request_not_honoured"

# transport
TIMEOUT = "timeout"
CONNECTION_FAILURE = "connection_failure"

# payload
NON_JSON_BODY = "non_json_body"
UNEXPECTED_SHAPE = "unexpected_shape"
TRUNCATED_CONTAINER = "truncated_container"

# hard constraints
DEPARTURE_BEFORE_REQUEST = "departure_before_request"
ARRIVAL_AFTER_DEADLINE = "arrival_after_deadline"

#: The exhaustive reason-to-outcome map. A reason absent here cannot be raised.
REASON_OUTCOME = {
    MALFORMED_TIME: INVALID_INPUT,
    AMBIGUOUS_LOCAL_TIME: INVALID_INPUT,
    NONEXISTENT_LOCAL_TIME: INVALID_INPUT,
    TIMEZONE_UNRESOLVED: INVALID_INPUT,
    UNKNOWN_TIMEZONE: INVALID_INPUT,
    BBOX_WINDOW_REQUIRES_EXPLICIT_ZONE: INVALID_INPUT,
    MIXED_ENDPOINT_FORM: INVALID_INPUT,
    UNKNOWN_MODE: INVALID_INPUT,
    UNKNOWN_PREFERENCE: INVALID_INPUT,
    STREET_MODE_NOT_TRANSIT: INVALID_INPUT,
    VALUE_OUT_OF_RANGE: INVALID_INPUT,
    MUTUALLY_EXCLUSIVE_ARGUMENTS: INVALID_INPUT,
    CURSOR_WITHOUT_IDENTICAL_QUERY: INVALID_INPUT,
    PREFERENCE_REQUIRES_TRANSIT_MODE: INVALID_INPUT,
    PREFERENCE_UNSUPPORTED_FOR_BEST: INVALID_INPUT,
    TIMEZONE_DISAGREEMENT: UNSUPPORTED,
    AMBIGUOUS_LOCAL_ENCODING: UNSUPPORTED,
    ACCESSIBILITY_UNVERIFIABLE: UNSUPPORTED,
    TEMPLATE_UNVERIFIED: UNSUPPORTED,
    TIMETABLE_WINDOW: OUT_OF_RANGE,
    BAD_REQUEST: UPSTREAM_REJECTED,
    AUTH_OR_BLOCKED: UPSTREAM_REJECTED,
    RATE_LIMITED: UPSTREAM_REJECTED,
    SERVER_ERROR: UPSTREAM_REJECTED,
    NOT_FOUND: UPSTREAM_REJECTED,
    REQUEST_NOT_HONOURED: UPSTREAM_REJECTED,
    TIMEOUT: TRANSPORT_ERROR,
    CONNECTION_FAILURE: TRANSPORT_ERROR,
    NON_JSON_BODY: MALFORMED_UPSTREAM,
    UNEXPECTED_SHAPE: MALFORMED_UPSTREAM,
    TRUNCATED_CONTAINER: MALFORMED_UPSTREAM,
    DEPARTURE_BEFORE_REQUEST: CONSTRAINT_VIOLATION,
    ARRIVAL_AFTER_DEADLINE: CONSTRAINT_VIOLATION,
}

REASONS = tuple(REASON_OUTCOME)


def outcome_for_reason(reason):
    """The outcome that ``reason`` belongs to. An unknown reason is a code defect."""
    try:
        return REASON_OUTCOME[reason]
    except KeyError:
        raise ValueError(f"unmapped error reason: {reason!r}") from None


def _default_message(reason):
    return reason.replace("_", " ")


def error_result(reason, message=None, details=None):
    """Build the constant-shape error result. The single constructor.

    Returns a dict with exactly ``error``, ``outcome``, ``error_type`` and
    ``error_details``. ``error_details`` is ``{}`` when there is nothing to add.
    """
    outcome = outcome_for_reason(reason)
    return {
        "error": message if message else _default_message(reason),
        "outcome": outcome,
        "error_type": reason,
        "error_details": dict(details) if details else {},
    }


def success_result(outcome=OK, **fields):
    """Build a success result: ``outcome`` present, ``error`` never."""
    if outcome not in (OK, NO_ROUTES):
        raise ValueError(f"not a success outcome: {outcome!r}")
    payload = dict(fields)
    payload.pop("error", None)
    payload.pop("error_type", None)
    payload.pop("error_details", None)
    payload["outcome"] = outcome
    return payload


def is_error_result(payload):
    """True when a result carries the error marker the protocol keys on."""
    return isinstance(payload, dict) and bool(payload.get("error"))


class ToolError(Exception):
    """A classified tool failure; converts to the constant error shape."""

    def __init__(self, reason, message=None, details=None):
        self.reason = reason
        self.details = dict(details) if details else {}
        self.outcome = outcome_for_reason(reason)
        self.message = message if message else _default_message(reason)
        super().__init__(self.message)

    def to_result(self):
        return error_result(self.reason, self.message, self.details)

    def __repr__(self):
        return f"ToolError({self.reason!r}, {self.message!r}, {self.details!r})"


def fail(reason, message=None, details=None) -> NoReturn:
    """Raise a :class:`ToolError`. Never returns."""
    raise ToolError(reason, message, details)


__all__ = [
    "OK", "NO_ROUTES", "INVALID_INPUT", "UNSUPPORTED", "OUT_OF_RANGE",
    "UPSTREAM_REJECTED", "TRANSPORT_ERROR", "MALFORMED_UPSTREAM", "CONSTRAINT_VIOLATION",
    "OUTCOMES", "ERROR_OUTCOMES", "REASONS", "REASON_OUTCOME",
    "MALFORMED_TIME", "AMBIGUOUS_LOCAL_TIME", "NONEXISTENT_LOCAL_TIME",
    "TIMEZONE_UNRESOLVED", "UNKNOWN_TIMEZONE", "BBOX_WINDOW_REQUIRES_EXPLICIT_ZONE",
    "MIXED_ENDPOINT_FORM", "UNKNOWN_MODE", "UNKNOWN_PREFERENCE", "STREET_MODE_NOT_TRANSIT",
    "VALUE_OUT_OF_RANGE", "MUTUALLY_EXCLUSIVE_ARGUMENTS", "CURSOR_WITHOUT_IDENTICAL_QUERY",
    "PREFERENCE_REQUIRES_TRANSIT_MODE", "PREFERENCE_UNSUPPORTED_FOR_BEST",
    "TIMEZONE_DISAGREEMENT", "AMBIGUOUS_LOCAL_ENCODING", "ACCESSIBILITY_UNVERIFIABLE",
    "TEMPLATE_UNVERIFIED", "TIMETABLE_WINDOW", "BAD_REQUEST", "AUTH_OR_BLOCKED",
    "RATE_LIMITED", "SERVER_ERROR", "NOT_FOUND", "REQUEST_NOT_HONOURED", "TIMEOUT",
    "CONNECTION_FAILURE", "NON_JSON_BODY", "UNEXPECTED_SHAPE", "TRUNCATED_CONTAINER",
    "DEPARTURE_BEFORE_REQUEST", "ARRIVAL_AFTER_DEADLINE",
    "outcome_for_reason", "error_result", "success_result", "is_error_result",
    "ToolError", "fail",
]
