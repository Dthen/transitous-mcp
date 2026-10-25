"""Time classification, frame selection and the five accepted forms (§2.1, §2.2).

Every assertion here is about a pure decision: which form a caller string is,
which zone a naive clock is read in, and what instant the request is built from.
The host zone is never an input — the last test proves that by running the same
decisions in four host zones and requiring identical output.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import fixture_support as fs

from transit_core import errors, rendering, timeparse, timezones


UTC = timezone.utc


def aware(y, m, d, hh=0, mm=0, ss=0, offset_hours=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone(timedelta(hours=offset_hours)))


# ── the five forms ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "given, instant",
    [
        ("2026-10-27T09:00:00Z", aware(2026, 10, 27, 9)),
        ("2026-10-27T09:00:00z", aware(2026, 10, 27, 9)),
        ("2026-10-27t09:00:00Z", aware(2026, 10, 27, 9)),
        ("2026-10-27T09:00:00+01:00", aware(2026, 10, 27, 8)),
        ("2026-10-27T09:00:00+0100", aware(2026, 10, 27, 8)),
        ("2026-10-27T09:00:00-05:00", aware(2026, 10, 27, 14)),
        ("2026-10-27T09:00Z", aware(2026, 10, 27, 9)),
        ("2026-10-27t09:00:00z", aware(2026, 10, 27, 9)),
    ],
)
def test_instant_forms_preserve_their_meaning(given, instant):
    parsed = timeparse.classify_syntax(given)
    assert parsed.form == timeparse.FORM_INSTANT
    assert parsed.as_given == given                       # echoed verbatim
    assert parsed.instant == instant


def test_fractional_seconds_are_preserved_in_the_instant_but_not_in_the_wire_token():
    parsed = timeparse.classify_syntax("2026-10-27T09:00:00.500Z")
    assert parsed.form == timeparse.FORM_INSTANT
    assert parsed.instant == datetime(2026, 10, 27, 9, 0, 0, 500000, tzinfo=UTC)
    assert timeparse.classify("2026-10-27T09:00:00.500Z").wire_value == "2026-10-27T09:00:00Z"


@pytest.mark.parametrize(
    "given, naive",
    [
        ("2026-10-27T09:00", datetime(2026, 10, 27, 9)),
        ("2026-10-27T09:00:00", datetime(2026, 10, 27, 9)),
        ("2026-10-27T09:00:30", datetime(2026, 10, 27, 9, 0, 30)),
        ("2026-10-27T09:00:30.250", datetime(2026, 10, 27, 9, 0, 30, 250000)),
        ("2026-10-27 09:00", datetime(2026, 10, 27, 9)),
        ("2026-10-27 09:00:00", datetime(2026, 10, 27, 9)),
        ("2026-10-27t09:00", datetime(2026, 10, 27, 9)),
    ],
)
def test_naive_local_forms_carry_a_wall_clock(given, naive):
    parsed = timeparse.classify_syntax(given)
    assert parsed.form == timeparse.FORM_NAIVE
    assert parsed.naive == naive
    assert parsed.instant is None


def test_date_only_is_local_midnight_in_the_frame():
    parsed = timeparse.classify_syntax("2026-10-27")
    assert parsed.form == timeparse.FORM_DATE_ONLY
    assert parsed.naive == datetime(2026, 10, 27, 0, 0)
    frame = timeparse.classify("2026-10-27", endpoint_zone="Europe/London")
    assert frame.instant == aware(2026, 10, 27, 0)         # GMT after 25 October
    assert frame.to_object()["interpreted_instant"]["local"] == "2026-10-27T00:00:00"


def test_omitted_time_is_resolved_once_and_echoed_as_null():
    calls = []

    def clock():
        calls.append(1)
        return aware(2026, 10, 24, 12, 34, 56, 0)

    frame = timeparse.classify(timeparse.OMITTED, now=clock)
    assert len(calls) == 1                                 # exactly one clock read
    assert frame.form == timeparse.FORM_OMITTED
    assert frame.instant == aware(2026, 10, 24, 12, 34, 56)
    obj = frame.to_object()
    assert obj["as_given"] is None
    assert obj["frame_zone"] is None
    assert obj["frame_source"] == "not_applicable"


def test_omitted_time_is_also_accepted_as_none():
    frame = timeparse.classify(None, now=aware(2026, 10, 24, 1, 2, 3))
    assert frame.form == timeparse.FORM_OMITTED
    assert frame.wire_value == "2026-10-24T01:02:03Z"


@pytest.mark.parametrize(
    "given",
    [
        "",
        "   ",
        "2026-10-27T",             # trailing T with no time
        "2026-10-27T09",           # incomplete time
        "09:00:00Z",               # bare time, no date
        "2026-10-2709:00:00+01:00",# offset with no separator at all
        "2026-10-27 09:00:00Z",    # space separator never carries an offset
        "2026-10-27 09:00:00z",
        "2026-10-27 09:00:00+01:00",
        "2026-02-30T09:00:00Z",    # calendar overflow
        "2026-13-01T09:00:00Z",
        "2026-10-27T25:00:00Z",
        "2026-10-27T09:60:00Z",
        "2026-10-27T09:00:00+24:00",
        "2026-10-27T09:00:00+01:60",
        "27/10/2026 09:00",
        "2026/10/27",
        20261027,
        1758896400,
        ["2026-10-27T09:00:00Z"],
        {"time": "2026-10-27T09:00:00Z"},
    ],
)
def test_malformed_forms_are_named_before_any_request(given):
    with pytest.raises(errors.ToolError) as caught:
        timeparse.classify_syntax(given)
    assert caught.value.outcome == errors.INVALID_INPUT
    assert caught.value.reason == errors.MALFORMED_TIME
    assert caught.value.details["as_given"] == given


def test_a_space_separator_is_only_accepted_without_an_offset():
    """§2.1: the space separator belongs to the naive form, which has no offset."""
    for given in (
        "2026-10-27 09:00:00Z",
        "2026-10-27 09:00:00z",
        "2026-10-27 09:00:00+01:00",
    ):
        with pytest.raises(errors.ToolError) as caught:
            timeparse.classify_syntax(given)
        assert caught.value.reason == errors.MALFORMED_TIME, given
        assert "separator" in caught.value.details["reason"], given
    # the same digits with a T separator are an instant, in either case
    upper = timeparse.classify_syntax("2026-10-27T09:00:00Z")
    lower = timeparse.classify_syntax("2026-10-27t09:00:00z")
    assert upper.form == timeparse.FORM_INSTANT
    assert lower.form == timeparse.FORM_INSTANT
    # and the offset-less space form still carries a wall clock
    spaced = timeparse.classify_syntax("2026-10-27 09:00:00")
    assert spaced.form == timeparse.FORM_NAIVE


def test_a_malformed_time_never_needs_a_zone():
    # no endpoint zone, no transport: the decision is pre-request and total
    with pytest.raises(errors.ToolError) as caught:
        timeparse.classify("2026-02-30", endpoint_zone=None)
    assert caught.value.reason == errors.MALFORMED_TIME


# ── frame selection ─────────────────────────────────────────────────


def test_depart_after_reads_a_naive_clock_in_the_origin_zone():
    frame = timeparse.classify(
        "2026-07-15T09:00",
        control=timeparse.CONTROL_DEPART_AFTER,
        endpoint_zone="Europe/London",
        endpoint_zone_source="caller_endpoint_timezone",
    )
    obj = frame.to_object()
    assert obj["control"] == "depart_after"
    assert obj["frame_zone"] == "Europe/London"
    assert obj["frame_source"] == "caller_endpoint_timezone"
    assert frame.instant == aware(2026, 7, 15, 8)            # BST: 09:00 local = 08:00Z
    assert frame.wire_value == "2026-07-15T08:00:00Z"


def test_arrive_by_reads_a_naive_clock_in_the_destination_zone():
    frame = timeparse.classify(
        "2026-10-27T22:00",
        control=timeparse.CONTROL_ARRIVE_BY,
        endpoint_zone="Europe/London",
        endpoint_zone_source="endpoint_stop_info",
    )
    obj = frame.to_object()
    assert obj["control"] == "arrive_by"
    assert obj["frame_zone"] == "Europe/London"
    assert frame.instant == aware(2026, 10, 27, 22)
    assert frame.wire_value == "2026-10-27T22:00:00Z"


def test_caller_timezone_wins_for_either_control_and_is_echoed():
    for control in (timeparse.CONTROL_DEPART_AFTER, timeparse.CONTROL_ARRIVE_BY):
        frame = timeparse.classify(
            "2026-07-15T09:00",
            control=control,
            caller_timezone="Europe/Berlin",
            endpoint_zone="Europe/London",
            endpoint_zone_source="endpoint_stop_info",
        )
        obj = frame.to_object()
        assert obj["frame_zone"] == "Europe/Berlin"
        assert obj["frame_source"] == "caller_timezone"
        assert frame.instant == aware(2026, 7, 15, 7)        # 09:00 CEST = 07:00Z


def test_caller_endpoint_declaration_outranks_discovered_metadata():
    frame = timeparse.classify(
        "2026-10-27T09:00",
        endpoint_zone="Europe/Berlin",
        endpoint_zone_source="caller_endpoint_timezone",
    )
    assert frame.to_object()["frame_source"] == "caller_endpoint_timezone"


def test_an_explicit_instant_needs_no_frame_at_all():
    frame = timeparse.classify("2026-10-27T09:00:00+01:00", endpoint_zone=None)
    obj = frame.to_object()
    assert obj["frame_zone"] is None
    assert obj["frame_source"] == "not_applicable"
    assert obj["dst_flags"] == []
    assert frame.wire_value == "2026-10-27T08:00:00Z"        # same instant, UTC wire form


def test_an_explicit_instant_is_never_reread_in_the_endpoint_zone():
    # 09:00+01:00 is 08:00Z; it must not become 09:00 in Europe/London
    frame = timeparse.classify(
        "2026-10-27T09:00:00+01:00",
        endpoint_zone="Europe/London",
        endpoint_zone_source="endpoint_stop_info",
    )
    assert frame.instant == aware(2026, 10, 27, 8)


def test_a_naive_clock_without_an_authority_is_timezone_unresolved():
    with pytest.raises(errors.ToolError) as caught:
        timeparse.classify("2026-10-27T09:00", control="depart_after")
    assert caught.value.outcome == errors.INVALID_INPUT
    assert caught.value.reason == errors.TIMEZONE_UNRESOLVED
    assert "remedy" in caught.value.details


def test_an_unknown_caller_zone_is_rejected_as_unknown_timezone():
    with pytest.raises(errors.ToolError) as caught:
        timeparse.classify("2026-10-27T09:00", caller_timezone="Mars/Olympus_Mons")
    assert caught.value.reason == errors.UNKNOWN_TIMEZONE
    assert caught.value.details["zone"] == "Mars/Olympus_Mons"


def test_an_empty_caller_zone_is_rejected():
    with pytest.raises(errors.ToolError) as caught:
        timeparse.classify("2026-10-27T09:00", caller_timezone="   ")
    assert caught.value.reason == errors.UNKNOWN_TIMEZONE


def test_time_frame_object_has_the_documented_six_keys_plus_flags():
    frame = timeparse.classify(
        "2026-10-27T09:00",
        endpoint_zone="Europe/London",
        endpoint_zone_source="caller_endpoint_timezone",
    )
    obj = frame.to_object()
    assert set(obj) == {
        "as_given",
        "form",
        "control",
        "interpreted_instant",
        "frame_zone",
        "frame_source",
        "dst_flags",
    }
    assert obj["as_given"] == "2026-10-27T09:00"
    assert obj["form"] == "naive_local"
    assert obj["dst_flags"] == []
    stamp = obj["interpreted_instant"]
    assert set(stamp) == {
        "iso", "epoch_seconds", "local", "local_date",
        "utc_offset_seconds", "zone", "zone_source", "display",
    }
    assert stamp["iso"] == "2026-10-27T09:00:00+00:00"
    # the naive clock's display zone came from the caller's declaration
    assert stamp["zone"] == "Europe/London"
    assert stamp["zone_source"] == rendering.CALLER_ENDPOINT


@pytest.mark.parametrize(
    "given, kwargs, zone, zone_source",
    [
        # the interpretation frame *is* the display zone for a naive clock
        ("2026-10-27T09:00",
         dict(endpoint_zone="Europe/London", endpoint_zone_source=timezones.CALLER_ENDPOINT),
         "Europe/London", rendering.CALLER_ENDPOINT),
        ("2026-10-27T09:00",
         dict(endpoint_zone="Europe/London", endpoint_zone_source=timezones.STOP_INFO),
         "Europe/London", rendering.RESPONSE_PLACE),
        ("2026-10-27T09:00",
         dict(endpoint_zone="Europe/London", endpoint_zone_source=timezones.REVERSE_GEOCODE),
         "Europe/London", rendering.INFERRED_NEARBY_STOP),
        ("2026-10-27T09:00",
         dict(caller_timezone="Europe/Berlin", endpoint_zone="Europe/London",
              endpoint_zone_source=timezones.STOP_INFO),
         "Europe/Berlin", rendering.CALLER_TIMEZONE),
        # an explicit instant has no interpretation frame, but the zone it is
        # displayed in still has a provenance and is never attributed to a
        # response nobody fetched
        ("2026-10-27T09:00:00Z",
         dict(caller_timezone="Europe/Berlin"),
         "Europe/Berlin", rendering.CALLER_TIMEZONE),
        ("2026-10-27T09:00:00Z",
         dict(endpoint_zone="Europe/London", endpoint_zone_source=timezones.STOP_INFO),
         "Europe/London", rendering.RESPONSE_PLACE),
        ("2026-10-27T09:00:00Z",
         dict(endpoint_zone="Europe/London", endpoint_zone_source=timezones.REVERSE_GEOCODE),
         "Europe/London", rendering.INFERRED_NEARBY_STOP),
        # omitted is the same story
        (timeparse.OMITTED,
         dict(caller_timezone="Europe/Berlin"),
         "Europe/Berlin", rendering.CALLER_TIMEZONE),
        # a zone nobody supplied stays honestly unavailable
        ("2026-10-27T09:00:00Z", {}, None, rendering.UNAVAILABLE),
        (timeparse.OMITTED, {}, None, rendering.UNAVAILABLE),
    ],
)
def test_display_zone_provenance_matches_the_source_that_supplied_it(
    given, kwargs, zone, zone_source
):
    """§2.5 ``zone_source`` is provenance for the *display* zone.

    §2.1 keeps ``frame_source`` as provenance for the interpretation frame, which
    is ``not_applicable`` for an explicit instant or an omitted time even when a
    display zone is known. The two must not be conflated, and an unknown zone is
    never reported as if a response had supplied it.
    """
    frame = timeparse.classify(given, now=aware(2026, 10, 24, 12, 0), **kwargs)
    obj = frame.to_object()
    stamp = obj["interpreted_instant"]
    assert set(stamp) == {
        "iso", "epoch_seconds", "local", "local_date",
        "utc_offset_seconds", "zone", "zone_source", "display",
    }
    assert stamp["zone"] == zone
    assert stamp["zone_source"] == zone_source
    if obj["form"] in (timeparse.FORM_INSTANT, timeparse.FORM_OMITTED):
        assert obj["frame_zone"] is None
        assert obj["frame_source"] == "not_applicable"
    if zone is None:
        assert "zone unavailable" in stamp["display"]


def test_a_frame_built_without_display_provenance_never_invents_one():
    """T1-02: a display zone whose origin was not recorded reports ``unavailable``.

    ``display_zone_source`` is what carries provenance into the timestamp object; a
    frame that has a zone but no recorded origin must not borrow one, because a zone
    nobody can attribute is not evidence a response supplied it.
    """
    frame = timeparse.TimeFrame(
        form=timeparse.FORM_NAIVE,
        instant=aware(2026, 10, 27, 9),
        frame_zone="Europe/London",
        frame_source=timezones.NOT_APPLICABLE,
        display_zone="Europe/London",
    )
    stamp = frame.to_object()["interpreted_instant"]
    assert stamp["zone"] == "Europe/London"
    assert stamp["zone_source"] == rendering.UNAVAILABLE


def test_wire_value_is_rfc3339_utc_for_every_form():
    cases = {
        "2026-10-27T09:00:00Z": "2026-10-27T09:00:00Z",
        "2026-10-27T09:00:00+01:00": "2026-10-27T08:00:00Z",
        "2026-10-27T09:00:00-05:00": "2026-10-27T14:00:00Z",
        "2026-10-27T09:00:00.900Z": "2026-10-27T09:00:00Z",
    }
    for given, wire in cases.items():
        assert timeparse.classify(given).wire_value == wire, given


def test_date_only_in_a_summer_frame_uses_the_summer_offset():
    frame = timeparse.classify("2026-07-15", endpoint_zone="Europe/London")
    assert frame.wire_value == "2026-07-14T23:00:00Z"        # local midnight BST
    assert frame.to_object()["interpreted_instant"]["local"] == "2026-07-15T00:00:00"


# ── host-zone invariance (§9.5) ─────────────────────────────────────


HOST_ZONE_SCRIPT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from transit_core import errors, rendering, timeparse
out = {}
out["render"] = rendering.timestamp(
    __import__("datetime").datetime(2026, 10, 27, 8, 0, tzinfo=__import__("datetime").timezone.utc),
    zone="Europe/London",
)
frame = timeparse.classify("2026-07-15T09:00", endpoint_zone="Europe/London")
out["naive"] = [frame.wire_value, frame.to_object()["interpreted_instant"]]
out["explicit"] = timeparse.classify(
    "2026-03-29T01:30:00+00:00", endpoint_zone="Europe/London"
).to_object()["interpreted_instant"]
# display-zone provenance for the two forms whose frame is not applicable: the
# decision must not depend on the host zone either
explicit_tz = timeparse.classify(
    "2026-10-27T09:00:00Z", caller_timezone="Europe/Berlin"
).to_object()["interpreted_instant"]
omitted_tz = timeparse.classify(
    timeparse.OMITTED, caller_timezone="Europe/Berlin",
    now=__import__("datetime").datetime(2026, 10, 24, 12, 0, tzinfo=__import__("datetime").timezone.utc),
).to_object()["interpreted_instant"]
out["provenance"] = {
    "explicit": [explicit_tz["zone"], explicit_tz["zone_source"], explicit_tz["local"]],
    "omitted": [omitted_tz["zone"], omitted_tz["zone_source"], omitted_tz["local"]],
}
try:
    timeparse.classify("2026-10-25T01:30", endpoint_zone="Europe/London")
except errors.ToolError as exc:
    out["ambiguous"] = exc.to_result()
print(json.dumps(out, sort_keys=True))
"""


