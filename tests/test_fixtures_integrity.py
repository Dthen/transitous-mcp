"""Fixture integrity and isolation gates (§8, §8.1, §8.3, §8.4).

The inputs are checked-in synthetic bodies (``tests/fixtures``), so the gates
here police a different risk from the capture-era suite: not "did we copy the
recording faithfully", but "is every body honestly labelled synthetic, is it the
file the manifest describes, and can the loader read anything outside the
fixtures directory". The checker's own non-vacuity is asserted by building
deliberately broken directories in ``tmp_path`` and requiring one problem per
failure mode.
"""

import json
import os
import subprocess
import sys

import pytest

import fixture_support as fs

from transit_core import http, timezones


def test_manifest_has_no_integrity_problem():
    assert fs.verify_manifest() == []


def test_manifest_covers_every_fixture_and_no_phantom():
    rows = fs.load_manifest()
    names = {row["fixture"] for row in rows}
    assert names == set(fs.fixture_names())
    assert len(rows) == len(fs.fixture_names())


def test_every_fixture_is_labelled_synthetic_and_prefixed():
    rows = fs.load_manifest()
    assert rows
    for row in rows:
        assert row["kind"] == "synthetic", row
        assert row["fixture"].startswith("synthetic_"), row
        assert row["purpose"].strip(), row


def test_the_full_inventory_is_provisioned():
    """Every body the suite reads is present and listed, and nothing else is."""
    expected = {
        "synthetic_plan_ayr_ncl_coord.json",
        "synthetic_plan_ayr_ncl_stopids_n3.json",
        "synthetic_plan_ayr_man_stopids_n3.json",
        "synthetic_plan_ayr_man_stopids_n8.json",
        "synthetic_plan_ayr_ncl_arriveby.json",
        "synthetic_plan_ncl_ayr_depart.json",
        "synthetic_plan_glw_edi_coord.json",
        "synthetic_plan_glw_edi_stopids.json",
        "synthetic_plan_glw_edi_offset.json",
        "synthetic_plan_glw_edi_naive_reject.json",
        "synthetic_plan_glw_ncl_direct.json",
        "synthetic_plan_glw_ncl_walk60.json",
        "synthetic_plan_ams_ber.json",
        "synthetic_plan_ber_muc.json",
        "synthetic_plan_empty_no_service.json",
        "synthetic_plan_empty_constraints.json",
        "synthetic_window_error.json",
        "synthetic_stop_metro_central.json",
        "synthetic_stoptimes_ayr_p3.json",
        "synthetic_stoptimes_glc_p11.json",
        "synthetic_stoptimes_ber_hbf.json",
        "synthetic_geocode_newcastle_central_station.json",
        "synthetic_geocode_manchester_piccadilly_station.json",
        "synthetic_geocode_ayr.json",
        "synthetic_geocode_ayr_station.json",
        "synthetic_geocode_newcastle_stop_rail.json",
        "synthetic_geocode_man_picc_rrail.json",
        "synthetic_plan_direct_alternatives.json",
        "synthetic_plan_cancelled_transit_leg.json",
        "synthetic_trip_intermediate_stops.json",
        "synthetic_trip_cancelled_stoptime.json",
        "synthetic_return_next_morning.json",
        "synthetic_westbound_date_line.json",
        "synthetic_continuation_pages.json",
        "synthetic_map_trips_window.json",
        "synthetic_reachable_pareto.json",
        "synthetic_one_to_many_street.json",
        "synthetic_one_to_many_transit.json",
        "synthetic_naive_arriveby.json",
    }
    assert set(fs.fixture_names()) == expected
    assert {row["fixture"] for row in fs.load_manifest()} == expected


def test_loader_returns_a_fresh_object_each_call():
    first = fs.load("synthetic_stop_metro_central.json")
    first["place"]["name"] = "mutated"
    second = fs.load("synthetic_stop_metro_central.json")
    assert second["place"]["name"] == "Central Station"


