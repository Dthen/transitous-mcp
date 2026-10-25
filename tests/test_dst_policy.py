"""DST policy: ambiguous, nonexistent, explicit instants, both hemispheres (§2.3).

The rule is deterministic and local: classification happens with no request in
flight, both ambiguous candidates are reported instead of one being chosen, a
spring-gap reading reports the gap it falls in, and an offset-bearing input is an
instant — never re-read as a wall clock.
"""

from datetime import datetime, timedelta, timezone

import pytest

import fixture_support as fs

from transit_core import errors, http, timeparse, timezones

UTC = timezone.utc


def aware(y, m, d, hh, mm, offset_hours=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone(timedelta(hours=offset_hours)))


# ── repeated hour: both candidates ──────────────────────────────────


def test_ambiguous_local_time_reports_both_candidates():
    validity = timezones.classify_local(datetime(2026, 10, 25, 1, 30), "Europe/London")
    assert validity.status == "ambiguous"
    assert len(validity.candidates) == 2
    offsets = sorted(c[0] for c in validity.candidates)
    instants = sorted(c[1] for c in validity.candidates)
    assert offsets == [0, 3600]
    assert instants == [aware(2026, 10, 25, 0, 30), aware(2026, 10, 25, 1, 30)]


def test_an_ambiguous_naive_time_is_invalid_input_with_both_candidates():
    with pytest.raises(errors.ToolError) as caught:
        timeparse.classify("2026-10-25T01:30", endpoint_zone="Europe/London")
    exc = caught.value
    assert exc.outcome == errors.INVALID_INPUT
    assert exc.reason == errors.AMBIGUOUS_LOCAL_TIME
    assert exc.details["zone"] == "Europe/London"
    pairs = exc.details["candidates"]
    assert {p["utc_offset_seconds"] for p in pairs} == {0, 3600}
    assert {p["instant"] for p in pairs} == {"2026-10-25T00:30:00Z", "2026-10-25T01:30:00Z"}
    assert "explicit offset" in exc.details["remedy"]
    assert "timezone" in exc.details["remedy"]


def test_date_only_midnight_passes_through_the_same_validity_gate(monkeypatch):
    """A date-only value is local midnight, so the local-validity gate applies to it.

    Exotic zones do transition at midnight, but which ones depends on the installed
    tz database; the wiring is pinned here by substituting the gate's verdict so the
    test cannot start failing when tzdata changes.
    """
    real_gate = timezones.classify_local

    def fake_gate(naive, zone):
        verdict = real_gate(naive, zone)
        assert naive == datetime(2026, 3, 29, 0, 0)          # date-only became midnight
        return timezones.LocalValidity(
            status="nonexistent",
            zone="Europe/London",
            naive=naive,
            candidates=(),
            gap_start=datetime(2026, 3, 29, 0, 0),
            gap_end=datetime(2026, 3, 29, 1, 0),
        )

    monkeypatch.setattr(timezones, "classify_local", fake_gate)
    with pytest.raises(errors.ToolError) as caught:
        timeparse.classify("2026-03-29", endpoint_zone="Europe/London")
    assert caught.value.reason == errors.NONEXISTENT_LOCAL_TIME


# ── spring gap: the gap is named ────────────────────────────────────


def test_nonexistent_local_time_reports_the_gap():
    validity = timezones.classify_local(datetime(2026, 3, 29, 1, 30), "Europe/London")
    assert validity.status == "nonexistent"
    assert validity.gap_start == datetime(2026, 3, 29, 1, 0)
    assert validity.gap_end == datetime(2026, 3, 29, 2, 0)


def test_a_nonexistent_naive_time_is_invalid_input_with_the_gap():
    with pytest.raises(errors.ToolError) as caught:
        timeparse.classify("2026-03-29T01:30", endpoint_zone="Europe/London")
    exc = caught.value
    assert exc.outcome == errors.INVALID_INPUT
    assert exc.reason == errors.NONEXISTENT_LOCAL_TIME
    assert exc.details["zone"] == "Europe/London"
    assert exc.details["gap"] == {"start": "2026-03-29T01:00:00", "end": "2026-03-29T02:00:00"}
    assert "remedy" in exc.details


def test_the_first_and_last_second_of_the_gap_are_nonexistent_but_the_bounds_are_valid():
    assert timezones.classify_local(datetime(2026, 3, 29, 0, 59, 59), "Europe/London").status == "valid"
    assert timezones.classify_local(datetime(2026, 3, 29, 1, 0, 0), "Europe/London").status == "nonexistent"
    assert timezones.classify_local(datetime(2026, 3, 29, 1, 59, 59), "Europe/London").status == "nonexistent"
    assert timezones.classify_local(datetime(2026, 3, 29, 2, 0, 0), "Europe/London").status == "valid"


def test_a_valid_local_time_reports_one_instant_and_no_candidates():
    validity = timezones.classify_local(datetime(2026, 10, 27, 9, 0), "Europe/London")
    assert validity.status == "valid"
    assert [c[1] for c in validity.candidates] == [aware(2026, 10, 27, 9, 0)]
    assert validity.candidates[0][0] == 0


# ── an offset is an instant ─────────────────────────────────────────


def test_an_explicit_instant_inside_the_gap_is_accepted_and_preserved():
    frame = timeparse.classify("2026-03-29T01:30:00+00:00", endpoint_zone="Europe/London")
    assert frame.instant == aware(2026, 3, 29, 1, 30)
    assert frame.wire_value == "2026-03-29T01:30:00Z"
    # its London rendering is 02:30+01:00 — the instant is not a nonexistent reading
    assert frame.to_object()["interpreted_instant"]["local"] == "2026-03-29T02:30:00"
    assert frame.to_object()["interpreted_instant"]["utc_offset_seconds"] == 3600


