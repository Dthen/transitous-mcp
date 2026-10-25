"""The single GET boundary: array serialisation, timeout, failure taxonomy (§4.2).

Every request the tools make passes through :func:`api_get`. Two defects can only
be fixed here: a list parameter serialised as a repeated query key, and a failure
taxonomy applied per call site. Both are now properties of this module.

Serialisation
    Each list or tuple value is joined with commas into one string and encoded
    once, so no request ever contains a repeated query key. A single-element list
    produces the byte-identical URL the previous encoding produced, and commas may
    be percent-encoded (the upstream accepts ``%2C``).

Failure taxonomy, in order
    timeout → ``transport_error`` / ``timeout``
    URLError, reset, OSError → ``transport_error`` / ``connection_failure``
    HTTP 400 matching the timetable-window pattern → ``out_of_range`` / ``timetable_window``
    HTTP 400 otherwise → ``upstream_rejected`` / ``bad_request``
    HTTP 401/403 → ``upstream_rejected`` / ``auth_or_blocked``
    HTTP 404 → ``upstream_rejected`` / ``not_found``
    HTTP 429 → ``upstream_rejected`` / ``rate_limited``
    HTTP 5xx → ``upstream_rejected`` / ``server_error``
    any other non-2xx → ``upstream_rejected`` / ``bad_request``
    2xx body that is not JSON → ``malformed_upstream`` / ``non_json_body``
    a transport failure while reading a 2xx body → the same timeout /
    ``connection_failure`` classification, because ``read()`` is a socket operation;
    an ``http.client.IncompleteRead`` while reading → ``malformed_upstream`` /
    ``truncated_container``, with no partial bytes parsed or echoed

An already-known non-2xx status always outranks a failure that follows it while
reading the error body: the upstream did answer, so the status is the more
specific fault and the excerpt is empty rather than guessed.

There are no retries, the timeout is bounded at 25 seconds, and the ``User-Agent``
is unchanged.

Transport injection
    ``transport(request, timeout)`` returns an object with ``status`` and
    ``read()``. The production transport calls ``urllib.request.urlopen`` through
    the module attribute at call time, never a cached binding, so an end-to-end
    suite can patch ``urllib.request.urlopen`` *before* importing the server and
    have the production path use the stub. :func:`set_transport` /
    :func:`reset_transport` install one for a test in this process.
"""

import http.client
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import errors

API_BASE = "https://api.transitous.org"

#: Unchanged: the contact is the project's public repository URL.
USER_AGENT = "HermesAgent/1.0 (transitous-mcp; +https://github.com/Dthen/transitous-mcp)"

#: Bounded request timeout in seconds. Never raised by a caller.
TIMEOUT_SECONDS = 25

#: Longest body excerpt echoed in an error's details.
EXCERPT_LIMIT = 512

#: Case-insensitive fragment of the upstream's out-of-window message.
WINDOW_MESSAGE = "outside of loaded timetable window"

_WINDOW_RE = None  # compiled lazily by _window_pattern()

_CREDENTIAL_RE = None


# ── serialisation ───────────────────────────────────────────────────


def encode_params(params):
    """Encode a mapping into a query string with no repeated keys.

    ``None`` values are dropped (the caller did not supply them); an empty list or
    tuple omits its key entirely, matching what the previous ``doseq`` encoding
    produced; list and tuple values are comma-joined before being encoded once.
    Order is the mapping's insertion order.
    """
    pairs = []
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            if not value:
                continue
            pairs.append((key, ",".join(str(item) for item in value)))
        elif isinstance(value, bool):
            pairs.append((key, "true" if value else "false"))
        else:
            pairs.append((key, str(value)))
    return urllib.parse.urlencode(pairs)


def build_url(path, params=None, base=API_BASE):
    """Absolute URL for ``path`` with ``params`` serialised once."""
    if not path.startswith("/"):
        path = "/" + path
    query = encode_params(params)
    return f"{base}{path}?{query}" if query else f"{base}{path}"


def build_request(url):
    """The one request shape: unchanged User-Agent, no other header."""
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


# ── transport ───────────────────────────────────────────────────────