def test_loader_cannot_resolve_paths_outside_the_fixture_directory():
    with pytest.raises(FileNotFoundError):
        fs.fixture_path("../server.py")
    with pytest.raises(FileNotFoundError):
        fs.fixture_path("/etc/passwd")
    with pytest.raises(FileNotFoundError):
        fs.fixture_path("no-such-fixture.json")


def test_a_missing_required_fixture_fails_loudly_with_its_checked_in_path():
    with pytest.raises(FileNotFoundError) as caught:
        fs.load("synthetic_absent_body.json")
    assert "synthetic_absent_body.json" in str(caught.value)
    assert fs.FIXTURES_DIR in str(caught.value)


def test_fixtures_live_inside_the_checkout_and_need_no_environment():
    repo = os.path.realpath(fs.REPO_ROOT)
    fixtures = os.path.realpath(fs.FIXTURES_DIR)
    assert os.path.commonpath([fixtures, repo]) == repo
    assert fixtures == os.path.join(repo, "tests", "fixtures")
    # The loader does not consult TRANSITOUS_TEST_FIXTURES; a stale value in the
    # environment is inert rather than an alternate corpus.
    env = dict(os.environ, TRANSITOUS_TEST_FIXTURES="/nonexistent/old-corpus")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import fixture_support, json; print(json.dumps([fixture_support.FIXTURES_DIR, len(fixture_support.fixture_names())]))",
        ],
        cwd=os.path.dirname(os.path.abspath(fs.__file__)),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    directory, count = json.loads(result.stdout)
    assert os.path.realpath(directory) == fixtures
    assert count == len(fs.fixture_names())


def test_tests_tree_contains_no_token_shaped_string():
    assert fs.scan_for_tokens() == []


def test_window_fixture_carries_both_clock_forms():
    """The two out-of-window bodies differ only in the echoed clock."""
    data = fs.load("synthetic_window_error.json")
    assert len(data["entries"]) == 2
    queries = set()
    for entry in data["entries"]:
        assert entry["status"] == 400
        assert "outside of loaded timetable window" in entry["body"]
        message = json.loads(entry["body"])["error"]
        assert message.startswith("query time 2026-07-15 ")
        queries.add(message.split()[3])
        assert http.parse_timetable_window(entry["body"]) == {
            "start": "2026-09-20 00:00",
            "end": "2027-10-21 00:00",
        }
    # the offset-bearing query was rendered by the upstream in UTC: 09:00+01:00 = 08:00
    assert queries == {"08:00", "09:00"}


def test_parent_id_is_exercised_from_a_body_that_carries_it():
    stoptimes = fs.load("synthetic_stoptimes_ayr_p3.json")
    assert stoptimes["place"]["parentId"]
    assert stoptimes["stopTimes"][0]["place"]["parentId"] == stoptimes["place"]["parentId"]
    # and the stop-metadata body genuinely lacks it, which is what its row asserts
    assert "parentId" not in fs.load("synthetic_stop_metro_central.json")["place"]


# ── non-vacuity: the checker must reject a tampered directory ───────


@pytest.fixture()
def broken_fixtures(tmp_path):
    """A one-or-two-file fixture directory that the checker must police."""

    def build(files, rows):
        root = tmp_path / "fixtures"
        root.mkdir()
        for name, blob in files.items():
            (root / name).write_bytes(blob)
        header = "| fixture | kind | content_sha256 | purpose |\n| --- | --- | --- | --- |\n"
        lines = [header] + [
            "| `{fixture}` | {kind} | `{content_sha}` | {purpose} |\n".format(**row)
            for row in rows
        ]
        (root / "README.md").write_text("".join(lines), encoding="utf-8")
        return str(root)

    def row(**overrides):
        base = dict(
            fixture="synthetic_a.json",
            kind="synthetic",
            content_sha=fs.sha256_bytes(b"{}"),
            purpose="a constructed example",
        )
        base.update(overrides)
        return base

    return build, row


