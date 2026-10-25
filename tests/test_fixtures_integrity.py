"""Fixture provision and provenance gates (§8, §8.1, §8.3, §8.4).

The provenance README is the manifest: one row per file, each row carrying the
storage hash, the source and the value the suite asserts. These tests prove the
manifest holds, that the loader cannot read outside the configured scratch corpus, that the
gzip rule was applied by recorded size, and that the checker itself is not
vacuous — the negative cases below construct a deliberately broken fixture
directory in ``tmp_path`` and require a problem for each failure mode.
"""

import gzip
import json
import os
import subprocess
import sys

import pytest

import fixture_support as fs


def test_manifest_has_no_integrity_problem():
    assert fs.verify_manifest() == []


def test_manifest_covers_every_fixture_and_no_phantom():
    rows = fs.load_manifest()
    names = {row["fixture"] for row in rows}
    assert names == set(fs.fixture_names())
    assert len(rows) == len(fs.fixture_names())


def test_manifest_constraints_agree_with_every_recorded_url():
    """T1-04: the `constraints` cell never contradicts the row's own request URL.

    Named separately from the general integrity gate so a drift in this column is
    legible: the URL is the recorded request, and a row that documents a different
    ``maxTransfers``/``numItineraries`` than it ran is a false provenance claim
    even though the body hashes still hold.
    """
    assert fs.check_constraints(fs.load_manifest()) == []


