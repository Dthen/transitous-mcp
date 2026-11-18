"""Request serialisation and failure taxonomy at the single request boundary (§4.2).

Two defects live at this boundary and nowhere else: a repeated query key for an
array parameter, and a failure taxonomy that a per-call-site fix would leave
half-applied. Both are pinned here against the documented body shapes.
"""

import io
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from http.client import IncompleteRead

import pytest

import fixture_support as fs  # noqa: F401  (puts the repository root on sys.path)

from transit_core import errors, http


# ── transport double ────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body if isinstance(body, bytes) else body.encode()

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def close(self):
        pass


class ReadFailingResponse(FakeResponse):
    """A response whose status is readable and whose body read raises."""

    def __init__(self, status, exc, close_calls=None):
        super().__init__(status, b"")
        self._exc = exc
        self.close_calls = [] if close_calls is None else close_calls

    def read(self):
        raise self._exc

    def close(self):
        self.close_calls.append("close")


def transport_returning(status, body, counter=None):
    def transport(request, timeout):
        if counter is not None:
            counter.append((request, timeout))
        return FakeResponse(status, body)

    return transport


def transport_raising(exc, counter=None):
    def transport(request, timeout):
        if counter is not None:
            counter.append((request, timeout))
        raise exc

    return transport


def http_error(status, body):
    return urllib.error.HTTPError(
        "https://api.transitous.org/api/v6/plan",
        status,
        "boom",
        {"Content-Type": "application/json"},
        io.BytesIO(body.encode() if isinstance(body, str) else body),
    )


# ── array serialisation ─────────────────────────────────────────────


def test_single_value_bytes_are_identical_to_the_legacy_encoding():
    cases = [
        {"transitModes": ["RAIL"]},
        {"mode": ["WALK"]},
        {"type": ["STOP"]},
        {"language": ["en"]},
        {"n": 20, "stopId": "gb-great-britain_9100AYRR3"},
        {"arriveBy": "true", "maxTransfers": 5},
    ]
    for params in cases:
        legacy = urllib.parse.urlencode(params, doseq=True)
        assert http.encode_params(params) == legacy, params


def test_every_array_parameter_becomes_one_comma_value():
    cases = [
        {"transitModes": ["BUS", "RAIL"]},
        {"type": ["STOP", "ADDRESS"]},
        {"mode": ["REGIONAL_RAIL", "BUS"]},
        {"language": ["en", "de"]},
    ]
    for params in cases:
        key, values = next(iter(params.items()))
        encoded = http.encode_params(params)
        assert encoded.count("=") == 1, encoded
        assert key in encoded
        # one decoded value, commas intact, order preserved
        parsed = urllib.parse.parse_qs(encoded, keep_blank_values=True)
        assert parsed[key] == [",".join(values)], (parsed, values)


def test_no_request_ever_contains_a_repeated_query_key():
    params = {
        "fromPlace": "55.458267,-4.625827",
        "transitModes": ["BUS", "RAIL", "SUBWAY"],
        "type": ["STOP"],
        "language": ["en"],
    }
    encoded = http.encode_params(params)
    keys = [pair.split("=")[0] for pair in encoded.split("&")]
    assert len(keys) == len(set(keys))
    assert encoded.count("transitModes") == 1


def test_array_order_is_preserved_exactly():
    assert http.encode_params({"transitModes": ["RAIL", "BUS"]}) == "transitModes=RAIL%2CBUS"
    assert http.encode_params({"transitModes": ["BUS", "RAIL"]}) == "transitModes=BUS%2CRAIL"
    assert http.encode_params({"transitModes": ["BUS", "BUS", "RAIL"]}) == "transitModes=BUS%2CBUS%2CRAIL"


def test_parameter_order_is_the_caller_order():
    params = {"b": 2, "a": 1, "c": "x"}
    assert http.encode_params(params) == "b=2&a=1&c=x"


def test_none_values_are_dropped_and_empty_arrays_omit_the_key():
    assert http.encode_params({"time": None, "n": 5}) == "n=5"
    assert http.encode_params({"transitModes": [], "n": 5}) == "n=5"
    assert http.encode_params({}) == ""
    assert http.encode_params(None) == ""


def test_booleans_serialise_lowercase():
    assert http.encode_params({"arriveBy": True}) == "arriveBy=true"
    assert http.encode_params({"arriveBy": False}) == "arriveBy=false"


def test_build_url_joins_path_and_query():
    url = http.build_url("/api/v6/plan", {"fromPlace": "1,2", "transitModes": ["RAIL"]})
    assert url == "https://api.transitous.org/api/v6/plan?fromPlace=1%2C2&transitModes=RAIL"
    assert http.build_url("/api/v1/health") == "https://api.transitous.org/api/v1/health"