def default_transport(request, timeout):
    """Production transport: ``urllib.request.urlopen`` resolved at call time.

    The attribute lookup is deliberately inside the function body so patching
    ``urllib.request.urlopen`` before importing a caller still takes effect.
    """
    return urllib.request.urlopen(request, timeout=timeout)


_transport_override = None


def set_transport(transport):
    """Install ``transport(request, timeout)`` for this process (tests only)."""
    global _transport_override
    _transport_override = transport


def reset_transport():
    """Restore the production transport."""
    global _transport_override
    _transport_override = None


def _resolve_transport(transport):
    if transport is not None:
        return transport
    if _transport_override is not None:
        return _transport_override
    return default_transport


def _status_of(response):
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else 200
    return int(status)


# ── body hygiene ────────────────────────────────────────────────────


def _credential_pattern():
    global _CREDENTIAL_RE
    if _CREDENTIAL_RE is None:
        _CREDENTIAL_RE = re.compile(
            r"(?i)\b(authorization|cookie|set-cookie|api[_-]?key|client[_-]?secret"
            r"|access[_-]?token|refresh[_-]?token|session[_-]?token|session|token|secret)\b"
            r"[\"']?(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
        )
    return _CREDENTIAL_RE


def redact(text):
    """Remove credential-shaped substrings from a body excerpt."""
    if not text:
        return ""
    # a bearer token is redacted before its scheme word is, so neither survives
    text = re.sub(r"(?i)\b(bearer)\s+\S+", r"\1 [redacted]", text)

    def replace(match):
        return f"{match.group(1)}{match.group(2)}[redacted]"

    return _credential_pattern().sub(replace, text)


def excerpt(body):
    """Bounded, credential-free excerpt of an upstream body for error details.

    Redaction runs over the whole body **before** the bound is applied. A
    replacement (``[redacted]``) is longer than the shortest value it replaces, so
    bounding first can return more than :data:`EXCERPT_LIMIT` characters; and a
    value that straddles the bound must be removed whole rather than echoed as a
    prefix. The returned string is therefore always at most ``EXCERPT_LIMIT``
    characters and never contains any part of a credential-shaped value.
    """
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    return redact(body or "")[:EXCERPT_LIMIT]


# ── timetable window ────────────────────────────────────────────────


def _window_pattern():
    global _WINDOW_RE
    if _WINDOW_RE is None:
        _WINDOW_RE = re.compile(
            r"\[\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*,\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*\["
        )
    return _WINDOW_RE


def parse_timetable_window(text):
    """``{"start", "end"}`` when ``text`` is the out-of-window message, else None.

    The predicate requires both the message fragment (case-insensitive) and the
    half-open bracketed interval ``[<start>, <end>[`` with ``YYYY-MM-DD HH:MM``
    endpoints. Nothing else is a timetable-window error.
    """
    if not text:
        return None
    if WINDOW_MESSAGE not in text.lower():
        return None
    match = _window_pattern().search(text)
    if match is None:
        return None
    return {"start": match.group(1), "end": match.group(2)}


# ── classification ──────────────────────────────────────────────────