def test_checker_rejects_a_tampered_body(broken_fixtures):
    build, row = broken_fixtures
    root = build({"synthetic_a.json": b'{"tampered": true}'}, [row()])
    problems = fs.verify_manifest(root)
    assert any("content_sha256" in p for p in problems), problems


def test_checker_rejects_a_missing_row(broken_fixtures):
    build, row = broken_fixtures
    root = build({"synthetic_a.json": b"{}", "synthetic_b.json": b"{}"}, [row()])
    problems = fs.verify_manifest(root)
    assert any("unlisted fixture file: synthetic_b.json" in p for p in problems), problems


def test_checker_rejects_a_row_without_a_file(broken_fixtures):
    build, row = broken_fixtures
    root = build({}, [row()])
    assert any("without a file" in p for p in fs.verify_manifest(root))


def test_checker_rejects_an_observed_label(broken_fixtures):
    build, row = broken_fixtures
    root = build({"synthetic_a.json": b"{}"}, [row(kind="observed")])
    assert any("synthetic" in p and "kind" in p for p in fs.verify_manifest(root))


def test_checker_rejects_a_fixture_without_the_synthetic_prefix(broken_fixtures):
    build, row = broken_fixtures
    root = build({"a.json": b"{}"}, [row(fixture="a.json")])
    assert any("prefix" in p for p in fs.verify_manifest(root))


def test_checker_rejects_a_body_that_claims_real_provenance(broken_fixtures):
    build, row = broken_fixtures
    blob = b'{"note": "captured from the live service"}'
    root = build({"synthetic_a.json": blob}, [row(content_sha=fs.sha256_bytes(blob))])
    assert any("provenance claim" in p for p in fs.verify_manifest(root))


def test_checker_rejects_an_empty_purpose(broken_fixtures):
    build, row = broken_fixtures
    root = build({"synthetic_a.json": b"{}"}, [row(purpose="")])
    assert any("empty purpose" in p for p in fs.verify_manifest(root))


def test_checker_rejects_a_truncated_manifest_row(broken_fixtures):
    build, row = broken_fixtures
    root = build({"synthetic_a.json": b"{}"}, [row()])
    with open(os.path.join(root, "README.md"), "a", encoding="utf-8") as fh:
        fh.write("| `synthetic_b.json` | synthetic |\n")
    with pytest.raises(AssertionError, match="cells"):
        fs.verify_manifest(root)


def test_scanner_detects_a_planted_token(tmp_path):
    planted = tmp_path / "tests"
    planted.mkdir()
    # assembled at runtime so this file never contains a token-shaped literal
    header = "".join(["Authori", "zation: ", "Bear", "er abcdefghijklmnop"])
    (planted / "synthetic_leak.json").write_text(json.dumps({"header": header}), encoding="utf-8")
    hits = fs.scan_for_tokens(str(planted))
    assert hits, "the token scanner must catch a planted credential"


# ── constructed plans are internally coherent examples ──────────────
#
# These bodies stand in for real responses, so they must themselves be
# believable: a declared leg duration that contradicts its instants, a leg that
# starts before the previous one ended, or a leg that departs from somewhere
# other than where the previous leg arrived would make the suite assert against
# an example no correct upstream could produce. This is a check on the test
# data, not a new validation rule for production code.
#
# Excluded on purpose: ``synthetic_plan_glw_edi_naive_reject`` and
# ``synthetic_window_error`` are error bodies, not plans; the empty-plan
# fixtures carry no alternatives; board/geocode/one-to-many fixtures are not
# plans. The street-only ``direct`` alternative is included because it is a
# complete itinerary.