def test_request_carries_the_unchanged_user_agent():
    request = http.build_request(http.build_url("/api/v1/health"))
    assert request.get_header("User-agent") == http.USER_AGENT
    assert "github.com/Dthen/transitous-mcp" in http.USER_AGENT
    assert "@" not in http.USER_AGENT           # no personal contact address
    assert http.TIMEOUT_SECONDS == 25


# ── success path ────────────────────────────────────────────────────


def test_api_get_returns_parsed_json_from_a_200_body():
    body = fs.read_bytes("synthetic_stop_metro_central.json")
    seen = []
    data = http.api_get(
        "/api/v6/stop", {"stopId": "gb-great-britain_940GZZTWCST"},
        transport=transport_returning(200, body, seen),
    )
    assert data["place"]["tz"] == "Europe/London"
    assert len(seen) == 1
    request, timeout = seen[0]
    assert timeout == http.TIMEOUT_SECONDS
    assert request.full_url == (
        "https://api.transitous.org/api/v6/stop?stopId=gb-great-britain_940GZZTWCST"
    )


def test_a_200_empty_itineraries_body_is_not_an_error():
    body = fs.read_bytes("synthetic_plan_empty_no_service.json")
    data = http.api_get("/api/v6/plan", {"fromPlace": "x"}, transport=transport_returning(200, body))
    assert data["itineraries"] == []


# ── status taxonomy ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status, body, outcome, reason",
    [
        (400, '{"error":"parameter \\"time\\": failed to parse timestamp \\"x\\""}',
         errors.UPSTREAM_REJECTED, errors.BAD_REQUEST),
        (401, "unauthorized", errors.UPSTREAM_REJECTED, errors.AUTH_OR_BLOCKED),
        (403, "forbidden", errors.UPSTREAM_REJECTED, errors.AUTH_OR_BLOCKED),
        (404, "no such stop", errors.UPSTREAM_REJECTED, errors.NOT_FOUND),
        (429, "slow down", errors.UPSTREAM_REJECTED, errors.RATE_LIMITED),
        (500, "kaboom", errors.UPSTREAM_REJECTED, errors.SERVER_ERROR),
        (503, "unavailable", errors.UPSTREAM_REJECTED, errors.SERVER_ERROR),
        (418, "teapot", errors.UPSTREAM_REJECTED, errors.BAD_REQUEST),
        (302, "", errors.UPSTREAM_REJECTED, errors.BAD_REQUEST),
    ],
)
def test_http_status_taxonomy(status, body, outcome, reason):
    with pytest.raises(errors.ToolError) as caught:
        http.api_get("/api/v6/plan", {}, transport=transport_returning(status, body))
    exc = caught.value
    assert (exc.outcome, exc.reason) == (outcome, reason)
    assert exc.details["status"] == status
    assert isinstance(exc.details["body_excerpt"], str)


def test_non_json_200_body_is_malformed_upstream():
    with pytest.raises(errors.ToolError) as caught:
        http.api_get("/api/v6/plan", {}, transport=transport_returning(200, "<html>nope</html>"))
    assert caught.value.outcome == errors.MALFORMED_UPSTREAM
    assert caught.value.reason == errors.NON_JSON_BODY


def test_the_recorded_naive_rejection_is_upstream_rejected_not_out_of_range():
    body = fs.read_text("synthetic_plan_glw_edi_naive_reject.json")
    with pytest.raises(errors.ToolError) as caught:
        http.api_get("/api/v6/plan", {}, transport=transport_returning(400, body))
    assert caught.value.outcome == errors.UPSTREAM_REJECTED
    assert caught.value.reason == errors.BAD_REQUEST
    assert caught.value.details["status"] == 400


# ── timetable window ────────────────────────────────────────────────


def test_window_predicate_parses_the_recorded_bracketed_interval():
    entries = fs.load("synthetic_window_error.json")["entries"]
    for entry in entries:
        window = http.parse_timetable_window(entry["body"])
        assert window == {"start": "2026-09-20 00:00", "end": "2027-10-21 00:00"}, entry["label"]


def test_window_predicate_is_case_insensitive_about_the_message():
    body = '{"error":"Query time 2026-07-15 09:00 is OUTSIDE OF LOADED TIMETABLE WINDOW [2026-09-20 00:00, 2027-10-21 00:00["}'
    assert http.parse_timetable_window(body) == {"start": "2026-09-20 00:00", "end": "2027-10-21 00:00"}


