"""The native ``transitous`` command line frontend.

This module owns argument parsing entry, dispatch to
:func:`transit_core.backend.run`, output ownership and exit classification.  It
calls the application boundary directly: no MCP client, protocol session, server
process or tool registry is involved, and nothing here imports ``server`` or
``transitous_mcp``.

Output discipline
    The whole document is built before the first stdout write.  After any write
    attempt, a broken pipe, failed flush, encoding fault or interruption returns
    a failure status and never appends a replacement document.  CLI-local
    failures use one CLI-only diagnostic (``outcome: cli_error``); backend
    results — including backend errors — are written unchanged.
"""

import json
import os

from transit_core import APPLICATION_VERSION, backend, errors

from . import declaration, human

_RAW = frozenset({"health", "refresh", "rentals"})


class _OutputFault(Exception):
    pass


def _silence(stream):
    """Point a broken standard stream's descriptor at devnull, safely.

    CPython's shutdown flush repeats a failed final flush and can turn our status
    1/2 into 120.  A stream without a descriptor (a StringIO) needs nothing.

    The devnull descriptor may itself be the closed destination number: when fd 1
    (or 2) is already closed, ``os.open`` returns that lowest free number.  In that
    case the descriptor already points at devnull after the open, so it must not be
    closed again — doing so re-breaks the stream and restores the shutdown fault.
    """
    try:
        descriptor = stream.fileno()
    except Exception:
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    if devnull == descriptor:
        # The closed destination number was just reacquired as devnull.
        return
    try:
        os.dup2(devnull, descriptor)
    except OSError:
        pass
    finally:
        try:
            os.close(devnull)
        except OSError:
            pass


def _write(stream, text):
    """Write one complete document, or raise :class:`_OutputFault`.

    A missing stream, a closed descriptor, a broken pipe or an encoding failure
    is an output fault: the caller reports status 1 and does not try again.
    ``KeyboardInterrupt`` is deliberately not caught here — it means 130.
    """
    if stream is None:
        raise _OutputFault("stdout is unavailable")
    try:
        stream.write(text)
        stream.flush()
    except (BrokenPipeError, OSError, ValueError, UnicodeError) as exc:
        raise _OutputFault(str(exc)) from None


def _quietly(stream, text):
    """Best-effort diagnostic; never changes a status.

    A failed diagnostic write neutralises that descriptor so the interpreter's
    shutdown flush cannot turn a classified status (2/1) into 120.  An interrupt
    is never swallowed here: it propagates so the frontend boundary can return
    130 instead of reporting success or a caller error.
    """
    if stream is None:
        return False
    try:
        stream.write(text)
        stream.flush()
    except KeyboardInterrupt:
        raise
    except Exception:
        _silence(stream)
        return False
    return True


def _bounded(message, limit=300):
    text = message if isinstance(message, str) else str(message)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _diagnostic(message, error_type):
    return {
        "error": _bounded(message),
        "outcome": "cli_error",
        "error_type": error_type,
        "error_details": {},
    }