def test_decisions_are_identical_under_four_host_zones():
    outputs = {}
    for host_zone in ("UTC", "Europe/London", "America/New_York", "Australia/Sydney"):
        env = dict(os.environ, TZ=host_zone, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            ["/usr/bin/python3", "-c", HOST_ZONE_SCRIPT, fs.REPO_ROOT],
            capture_output=True,
            text=True,
            env=env,
            cwd=fs.REPO_ROOT,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        outputs[host_zone] = json.loads(result.stdout)
    baseline = outputs["UTC"]
    for host_zone, payload in outputs.items():
        assert payload == baseline, host_zone
    # and the answers are the ones the specification demands
    assert baseline["render"]["iso"] == "2026-10-27T08:00:00+00:00"       # GMT by then
    assert baseline["naive"][0] == "2026-07-15T08:00:00Z"
    assert baseline["explicit"]["local"] == "2026-03-29T02:30:00"
    assert baseline["ambiguous"]["error_type"] == "ambiguous_local_time"
    assert baseline["provenance"]["explicit"] == [
        "Europe/Berlin", "caller_timezone", "2026-10-27T10:00:00"
    ]
    assert baseline["provenance"]["omitted"] == [
        "Europe/Berlin", "caller_timezone", "2026-10-24T14:00:00"
    ]