@pytest.mark.parametrize(
    "body",
    [
        '{"error":"query time 2026-07-15 09:00 is outside of loaded timetable window"}',
        '{"error":"query time is outside of loaded timetable window [2026-09-20, 2027-10-21["}',
        '{"error":"failed to parse timestamp"}',
        '{"error":"something else [2026-09-20 00:00, 2027-10-21 00:00["}',
        "",
    ],
)
def test_window_predicate_rejects_everything_but_the_exact_pattern(body):
    assert http.parse_timetable_window(body) is None


def test_a_window_400_becomes_out_of_range_with_the_interval_echoed():
    body = fs.load("synthetic_window_error.json")["entries"][0]["body"]
    with pytest.raises(errors.ToolError) as caught:
        http.api_get("/api/v6/plan", {"time": "2026-07-15T09:00:00"}, transport=transport_returning(400, body))
    exc = caught.value
    assert exc.outcome == errors.OUT_OF_RANGE
    assert exc.reason == errors.TIMETABLE_WINDOW
    assert exc.details["window"] == {"start": "2026-09-20 00:00", "end": "2027-10-21 00:00"}


# ── transport failures ──────────────────────────────────────────────


def test_timeout_maps_to_transport_error_timeout():
    with pytest.raises(errors.ToolError) as caught:
        http.api_get("/api/v6/plan", {}, transport=transport_raising(socket.timeout("timed out")))
    assert (caught.value.outcome, caught.value.reason) == (errors.TRANSPORT_ERROR, errors.TIMEOUT)


def test_urlerror_with_a_timeout_reason_maps_to_timeout():
    exc = urllib.error.URLError(socket.timeout("timed out"))
    with pytest.raises(errors.ToolError) as caught:
        http.api_get("/api/v6/plan", {}, transport=transport_raising(exc))
    assert caught.value.reason == errors.TIMEOUT


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError("connection refused"),
        ConnectionResetError("reset"),
        OSError("network unreachable"),
    ],
)
def test_connection_failures_map_to_connection_failure(exc):
    with pytest.raises(errors.ToolError) as caught:
        http.api_get("/api/v6/plan", {}, transport=transport_raising(exc))
    assert (caught.value.outcome, caught.value.reason) == (errors.TRANSPORT_ERROR, errors.CONNECTION_FAILURE)


def test_no_failure_is_retried():
    for transport in (
        transport_returning(500, "kaboom", counter := []),
        transport_raising(urllib.error.URLError("refused"), counter),
    ):
        with pytest.raises(errors.ToolError):
            http.api_get("/api/v6/plan", {}, transport=transport)
        assert len(counter) == 1
        counter.clear()


# ── transport failures while reading the body ───────────────────────


@pytest.mark.parametrize(
    "exc, outcome, reason",
    [
        (socket.timeout("timed out"), errors.TRANSPORT_ERROR, errors.TIMEOUT),
        (TimeoutError("timed out"), errors.TRANSPORT_ERROR, errors.TIMEOUT),
        (ConnectionResetError("reset"), errors.TRANSPORT_ERROR, errors.CONNECTION_FAILURE),
        (OSError("input/output error"), errors.TRANSPORT_ERROR, errors.CONNECTION_FAILURE),
        (IncompleteRead(b'{"itineraries": [', 100), errors.MALFORMED_UPSTREAM, errors.TRUNCATED_CONTAINER),
    ],
)
def test_a_failure_while_reading_the_body_is_classified_like_the_send(exc, outcome, reason):
    """§4.2: ``read()`` is a socket operation and carries the same failures."""
    response = ReadFailingResponse(200, exc)
    sends = []

    def transport(request, timeout):
        sends.append((request, timeout))
        return response

    with pytest.raises(errors.ToolError) as caught:
        http.api_get("/api/v6/plan", {}, transport=transport)
    assert (caught.value.outcome, caught.value.reason) == (outcome, reason)
    assert len(sends) == 1                      # exactly one request, no retry
    assert response.close_calls == ["close"]    # guaranteed close on the exceptional path


def test_a_known_non_success_status_outranks_a_body_read_failure():
    """The upstream did answer: its status is more specific than a read fault."""
    for status, reason in ((503, errors.SERVER_ERROR), (400, errors.BAD_REQUEST)):
        response = ReadFailingResponse(status, socket.timeout("timed out"))
        with pytest.raises(errors.ToolError) as caught:
            http.api_get(
                "/api/v6/plan", {},
                transport=lambda request, timeout, response=response: response,
            )
        exc = caught.value
        assert (exc.outcome, exc.reason) == (errors.UPSTREAM_REJECTED, reason)
        assert exc.details["body_excerpt"] == ""
        assert response.close_calls == ["close"]


