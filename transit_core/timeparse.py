"""Input classification, frame selection and DST enforcement (§2.1–§2.3).

Every time-accepting argument is classified into exactly one form **before any
upstream request**, and the classification is echoed in ``time_frame``:

===============  ==================================================  ==========================
form             accepted syntax                                     interpretation
===============  ==================================================  ==========================
``instant``      RFC 3339 date-time with a ``T``/``t`` separator      the instant is the meaning
                 and ``Z``, ``z``, ``±HH:MM``, ``±HHMM``              and is never re-read
``naive_local``  ``YYYY-MM-DDTHH:MM[:SS[.f]]``, ``T``/``t``/space      a wall clock in the frame
``date_only``    ``YYYY-MM-DD``                                       local midnight in the frame
``omitted``      absent or ``null``                                   the current instant, once
===============  ==================================================  ==========================

Anything else — including an impossible calendar date, a trailing ``T``, a bare
time, an offset carried on a space separator or with no separator at all, calendar
overflow — is ``invalid_input`` / ``malformed_time``, raised before a request
could be assembled. Only the naive form takes a space separator, and it carries
no offset, so ``2026-10-27 09:00:00Z`` is malformed while
``2026-10-27 09:00:00`` is a wall clock and ``2026-10-27T09:00:00Z`` (or
``2026-10-27t09:00:00z``) is an instant.

Frame selection is identical for both directions:

* caller ``timezone`` → that zone, source ``caller_timezone``, for either control;
* else the direction-relevant endpoint zone (``arrive_by``: destination,
  ``depart_after``: origin), with the source that established it;
* else unresolved → ``invalid_input`` / ``timezone_unresolved``.

An explicit instant needs no frame at all: its ``frame_zone`` is ``null`` and its
``frame_source`` is ``not_applicable``. A caller endpoint-zone declaration is an
authority about the endpoint; a caller ``timezone`` is a deliberate frame and is
never checked against it.

The **display zone** is a different question from the frame: every timestamp is
rendered in some zone, and §2.5 requires that zone's ``zone_source``. It is carried
internally as ``TimeFrame.display_zone_source`` in ``frame_source`` vocabulary, so
an explicit instant (frame ``not_applicable``) still says the display zone came
from the caller's ``timezone`` or endpoint declaration, and an unknown zone is
reported ``unavailable`` rather than attributed to a response that was never
fetched. ``frame_source`` is never rewritten to carry this.

The host time zone is never read: ``now`` must be timezone-aware, and every
conversion names its zone explicitly.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import NoReturn, Optional

from . import errors, rendering, timezones

UTC = timezone.utc

FORM_INSTANT = "instant"
FORM_NAIVE = "naive_local"
FORM_DATE_ONLY = "date_only"
FORM_OMITTED = "omitted"

FORMS = (FORM_INSTANT, FORM_NAIVE, FORM_DATE_ONLY, FORM_OMITTED)

CONTROL_DEPART_AFTER = "depart_after"
CONTROL_ARRIVE_BY = "arrive_by"

CONTROLS = (CONTROL_DEPART_AFTER, CONTROL_ARRIVE_BY)


class _Omitted:
    """Sentinel for an argument that was absent (or ``null``)."""

    __slots__ = ()

    def __repr__(self):
        return "timeparse.OMITTED"

    def __bool__(self):
        return False


OMITTED = _Omitted()

_DATE = r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
_TIME = r"(?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2})(?:\.(?P<fraction>\d{1,9}))?)?"
#: An instant is RFC 3339 date-time: the separator is ``T``/``t`` only. The space
#: separator is granted to the naive form alone, which carries no offset (§2.1).
_SEPARATOR_T = r"[Tt]"
_SEPARATOR = r"[Tt ]"
_OFFSET = r"(?P<offset>Z|z|[+-]\d{2}:\d{2}|[+-]\d{4})"

_INSTANT_RE = re.compile(rf"^{_DATE}{_SEPARATOR_T}{_TIME}{_OFFSET}$")
_NAIVE_RE = re.compile(rf"^{_DATE}{_SEPARATOR}{_TIME}$")
_DATE_ONLY_RE = re.compile(rf"^{_DATE}$")

#: A trailing offset on a string whose separator is a space: rejected with a
#: reason that names the actual mistake instead of a generic one.
_SPACED_OFFSET_RE = re.compile(rf"^{_DATE} {_TIME}(?:Z|z|[+-]\d{{2}}:?\d{{2}})$")


@dataclass(frozen=True)
class ParsedTime:
    """The syntactic decision about one argument. Pure; no zone, no clock."""

    form: str
    as_given: Optional[str] = None
    naive: Optional[datetime] = None
    instant: Optional[datetime] = None

    @property
    def needs_frame(self):
        return self.form in (FORM_NAIVE, FORM_DATE_ONLY)


@dataclass(frozen=True)
class FrameChoice:
    """The frame zone selected for a naive clock, and what established it."""

    zone: Optional[str] = None
    source: str = timezones.NOT_APPLICABLE

    @property
    def resolved(self):
        return self.zone is not None


@dataclass(frozen=True)
class TimeFrame:
    """A classified time: its form, its instant, and the frame it was read in.

    ``display_zone_source`` is the *provenance of the display zone* in
    ``frame_source`` vocabulary (or ``None`` when nothing established it). It is an
    internal field: ``to_object`` translates it to the §2.5 ``zone_source`` and
    never echoes it beside ``frame_source``, so the two provenances stay separate
    and the timestamp object keeps its documented keys.
    """

    as_given: Optional[str] = None
    form: str = FORM_OMITTED
    control: str = CONTROL_DEPART_AFTER
    instant: Optional[datetime] = None
    frame_zone: Optional[str] = None
    frame_source: str = timezones.NOT_APPLICABLE
    display_zone: Optional[str] = None
    display_zone_source: Optional[str] = None
    dst_flags: tuple = ()

    @property
    def wire_value(self):
        """RFC 3339 in UTC — what a Transitous request carries."""
        return timezones.iso_z(self.instant)

    def to_object(self):
        """The ``time_frame`` object of §2.1."""
        zone = self.display_zone if self.display_zone is not None else self.frame_zone
        zone_source = self.display_zone_source
        if zone_source is None and self.display_zone is None:
            # a consumer-built frame that only knows the interpretation frame
            zone_source = self.frame_source
        return {
            "as_given": self.as_given,
            "form": self.form,
            "control": self.control,
            "interpreted_instant": rendering.timestamp(
                self.instant,
                zone=zone,
                zone_source=rendering.zone_source_for_frame(zone_source),
            ),
            "frame_zone": self.frame_zone,
            "frame_source": self.frame_source,
            "dst_flags": list(self.dst_flags),
        }


def wire_value(frame):
    """Convenience wrapper: the wire token for a classified time."""
    return frame.wire_value


# ── syntax ─────────────────────────────────────────────────────────


def _malformed(value, why) -> NoReturn:
    errors.fail(
        errors.MALFORMED_TIME,
        f"time {value!r} is not a recognised time: {why}",
        {"as_given": value, "reason": why},
    )


def _build(match, invalid):
    groups = match.groupdict()
    fraction = groups.get("fraction") or ""
    micro = int((fraction + "000000")[:6]) if fraction else 0
    try:
        return datetime(
            int(groups["year"]),
            int(groups["month"]),
            int(groups["day"]),
            int(groups.get("hour") or 0),
            int(groups.get("minute") or 0),
            int(groups.get("second") or 0),
            micro,
        )
    except ValueError as exc:
        _malformed(invalid, str(exc))


def _offset_from(token, invalid):
    if token in ("Z", "z"):
        return UTC
    sign = 1 if token[0] == "+" else -1
    digits = token[1:].replace(":", "")
    hours, minutes = int(digits[0:2]), int(digits[2:4])
    if hours > 23 or minutes > 59:
        _malformed(invalid, f"offset {token} is out of range")
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def classify_syntax(value):
    """Classify one time argument by syntax alone.

    Raises ``invalid_input`` / ``malformed_time`` for anything outside the four
    accepted forms. Never consults a zone or a clock, so it always runs before a
    request is assembled.
    """
    if value is OMITTED or value is None:
        return ParsedTime(form=FORM_OMITTED)
    if not isinstance(value, str):
        _malformed(value, "a time argument must be a string")
    if not value:
        _malformed(value, "the empty string is not a time")
    if value != value.strip():
        _malformed(value, "surrounding whitespace is not part of any accepted form")

    date_only = _DATE_ONLY_RE.match(value)
    if date_only is not None:
        naive = _build(date_only, value)
        return ParsedTime(form=FORM_DATE_ONLY, as_given=value, naive=naive)

    instant = _INSTANT_RE.match(value)
    if instant is not None:
        naive = _build(instant, value)
        return ParsedTime(
            form=FORM_INSTANT,
            as_given=value,
            instant=naive.replace(tzinfo=_offset_from(instant.group("offset"), value)),
        )

    naive_match = _NAIVE_RE.match(value)
    if naive_match is not None:
        return ParsedTime(form=FORM_NAIVE, as_given=value, naive=_build(naive_match, value))

    if _SPACED_OFFSET_RE.match(value) is not None:
        _malformed(
            value,
            "an offset needs a date-time T separator; the space separator is "
            "accepted only for a naive clock with no offset",
        )
    if value.endswith(("T", "t", " ")):
        _malformed(value, "a date-time separator must be followed by a clock")
    if ":" in value and "-" not in value.split(":")[0]:
        _malformed(value, "a clock needs a date")
    if len(value) >= 11 and value[10] not in "Tt " and not value.endswith("Z"):
        _malformed(value, "an offset needs a date-time separator")
    _malformed(value, "expected an instant, a naive local time, a date, or null")


# ── clock ──────────────────────────────────────────────────────────


def resolve_now(now=None):
    """Resolve ``now`` exactly once, into an aware UTC instant truncated to seconds.

    ``now`` may be an aware datetime or a zero-argument callable returning one; a
    callable is invoked once. A naive datetime is refused: accepting it would make
    the host zone an input.
    """
    if callable(now):
        now = now()
    if now is None:
        return datetime.now(UTC).replace(microsecond=0)
    if not isinstance(now, datetime):
        raise ValueError(f"now must be an aware datetime or a callable, not {type(now).__name__}")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware; the host zone is never assumed")
    return now.astimezone(UTC).replace(microsecond=0)


# ── frame selection ────────────────────────────────────────────────


def _control(control):
    if control not in CONTROLS:
        raise ValueError(f"unknown time control: {control!r}")
    return control


def choose_frame(
    *,
    control,
    caller_timezone=None,
    origin_zone=None,
    origin_zone_source=None,
    destination_zone=None,
    destination_zone_source=None,
    endpoint_zone=None,
    endpoint_zone_source=None,
):
    """Select the interpretation frame by §2.2 precedence.

    ``caller_timezone`` wins outright. Otherwise the direction-relevant endpoint
    zone is used: ``destination_zone`` for ``arrive_by``, ``origin_zone`` for
    ``depart_after``. ``endpoint_zone`` is the shorthand a caller uses when it has
    already picked the relevant endpoint. A supplied zone is validated here, so an
    unresolvable id fails before any request.
    """
    control = _control(control)
    if caller_timezone is not None:
        return FrameChoice(zone=timezones.resolve_zone_name(caller_timezone), source=timezones.CALLER_TIMEZONE)

    if control == CONTROL_ARRIVE_BY:
        zone, source = destination_zone, destination_zone_source
    else:
        zone, source = origin_zone, origin_zone_source
    if zone is None and endpoint_zone is not None:
        zone, source = endpoint_zone, endpoint_zone_source
    if zone is None:
        return FrameChoice(zone=None, source=timezones.NOT_APPLICABLE)
    return FrameChoice(
        zone=timezones.resolve_zone_name(zone),
        source=source if source in _SOURCE_VALUES else timezones.CALLER_ENDPOINT,
    )


_SOURCE_VALUES = frozenset(
    {
        timezones.CALLER_TIMEZONE,
        timezones.CALLER_ENDPOINT,
        timezones.STOP_INFO,
        timezones.REVERSE_GEOCODE,
        timezones.RESPONSE,
        timezones.NOT_APPLICABLE,
    }
)


# ── interpretation ────────────────────────────────────────────────


def _display_source(zone, frame):
    """Provenance of ``zone``, when it is the zone the frame established.

    Returns a ``frame_source`` value for :class:`TimeFrame.display_zone_source`,
    or ``None`` when nothing established the zone — which the timestamp reports as
    ``unavailable``. A zone chosen by a caller or an endpoint authority keeps its
    own provenance even for forms whose interpretation frame is not applicable.
    """
    if zone is None or frame is None or frame.zone is None or zone != frame.zone:
        return None
    return frame.source


def interpret(parsed, *, control, frame=None, display_zone=None, now=None):
    """Turn a parsed time into a :class:`TimeFrame`, enforcing DST validity."""
    control = _control(control)
    if parsed.form == FORM_INSTANT:
        zone = display_zone if display_zone is not None else (frame.zone if frame is not None else None)
        return TimeFrame(
            as_given=parsed.as_given,
            form=FORM_INSTANT,
            control=control,
            instant=parsed.instant,
            frame_zone=None,
            frame_source=timezones.NOT_APPLICABLE,
            display_zone=zone,
            display_zone_source=_display_source(zone, frame),
        )

    if parsed.form == FORM_OMITTED:
        return TimeFrame(
            as_given=None,
            form=FORM_OMITTED,
            control=control,
            instant=resolve_now(now),
            frame_zone=None,
            frame_source=timezones.NOT_APPLICABLE,
            display_zone=display_zone,
            display_zone_source=_display_source(display_zone, frame),
        )

    if frame is not None:
        zone, zone_source = frame.zone, frame.source
    else:
        zone, zone_source = None, timezones.NOT_APPLICABLE
    if zone is None:
        errors.fail(
            errors.TIMEZONE_UNRESOLVED,
            "a naive or date-only time needs a frame zone, and none was established",
            {"form": parsed.form, "remedy": timezones.REMEDY_UNRESOLVED},
        )
    validity = timezones.classify_local(parsed.naive, zone)
    if validity.status == "ambiguous":
        errors.fail(
            errors.AMBIGUOUS_LOCAL_TIME,
            f"{parsed.as_given!r} is ambiguous in {validity.zone}",
            validity.error_details(),
        )
    if validity.status == "nonexistent":
        errors.fail(
            errors.NONEXISTENT_LOCAL_TIME,
            f"{parsed.as_given!r} does not exist in {validity.zone}",
            validity.error_details(),
        )
    return TimeFrame(
        as_given=parsed.as_given,
        form=parsed.form,
        control=control,
        instant=validity.candidates[0][1],
        frame_zone=validity.zone,
        frame_source=zone_source,
        display_zone=validity.zone,
        display_zone_source=zone_source,
    )


def classify(
    value,
    *,
    control=CONTROL_DEPART_AFTER,
    caller_timezone=None,
    endpoint_zone=None,
    endpoint_zone_source=None,
    origin_zone=None,
    origin_zone_source=None,
    destination_zone=None,
    destination_zone_source=None,
    now=None,
):
    """Classify, choose the frame and interpret, in one call.

    For a naive clock the frame must already be known (the caller has run
    discovery); an explicit instant needs no frame. ``caller_timezone`` is
    validated even when the time is an explicit instant, because it is echoed in
    the frame object and a caller who supplied a zone deserves to hear that it is
    not a zone.
    """
    parsed = classify_syntax(value)
    frame = choose_frame(
        control=control,
        caller_timezone=caller_timezone,
        origin_zone=origin_zone,
        origin_zone_source=origin_zone_source,
        destination_zone=destination_zone,
        destination_zone_source=destination_zone_source,
        endpoint_zone=endpoint_zone,
        endpoint_zone_source=endpoint_zone_source,
    )
    display_zone = frame.zone
    if display_zone is None:
        display_zone = endpoint_zone
    return interpret(parsed, control=control, frame=frame, display_zone=display_zone, now=now)


__all__ = [
    "FORM_INSTANT", "FORM_NAIVE", "FORM_DATE_ONLY", "FORM_OMITTED", "FORMS",
    "CONTROL_DEPART_AFTER", "CONTROL_ARRIVE_BY", "CONTROLS",
    "OMITTED", "ParsedTime", "FrameChoice", "TimeFrame",
    "classify_syntax", "resolve_now", "choose_frame", "interpret", "classify",
    "wire_value",
]