CONSISTENT_PLAN_BODIES = (
    "synthetic_plan_ams_ber.json",
    "synthetic_plan_ayr_man_stopids_n3.json",
    "synthetic_plan_ayr_man_stopids_n8.json",
    "synthetic_plan_ayr_ncl_arriveby.json",
    "synthetic_plan_ayr_ncl_coord.json",
    "synthetic_plan_ayr_ncl_stopids_n3.json",
    "synthetic_plan_ber_muc.json",
    "synthetic_plan_cancelled_transit_leg.json",
    "synthetic_plan_direct_alternatives.json",
    "synthetic_plan_glw_edi_coord.json",
    "synthetic_plan_glw_edi_offset.json",
    "synthetic_plan_glw_edi_stopids.json",
    "synthetic_plan_glw_ncl_direct.json",
    "synthetic_plan_glw_ncl_walk60.json",
    "synthetic_naive_arriveby.json",
    "synthetic_plan_ncl_ayr_depart.json",
    "synthetic_return_next_morning.json",
    "synthetic_westbound_date_line.json",
)


def _instant(value):
    return timezones.parse_rfc3339(value)


def _leg_start(leg):
    value = leg.get("startTime") or leg["from"].get("departure")
    assert value, f"leg {leg.get('mode')} has no start instant"
    return _instant(value)


def _leg_end(leg):
    value = leg.get("endTime") or leg["to"].get("arrival")
    assert value, f"leg {leg.get('mode')} has no end instant"
    return _instant(value)


def assert_alternative_is_coherent(alternative, label):
    """One itinerary/direct object: bounds, durations, order and place chaining."""
    start = _instant(alternative["startTime"])
    end = _instant(alternative["endTime"])
    legs = alternative["legs"]
    assert legs, f"{label}: no legs"
    assert alternative["duration"] == int((end - start).total_seconds()), (
        f"{label}: itinerary duration contradicts its instants"
    )
    assert _leg_start(legs[0]) == start, f"{label}: first leg does not start the itinerary"
    assert _leg_end(legs[-1]) == end, f"{label}: last leg does not end the itinerary"

    previous_end = None
    previous_to = None
    for index, leg in enumerate(legs):
        leg_start = _leg_start(leg)
        leg_end = _leg_end(leg)
        assert leg_end >= leg_start, f"{label} leg {index}: ends before it starts"
        assert leg["duration"] == int((leg_end - leg_start).total_seconds()), (
            f"{label} leg {index}: declared duration contradicts its instants"
        )
        if previous_end is not None:
            assert leg_start >= previous_end, f"{label} leg {index}: overlaps the previous leg"
            assert_places_continue(previous_to, leg["from"], f"{label} leg {index}")
        previous_end, previous_to = leg_end, leg["to"]


def assert_places_continue(previous_to, leg_from, label):
    """A leg departs from where the previous leg arrived.

    Identity is the stop id where both places carry one (platform-level bodies
    may render a different coordinate for the same stop); otherwise name and
    coordinates must match exactly.
    """
    previous_stop = previous_to.get("stopId")
    current_stop = leg_from.get("stopId")
    if isinstance(previous_stop, str) and previous_stop and isinstance(current_stop, str) and current_stop:
        assert previous_stop == current_stop, (
            f"{label}: departs from {current_stop!r} but the previous leg arrived at {previous_stop!r}"
        )
        return
    assert (previous_to.get("name"), previous_to.get("lat"), previous_to.get("lon")) == (
        leg_from.get("name"),
        leg_from.get("lat"),
        leg_from.get("lon"),
    ), f"{label}: departs from {leg_from.get('name')!r} but the previous leg arrived at {previous_to.get('name')!r}"


@pytest.mark.parametrize("name", CONSISTENT_PLAN_BODIES)
def test_constructed_plan_body_is_internally_coherent(name):
    body = fs.load(name)
    alternatives = list(body.get("itineraries") or []) + list(body.get("direct") or [])
    assert alternatives, f"{name}: expected at least one alternative"
    for position, alternative in enumerate(alternatives, start=1):
        assert_alternative_is_coherent(alternative, f"{name} alternative {position}")


def test_constructed_continuation_pages_are_internally_coherent():
    pages = fs.load("synthetic_continuation_pages.json")
    for page_name, body in pages.items():
        for position, itinerary in enumerate(body["itineraries"], start=1):
            assert_alternative_is_coherent(itinerary, f"continuation {page_name} itinerary {position}")