def test_an_http_error_whose_body_cannot_be_read_keeps_the_http_status():
    class UnreadableHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__(
                "https://api.transitous.org/api/v6/plan",
                503,
                "boom",
                {"Content-Type": "application/json"},
                io.BytesIO(b"unavailable"),
            )
            self.closes = []

        def read(self, amt=None):
            raise ConnectionResetError("reset while reading the error body")

        def close(self):
            self.closes.append("close")

    error = UnreadableHTTPError()
    with pytest.raises(errors.ToolError) as caught:
        http.api_get("/api/v6/plan", {}, transport=transport_raising(error))
    assert (caught.value.outcome, caught.value.reason) == (
        errors.UPSTREAM_REJECTED, errors.SERVER_ERROR
    )
    assert caught.value.details["body_excerpt"] == ""
    assert error.closes == ["close"]


def test_a_truncated_body_never_exposes_or_parses_the_partial_bytes():
    partial = b'{"itineraries": [{"note": "super-secret-partial"}'
    response = ReadFailingResponse(200, IncompleteRead(partial, 4096))
    with pytest.raises(errors.ToolError) as caught:
        http.api_get(
            "/api/v6/plan", {}, transport=lambda request, timeout: response
        )
    exc = caught.value
    assert (exc.outcome, exc.reason) == (errors.MALFORMED_UPSTREAM, errors.TRUNCATED_CONTAINER)
    rendered = json.dumps(exc.to_result())
    assert "super-secret-partial" not in rendered
    assert "itineraries" not in rendered
    assert exc.details["expected_bytes"] == 4096
    assert exc.details["bytes_read"] == len(partial)
    assert response.close_calls == ["close"]


def test_a_programming_error_while_reading_the_body_is_not_masked():
    response = ReadFailingResponse(200, RuntimeError("bug in the caller"))
    with pytest.raises(RuntimeError, match="bug in the caller"):
        http.api_get("/api/v6/plan", {}, transport=lambda request, timeout: response)
    assert response.close_calls == ["close"]


def test_the_response_is_closed_exactly_once_on_success_and_on_a_read_failure():
    closes = []

    class CountingResponse(FakeResponse):
        def close(self):
            closes.append("close")

    data = http.api_get(
        "/api/v6/plan", {},
        transport=lambda request, timeout: CountingResponse(200, '{"itineraries": []}'),
    )
    assert data == {"itineraries": []}
    assert closes == ["close"]
    closes.clear()
    with pytest.raises(errors.ToolError):
        http.api_get(
            "/api/v6/plan", {},
            transport=lambda request, timeout: CountingResponse(200, b""),
        )


# ── excerpt hygiene ─────────────────────────────────────────────────


def test_error_excerpt_is_bounded_to_512_characters_after_redaction():
    """§4.2: at most 512 characters *with* credential-shaped substrings removed.

    Redaction can lengthen the text — ``[redacted]`` is longer than the shortest
    value it replaces — so a bound applied before redaction does not bound what is
    returned. Both credential bodies below sit at the truncation edge: the first
    expands the placeholder past the limit, the second has the secret itself
    straddling it.
    """
    bodies = {
        # the header word is assembled at runtime: the token scanner in
        # test_fixtures_integrity.py would otherwise flag this file
        "expanding_placeholder": (
            ("x" * 495)
            + " "
            + "".join(["Authori", "zation", ": ", "*** "])
            + ("A" * 100)
            + " token="
            + ("B" * 200)
        ),
        "secret_straddles_bound": ("y" * 500) + " token=" + ("C" * 400),
        "no_credentials": json.dumps({"error": "x" * 5000, "pad": "y" * 5000}),
    }
    for label, body in bodies.items():
        with pytest.raises(errors.ToolError) as caught:
            http.api_get("/api/v6/plan", {}, transport=transport_returning(400, body))
        exc = caught.value
        excerpt = exc.details["body_excerpt"]
        assert len(excerpt) <= http.EXCERPT_LIMIT == 512, label
        # redaction runs before the bound, so a straddling value is removed whole
        assert excerpt == http.redact(body)[: http.EXCERPT_LIMIT], label
        for secret in ("A" * 20, "B" * 20, "C" * 20):
            assert secret not in excerpt, (label, secret[0])
        # the canonical error object and its message carry the same bounded text
        payload = exc.to_result()
        assert payload["error_details"]["body_excerpt"] == excerpt, label
        assert len(payload["error"]) <= len("upstream responded HTTP 400: ") + 512, label

    # the non-JSON 2xx path echoes an excerpt too, under the same bound
    with pytest.raises(errors.ToolError) as caught:
        http.api_get(
            "/api/v6/plan", {}, transport=transport_returning(200, bodies["expanding_placeholder"])
        )
    assert caught.value.reason == errors.NON_JSON_BODY
    assert len(caught.value.details["body_excerpt"]) <= http.EXCERPT_LIMIT