def _json_text(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"


def _exit_code(result):
    if errors.is_error_result(result):
        return 2 if result.get("outcome") == errors.INVALID_INPUT else 1
    return 0


def _format_backend_error(result):
    lines = [f"transitous: error: {_bounded(result.get('error'), 500)}"]
    lines.append(f"  outcome: {result.get('outcome')}")
    if result.get("error_type"):
        lines.append(f"  error_type: {result['error_type']}")
    details = result.get("error_details")
    if isinstance(details, dict) and details:
        remedy = details.get("remedy")
        if remedy:
            lines.append(f"  remedy: {remedy}")
        rest = {key: value for key, value in details.items() if key != "remedy"}
        if rest:
            lines.append("  details: " + json.dumps(rest, ensure_ascii=True, default=str))
    return "\n".join(lines) + "\n"


def _local_failure(stdout, stderr, json_mode, error_type, message, code=2, emitted=False,
                   human_message=None):
    """Report one CLI-local failure with no backend result behind it."""
    if emitted:
        # A document (or part of one) has already gone to stdout: only a brief
        # stderr note is allowed, and never a replacement document.
        _quietly(stderr, f"transitous: {error_type}: {_bounded(message)}\n")
        return code
    if json_mode:
        try:
            _write(stdout, _json_text(_diagnostic(message, error_type)))
        except KeyboardInterrupt:
            return 130
        except _OutputFault:
            _silence(stdout)
            return 1
        return code
    if human_message is None:
        human_message = f"transitous: error: {message}"
    _quietly(stderr, human_message if human_message.endswith("\n") else human_message + "\n")
    return code


def _emit_result(command, result, json_mode, stdout, stderr):
    code = _exit_code(result)

    if json_mode:
        try:
            text = _json_text(result)
        except (ValueError, TypeError):
            return _local_failure(stdout, stderr, True, "internal_error",
                                  "internal error while serialising the result", 1)
        try:
            _write(stdout, text)
        except KeyboardInterrupt:
            return 130
        except _OutputFault:
            _silence(stdout)
            return 1
        return code

    if errors.is_error_result(result):
        if command is not None and command.operation in _RAW:
            # A raw marked error keeps its whole diagnostic content, whatever shape
            # the provider used; it is not assumed to be the canonical four fields.
            _quietly(stderr, json.dumps(result, indent=2, ensure_ascii=True,
                                        allow_nan=False, default=str) + "\n")
        else:
            _quietly(stderr, _format_backend_error(result))
        return code

    try:
        body = human.render(command.renderer if command is not None else "?", result)
    except Exception:
        return _local_failure(stdout, stderr, False, "internal_error",
                              "internal error while rendering the result", 1)
    try:
        _write(stdout, body + "\n")
    except KeyboardInterrupt:
        return 130
    except _OutputFault:
        _silence(stdout)
        return 1

    warnings = result.get("warnings") if isinstance(result, dict) else None
    if isinstance(warnings, list):
        for warning in warnings:
            _quietly(stderr, "warning: " + json.dumps(warning, ensure_ascii=True,
                                                      sort_keys=True, default=str) + "\n")
    return code


def main(argv=None, *, stdout=None, stderr=None, commands=None, prog=declaration.PROG):
    """Run the CLI and return its exit status (0, 1, 2 or 130).

    This boundary owns interruption: a real ``KeyboardInterrupt`` raised anywhere
    inside the frontend — including while a best-effort stderr diagnostic or a
    warning is being written — becomes status 130 with no traceback and no
    replacement document.  It is never swallowed as success or as a caller error.
    """
    try:
        return _run(argv, stdout=stdout, stderr=stderr, commands=commands, prog=prog)
    except KeyboardInterrupt:
        return 130


def _run(argv=None, *, stdout=None, stderr=None, commands=None, prog=declaration.PROG):
    """The frontend body; ``main`` owns the interruption boundary."""
    if argv is None:
        import sys

        argv = sys.argv[1:]
    argv = list(argv)
    if stdout is None:
        import sys

        stdout = getattr(sys, "stdout", None)
    if stderr is None:
        import sys

        stderr = getattr(sys, "stderr", None)

    json_mode = declaration.scan_json_intent(argv, commands)
    parser = declaration.build_parser(commands, prog=prog)

    try:
        namespace = parser.parse_args(argv)
    except declaration.HelpRequested as request:
        try:
            _write(stdout, request.parser.format_help())
        except KeyboardInterrupt:
            return 130
        except _OutputFault:
            _silence(stdout)
            return 1
        return 0
    except declaration.VersionRequested:
        try:
            _write(stdout, f"{prog} {APPLICATION_VERSION}\n")
        except KeyboardInterrupt:
            return 130
        except _OutputFault:
            _silence(stdout)
            return 1
        return 0
    except declaration.UsageError as exc:
        return _local_failure(stdout, stderr, json_mode, "usage", str(exc), 2,
                              human_message=_usage_message(exc))
    except KeyboardInterrupt:
        return _local_failure(stdout, stderr, json_mode, "interrupted",
                              "interrupted", 130)

    try:
        args = declaration.to_backend_args(namespace, commands)
    except declaration.UsageError as exc:
        return _local_failure(stdout, stderr, json_mode, "usage", str(exc), 2,
                              human_message=_usage_message(exc))
    except declaration.InputFileError as exc:
        return _local_failure(stdout, stderr, json_mode, "input_file", str(exc), 2)
    except KeyboardInterrupt:
        return _local_failure(stdout, stderr, json_mode, "interrupted",
                              "interrupted", 130)

    command = declaration.find_command(namespace.command, commands)

    try:
        result = backend.run(command.operation, args)
    except KeyboardInterrupt:
        return _local_failure(stdout, stderr, json_mode, "interrupted",
                              "interrupted", 130)
    except Exception:
        return _local_failure(stdout, stderr, json_mode, "internal_error",
                              "internal error", 1)

    return _emit_result(command, result, json_mode, stdout, stderr)


def _usage_message(exc):
    message = str(exc)
    usage = getattr(exc, "usage", None)
    return message if not usage else f"{usage}transitous: error: {message}\n"


__all__ = ["main"]