def failure_for_status(status, body):
    """The classified failure for a non-2xx status, or ``None`` for 2xx."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else (body or "")
    if 200 <= status < 300:
        return None
    details = {"status": status, "body_excerpt": excerpt(text)}
    message = f"upstream responded HTTP {status}: {excerpt(text)}"
    if status == 400:
        window = parse_timetable_window(text)
        if window is not None:
            details["window"] = window
            return errors.ToolError(
                errors.TIMETABLE_WINDOW,
                f"requested time is outside the loaded timetable window "
                f"[{window['start']}, {window['end']}[",
                details,
            )
        return errors.ToolError(errors.BAD_REQUEST, message, details)
    if status in (401, 403):
        return errors.ToolError(errors.AUTH_OR_BLOCKED, message, details)
    if status == 404:
        return errors.ToolError(errors.NOT_FOUND, message, details)
    if status == 429:
        return errors.ToolError(errors.RATE_LIMITED, message, details)
    if 500 <= status < 600:
        return errors.ToolError(errors.SERVER_ERROR, message, details)
    return errors.ToolError(errors.BAD_REQUEST, message, details)


def transport_failure(exc):
    """Classify a raised transport exception, or return ``None`` if it is not one.

    A ``urllib`` timeout and a ``URLError`` whose reason is a timeout both map to
    ``timeout``; every other URLError, reset and OSError maps to
    ``connection_failure``.
    """
    timeout_types = (TimeoutError, socket.timeout)
    if isinstance(exc, urllib.error.HTTPError):
        return None                                  # handled from the response side
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, timeout_types):
            return errors.ToolError(errors.TIMEOUT, f"request timed out: {exc}", {"cause": str(exc)})
        return errors.ToolError(
            errors.CONNECTION_FAILURE, f"request failed: {exc}", {"cause": str(exc)}
        )
    if isinstance(exc, timeout_types):
        return errors.ToolError(errors.TIMEOUT, f"request timed out: {exc}", {"cause": str(exc)})
    if isinstance(exc, (OSError, ConnectionError)):
        return errors.ToolError(
            errors.CONNECTION_FAILURE, f"request failed: {exc}", {"cause": str(exc)}
        )
    return None


def _body_read_failure(exc):
    """Classify an exception raised while reading an already-received body."""
    timeout_types = (TimeoutError, socket.timeout)
    if isinstance(exc, http.client.IncompleteRead):
        # The partial bytes are never returned, echoed or parsed: only the counts
        # the exception carries, which cannot contain body content.
        return errors.ToolError(
            errors.TRUNCATED_CONTAINER,
            "upstream response body ended before its declared length",
            {
                "expected_bytes": getattr(exc, "expected", None),
                "bytes_read": len(getattr(exc, "partial", b"") or b""),
            },
        )
    if isinstance(exc, timeout_types):
        return errors.ToolError(errors.TIMEOUT, f"request timed out: {exc}", {"cause": str(exc)})
    if isinstance(exc, (OSError, ConnectionError)):
        return errors.ToolError(
            errors.CONNECTION_FAILURE, f"request failed: {exc}", {"cause": str(exc)}
        )
    return None


def _close_quietly(response):
    """Close a response exactly once; a close fault never masks the real failure."""
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:                            # pragma: no cover - defensive
            pass


# ── the one GET ─────────────────────────────────────────────────────


def api_get(path, params=None, *, transport=None, base=API_BASE):
    """GET ``path`` and return the parsed JSON body.

    Raises :class:`transit_core.errors.ToolError` for every failure, classified by
    :func:`failure_for_status`, :func:`transport_failure` or
    :func:`_body_read_failure`. Exactly one request is issued and the response is
    closed exactly once on every path; there is no retry path.
    """
    url = build_url(path, params, base=base)
    request = build_request(url)
    send = _resolve_transport(transport)

    try:
        response = send(request, TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        # An HTTPError is the upstream's answer. The status is already known, so an
        # unreadable error body degrades to an empty excerpt instead of being
        # re-labelled as a transport or truncation failure.
        try:
            body = exc.read()
        except Exception:
            body = b""
        finally:
            _close_quietly(exc)
        failure = failure_for_status(getattr(exc, "code", 0), body)
        raise failure from None
    except Exception as exc:
        failure = transport_failure(exc)
        if failure is None:
            raise
        raise failure from None

    try:
        status = _status_of(response)
        try:
            body = response.read()
        except Exception as exc:
            # A non-2xx status is a more specific answer than the read fault that
            # followed it; a 2xx response has no such answer, so the read fault is
            # classified on its own. Programming errors are re-raised unchanged.
            failure = failure_for_status(status, b"")
            if failure is None:
                failure = _body_read_failure(exc)
            if failure is None:
                raise
            raise failure from None
    finally:
        _close_quietly(response)

    failure = failure_for_status(status, body)
    if failure is not None:
        raise failure
    try:
        return json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except (ValueError, UnicodeDecodeError):
        raise errors.ToolError(
            errors.NON_JSON_BODY,
            "upstream returned a body that is not JSON",
            {"status": status, "body_excerpt": excerpt(body)},
        ) from None


__all__ = [
    "API_BASE", "USER_AGENT", "TIMEOUT_SECONDS", "EXCERPT_LIMIT", "WINDOW_MESSAGE",
    "encode_params", "build_url", "build_request",
    "default_transport", "set_transport", "reset_transport",
    "parse_timetable_window", "failure_for_status", "transport_failure", "api_get",
    "excerpt", "redact",
]