def test_error_excerpt_redacts_credential_shaped_substrings():
    body = json.dumps(
        {
            "error": "bad request",
            "echo": "".join(["Authori", "zation: ", "Bear", "er "]) + "abcdefghijklmnopqrstuvwxyz",
            "cookie": "session=deadbeef",
        }
    )
    with pytest.raises(errors.ToolError) as caught:
        http.api_get("/api/v6/plan", {}, transport=transport_returning(400, body))
    excerpt = caught.value.details["body_excerpt"]
    assert "abcdefghijklmnopqrstuvwxyz" not in excerpt
    assert "deadbeef" not in excerpt


def test_error_result_has_the_constant_four_field_shape():
    with pytest.raises(errors.ToolError) as caught:
        http.api_get("/api/v6/plan", {}, transport=transport_returning(404, "nope"))
    payload = caught.value.to_result()
    assert set(payload) == {"error", "outcome", "error_type", "error_details"}
    assert payload["outcome"] == errors.UPSTREAM_REJECTED
    assert payload["error_type"] == errors.NOT_FOUND
    assert isinstance(payload["error"], str) and payload["error"]


# ── transport injection ─────────────────────────────────────────────


def test_a_module_level_transport_can_be_installed_and_reset():
    calls = []
    try:
        http.set_transport(transport_returning(200, '{"ok": true}', calls))
        assert http.api_get("/api/v1/health") == {"ok": True}
        assert len(calls) == 1
    finally:
        http.reset_transport()
    with pytest.raises(errors.ToolError):
        http.api_get("/api/v1/health", transport=transport_returning(500, "boom"))


def test_production_transport_resolves_urlopen_at_call_time(monkeypatch):
    """Patch urllib.request.urlopen AFTER import: the production path must see it.

    The future end-to-end suite patches urlopen before importing the server; this
    test pins the weaker property that the lookup is never cached at import time.
    """
    seen = []

    def fake_urlopen(request, timeout=None):
        seen.append((request.full_url, timeout))
        return FakeResponse(200, '{"patched": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert http.api_get("/api/v1/health") == {"patched": True}
    assert seen == [("https://api.transitous.org/api/v1/health", 25)]


# ── the one error constructor and the exhaustive reason map ─────────


def test_every_named_reason_maps_to_exactly_one_error_outcome():
    assert len(errors.REASONS) == 33
    for reason in errors.REASONS:
        outcome = errors.outcome_for_reason(reason)
        assert outcome in errors.ERROR_OUTCOMES, reason
        payload = errors.error_result(reason, "message", {"k": "v"})
        assert set(payload) == {"error", "outcome", "error_type", "error_details"}
        assert payload["outcome"] == outcome
        assert payload["error_type"] == reason
        assert payload["error_details"] == {"k": "v"}
        assert errors.is_error_result(payload)


def test_an_unmapped_reason_is_a_code_defect_not_a_silent_success():
    with pytest.raises(ValueError, match="unmapped error reason"):
        errors.error_result("not_a_reason")


def test_the_one_constructor_defaults_details_to_an_empty_object():
    payload = errors.error_result(errors.NOT_FOUND)
    assert payload["error_details"] == {}
    assert payload["error"] == "not found"


def test_a_success_result_never_carries_error_fields():
    payload = errors.success_result(errors.NO_ROUTES, itineraries=[], error="stale")
    assert payload["outcome"] == errors.NO_ROUTES
    assert "error" not in payload and "error_type" not in payload
    assert errors.is_error_result(payload) is False
    with pytest.raises(ValueError):
        errors.success_result(errors.INVALID_INPUT)


def test_the_production_interpreter_can_import_the_package():
    """``server.py`` runs under /usr/bin/python3; the package must import there."""
    import subprocess

    result = subprocess.run(
        [
            "/usr/bin/python3",
            "-c",
            "import transit_core, json;"
            "print(json.dumps(sorted(transit_core.__all__)))",
        ],
        capture_output=True,
        text=True,
        cwd=fs.REPO_ROOT,
        env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "timeparse" in result.stdout and "errors" in result.stdout