def test_an_explicit_instant_inside_the_repeated_hour_is_accepted():
    first = timeparse.classify("2026-10-25T01:30:00+01:00")
    second = timeparse.classify("2026-10-25T01:30:00+00:00")
    assert first.instant == aware(2026, 10, 25, 0, 30)
    assert second.instant == aware(2026, 10, 25, 1, 30)
    assert first.instant != second.instant          # the offset selects the occurrence


# ── southern hemisphere ─────────────────────────────────────────────


def test_southern_hemisphere_autumn_fold_is_ambiguous():
    validity = timezones.classify_local(datetime(2026, 4, 5, 2, 30), "Australia/Sydney")
    assert validity.status == "ambiguous"
    assert {c[0] for c in validity.candidates} == {36000, 39600}   # +10:00 / +11:00
    with pytest.raises(errors.ToolError) as caught:
        timeparse.classify("2026-04-05T02:30", endpoint_zone="Australia/Sydney")
    assert caught.value.reason == errors.AMBIGUOUS_LOCAL_TIME


def test_southern_hemisphere_spring_gap_is_nonexistent():
    validity = timezones.classify_local(datetime(2026, 10, 4, 2, 30), "Australia/Sydney")
    assert validity.status == "nonexistent"
    assert validity.gap_start == datetime(2026, 10, 4, 2, 0)
    assert validity.gap_end == datetime(2026, 10, 4, 3, 0)
    with pytest.raises(errors.ToolError) as caught:
        timeparse.classify("2026-10-04T02:30", endpoint_zone="Australia/Sydney")
    assert caught.value.details["gap"]["start"] == "2026-10-04T02:00:00"


def test_a_zone_without_dst_never_reports_a_second_candidate():
    for naive in (datetime(2026, 3, 29, 1, 30), datetime(2026, 10, 25, 1, 30)):
        validity = timezones.classify_local(naive, "Africa/Lagos")
        assert validity.status == "valid"
        assert len(validity.candidates) == 1


# ── no request is issued for a pre-request rejection ────────────────


def test_an_ambiguous_clock_is_rejected_without_issuing_a_request():
    calls = []

    def tripwire(request, timeout):
        calls.append(request)
        raise AssertionError("no request may be issued for a pre-request rejection")

    http.set_transport(tripwire)
    try:
        with pytest.raises(errors.ToolError) as caught:
            timeparse.classify("2026-10-25T01:30", endpoint_zone="Europe/London")
    finally:
        http.reset_transport()
    assert caught.value.reason == errors.AMBIGUOUS_LOCAL_TIME
    assert calls == []


def test_a_nonexistent_clock_is_rejected_without_issuing_a_request():
    calls = []
    http.set_transport(lambda request, timeout: calls.append(request))
    try:
        with pytest.raises(errors.ToolError):
            timeparse.classify("2026-03-29T01:30", endpoint_zone="Europe/London")
    finally:
        http.reset_transport()
    assert calls == []


# ── the recorded offset capture agrees with the model ───────────────


def test_to_instant_returns_the_single_valid_instant_and_refuses_a_fold():
    assert timezones.to_instant(datetime(2026, 10, 27, 9, 0), "Europe/London") == aware(
        2026, 10, 27, 9, 0
    )
    with pytest.raises(errors.ToolError) as caught:
        timezones.to_instant(datetime(2026, 10, 25, 1, 30), "Europe/London")
    assert caught.value.reason == errors.AMBIGUOUS_LOCAL_TIME
    with pytest.raises(errors.ToolError) as caught:
        timezones.to_instant(datetime(2026, 3, 29, 1, 30), "Europe/London")
    assert caught.value.reason == errors.NONEXISTENT_LOCAL_TIME


def test_payload_timestamp_parsing_requires_an_offset_unless_one_is_named():
    assert timezones.parse_rfc3339("2026-10-27T09:00:00Z") == aware(2026, 10, 27, 9, 0)
    assert timezones.parse_rfc3339("2026-10-27T09:00:00+01:00") == aware(2026, 10, 27, 8, 0)
    assert timezones.parse_rfc3339("2026-10-27T09:00:00+0100") == aware(2026, 10, 27, 8, 0)
    assert timezones.parse_rfc3339("2026-10-27T09:00:00", default_zone="Europe/London") == aware(
        2026, 10, 27, 9, 0
    )
    for bad in ("2026-10-27T09:00:00", "not a time", "2026-10-27T09:00:00+24:00", 5):
        with pytest.raises(ValueError):
            timezones.parse_rfc3339(bad)


def test_iso_z_is_the_unambiguous_rendering_of_any_instant():
    assert timezones.iso_z(aware(2026, 7, 15, 8, 0)) == "2026-07-15T08:00:00Z"
    assert timezones.iso_z(aware(2026, 7, 15, 9, 0, offset_hours=1)) == "2026-07-15T08:00:00Z"


def test_the_recorded_offset_capture_is_the_instant_the_model_computes():
    frame = timeparse.classify(
        "2026-10-27T09:00:00+01:00", endpoint_zone="Europe/London"
    )
    assert frame.wire_value == "2026-10-27T08:00:00Z"
    # the captured plan for that request started at 08:03Z, i.e. inside the instant's morning
    plan = fs.load("plan_glw_edi_offset.json")
    assert plan["itineraries"][0]["startTime"].startswith("2026-10-27T08:")