def test_full_approved_inventory_is_provisioned():
    """The §8.1 observed inventory plus every §8.3 synthetic input."""
    expected_observed = {
        "plan_ayr_ncl_coord.json",
        "plan_ayr_ncl_stopids_n3.json",
        "plan_ayr_man_stopids_n3.json",
        "plan_ayr_man_stopids_n8.json.gz",
        "plan_ayr_ncl_arriveby.json",
        "plan_ncl_ayr_depart.json",
        "plan_glw_edi_coord.json",
        "plan_glw_edi_stopids.json",
        "plan_glw_edi_offset.json",
        "plan_glw_edi_naive_reject.json",
        "plan_glw_ncl_direct.json",
        "plan_glw_ncl_walk60.json",
        "plan_ams_ber.json",
        "plan_ber_muc.json",
        "plan_empty_no_service.json",
        "plan_empty_constraints.json",
        "window_error.json",
        "stop_metro_central.json",
        "stoptimes_ayr_p3.json",
        "stoptimes_glc_p11.json",
        "stoptimes_ber_hbf.json.gz",
        "geocode_newcastle_central_station.json",
        "geocode_manchester_piccadilly_station.json",
        "geocode_ayr.json",
        "geocode_ayr_station.json",
        "geocode_newcastle_stop_rail.json",
        "geocode_man_picc_rrail.json",
    }
    expected_synthetic = {
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
    observed = {r["fixture"] for r in fs.load_manifest() if r["kind"] == "observed"}
    synthetic = {r["fixture"] for r in fs.load_manifest() if r["kind"] == "synthetic"}
    assert observed == expected_observed
    assert synthetic == expected_synthetic


def test_loader_opens_gzipped_and_plain_bodies_identically():
    from_gz = fs.load("stoptimes_ber_hbf.json.gz")
    assert isinstance(from_gz, dict) and len(from_gz["stopTimes"]) == 200
    plain = fs.load("stop_metro_central.json")
    assert plain["place"]["tz"] == "Europe/London"
    # gzip is storage only: the loader exposes the uncompressed logical bytes
    logical = fs.read_bytes("stoptimes_ber_hbf.json.gz")
    assert logical.lstrip().startswith(b"{")
    with open(fs.fixture_path("stoptimes_ber_hbf.json.gz"), "rb") as fh:
        assert fh.read(2) == b"\x1f\x8b"          # on disk it really is gzip
    assert json.loads(logical) == from_gz


def test_gzip_storage_rule_follows_recorded_size():
    for name in fs.fixture_names():
        logical = fs.read_bytes(name)
        if len(logical) > fs.GZIP_THRESHOLD_BYTES:
            assert name.endswith(".json.gz"), name
        else:
            assert not name.endswith(".json.gz"), name


def test_loader_returns_a_fresh_object_each_call():
    first = fs.load("stop_metro_central.json")
    first["place"]["name"] = "mutated"
    second = fs.load("stop_metro_central.json")
    assert second["place"]["name"] == "Central Station"


def test_loader_cannot_resolve_paths_outside_the_fixture_directory():
    with pytest.raises(FileNotFoundError):
        fs.fixture_path("../server.py")
    with pytest.raises(FileNotFoundError):
        fs.fixture_path("/etc/passwd")


def test_tests_tree_contains_no_token_shaped_string():
    assert fs.scan_for_tokens() == []


def test_derived_window_fixture_preserves_both_verbatim_bodies():
    data = fs.load("window_error.json")
    assert len(data["entries"]) == 2
    queries = set()
    for entry in data["entries"]:
        assert entry["status"] == 400
        assert "outside of loaded timetable window" in entry["body"]
        recorded = json.loads(entry["recorded_mcp_text"])
        assert recorded["error"] == "HTTP 400: " + entry["body"]
        message = json.loads(entry["body"])["error"]
        assert message.startswith("query time 2026-07-15 ")
        queries.add(message.split()[3])
    # the offset-bearing query is echoed by the upstream in UTC: 09:00+01:00 = 08:00
    assert queries == {"08:00", "09:00"}


def test_parent_id_is_exercised_from_a_body_that_carries_it():
    stoptimes = fs.load("stoptimes_ayr_p3.json")
    assert stoptimes["place"]["parentId"]
    assert stoptimes["stopTimes"][0]["place"]["parentId"] == stoptimes["place"]["parentId"]
    # and the stop-metadata body genuinely lacks it, which is what its row asserts
    assert "parentId" not in fs.load("stop_metro_central.json")["place"]


# ── non-vacuity: the checker must reject a tampered directory ───────


@pytest.fixture()
def broken_fixtures(tmp_path):
    """A two-file fixture directory that the integrity checker must police."""
    def build(files, rows):
        root = tmp_path / "fixtures"
        root.mkdir()
        for name, blob in files.items():
            (root / name).write_bytes(blob)
        header = (
            "| fixture | kind | source | source_sha256 | captured_utc | query_direction | "
            "constraints | asserts | content_sha256 |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        )
        lines = [header] + [
            "| `{fixture}` | {kind} | {source} | `{source_sha}` | {captured} | {query} | "
            "{constraints} | {asserts} | `{content_sha}` |\n".format(**row)
            for row in rows
        ]
        (root / "README.md").write_text("".join(lines), encoding="utf-8")
        return str(root)

    def row(**overrides):
        base = dict(
            fixture="a.json",
            kind="observed",
            source="pkg/a.body.json",
            source_sha=fs.sha256_bytes(b"{}"),
            captured="2026-10-21T09:00:00Z",
            query="GET /api/v6/plan (depart_after)",
            constraints="none",
            asserts="nothing",
            content_sha=fs.sha256_bytes(b"{}"),
        )
        base.update(overrides)
        return base

    return build, row


def test_checker_rejects_a_tampered_body(broken_fixtures):
    build, row = broken_fixtures
    root = build({"a.json": b'{"tampered": true}'}, [row()])
    problems = fs.verify_manifest(root)
    assert any("content_sha256" in p for p in problems), problems


def test_checker_rejects_a_missing_row(broken_fixtures):
    build, row = broken_fixtures
    root = build({"a.json": b"{}", "b.json": b"{}"}, [row()])
    problems = fs.verify_manifest(root)
    assert any("unlisted fixture file: b.json" in p for p in problems), problems


def test_checker_rejects_a_row_without_a_file(broken_fixtures):
    build, row = broken_fixtures
    root = build({}, [row()])
    assert any("without a file" in p for p in fs.verify_manifest(root))


def test_checker_rejects_a_mislabelled_synthetic(broken_fixtures):
    build, row = broken_fixtures
    root = build(
        {"synthetic_a.json": b"{}"},
        [
            row(
                fixture="synthetic_a.json",
                kind="observed",
                content_sha=fs.sha256_bytes(b"{}"),
            )
        ],
    )
    assert any("synthetic_ prefix" in p for p in fs.verify_manifest(root))


def test_checker_rejects_an_ungzipped_large_body(broken_fixtures):
    build, row = broken_fixtures
    big = b"{}" + b" " * (fs.GZIP_THRESHOLD_BYTES + 1)
    root = build({"a.json": big}, [row(content_sha=fs.sha256_bytes(big), source_sha=fs.sha256_bytes(big))])
    assert any("exceeds 300 KB" in p for p in fs.verify_manifest(root))


def test_checker_rejects_an_observed_body_that_diverges_from_its_source(broken_fixtures):
    build, row = broken_fixtures
    root = build({"a.json": b"{}"}, [row(source_sha=fs.sha256_bytes(b'{"different": 1}'))])
    assert any("byte-identical" in p for p in fs.verify_manifest(root))


def test_checker_rejects_a_truncated_manifest_row(broken_fixtures):
    build, row = broken_fixtures
    root = build({"a.json": b"{}"}, [row()])
    with open(os.path.join(root, "README.md"), "a", encoding="utf-8") as fh:
        fh.write("| `b.json` | observed | pkg/b.body.json |\n")
    with pytest.raises(AssertionError, match="cells"):
        fs.verify_manifest(root)


#: A recorded request URL and the constraint cell that agrees with it. §8.4: the
#: row's own URL is the recorded request, so the `constraints` column may neither
#: contradict it (T1-04) nor silently drop a constraint the URL carries.
RECORDED_URL = (
    "https://api.transitous.org/api/v6/plan?fromPlace=a&toPlace=b"
    "&numItineraries=3&maxTransfers=0&transitModes=RAIL&searchWindow=10800 (depart_after)"
)
AGREEING_CONSTRAINTS = "numItineraries=3, maxTransfers=0, transitModes=RAIL, searchWindow=10800"


def test_checker_rejects_constraints_that_contradict_the_recorded_url(broken_fixtures):
    build, row = broken_fixtures
    root = build(
        {"a.json": b"{}"},
        [row(query=RECORDED_URL, constraints="numItineraries=3, maxTransfers=5, transitModes=RAIL, searchWindow=10800")],
    )
    problems = fs.verify_manifest(root)
    assert any("maxTransfers" in p and "5" in p for p in problems), problems


def test_checker_rejects_a_recorded_constraint_the_row_omits(broken_fixtures):
    build, row = broken_fixtures
    root = build(
        {"a.json": b"{}"},
        [row(query=RECORDED_URL, constraints="numItineraries=3, maxTransfers=0, transitModes=RAIL")],
    )
    problems = fs.verify_manifest(root)
    assert any("searchWindow" in p for p in problems), problems


def test_checker_accepts_constraints_that_agree_with_the_recorded_url(broken_fixtures):
    build, row = broken_fixtures
    root = build({"a.json": b"{}"}, [row(query=RECORDED_URL, constraints=AGREEING_CONSTRAINTS)])
    assert fs.verify_manifest(root) == []


def test_checker_leaves_a_row_without_a_recorded_url_alone(broken_fixtures):
    """A derived or constructed body has no single request to compare against."""
    build, row = broken_fixtures
    root = build({"a.json": b"{}"}, [row(query="constructed plan response; see the note",
                                        constraints="numItineraries=2 / 1, transitModes=RAIL")])
    assert fs.verify_manifest(root) == []


def test_scanner_detects_a_planted_token(tmp_path):
    planted = tmp_path / "tests"
    planted.mkdir()
    # assembled at runtime so this file never contains a token-shaped literal
    header = "".join(["Authori", "zation: ", "Bear", "er abcdefghijklmnop"])
    (planted / "leak.json").write_text(json.dumps({"header": header}), encoding="utf-8")
    hits = fs.scan_for_tokens(str(planted))
    assert hits, "the token scanner must catch a planted credential"


def test_capture_corpus_is_explicitly_configured_outside_the_repository():
    configured = os.path.realpath(os.path.expanduser(os.environ["TRANSITOUS_TEST_FIXTURES"]))
    repo = os.path.realpath(fs.REPO_ROOT)
    assert fs.FIXTURES_DIR == configured
    assert os.path.commonpath([configured, repo]) != repo
    assert not os.path.exists(os.path.join(repo, "tests", "fixtures"))


# ── a missing scratch corpus must say so (§8.1, amendment 02) ───────


def _import_support_without_corpus(**overrides):
    """Import the loader in a child process with the corpus variable controlled.

    The parent suite is itself running under a configured corpus, so the only way
    to observe the unconfigured path is a child interpreter. Bytecode writing is
    off so the probe leaves nothing behind in the checkout.
    """
    env = {key: value for key, value in os.environ.items() if key != "TRANSITOUS_TEST_FIXTURES"}
    env.update(overrides)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", "import fixture_support"],
        cwd=os.path.dirname(os.path.abspath(fs.__file__)),
        env=env,
        capture_output=True,
        text=True,
    )


def test_an_unset_corpus_variable_fails_loudly_instead_of_skipping():
    result = _import_support_without_corpus()
    assert result.returncode != 0, "a missing corpus must never look like a clean import"
    assert "TRANSITOUS_TEST_FIXTURES" in result.stderr
    assert "scratch corpus" in result.stderr


def test_a_corpus_path_that_does_not_exist_fails_loudly(tmp_path):
    absent = tmp_path / "no-such-corpus"
    result = _import_support_without_corpus(TRANSITOUS_TEST_FIXTURES=str(absent))
    assert result.returncode != 0
    assert "TRANSITOUS_TEST_FIXTURES" in result.stderr
    assert not absent.exists()
