"""The one native command declaration, its parser and its typed conversion.

Everything the CLI knows about the command surface lives here: one table of
commands, operands and options naming the backend argument each supplied value
becomes.  The parser is built from this table, conversion reads the parsed
namespace through the same entries, and dispatch/renderer selection use the
command record.  No MCP tool schema, registry module or framework is imported:
the operation names are the ones :mod:`transit_core.backend` publishes.

Absent optional arguments stay absent.  Every option is registered with
``argparse.SUPPRESS`` so the namespace simply has no attribute when the caller
did not supply it; :func:`to_backend_args` therefore never manufactures a
``None``, an empty list or a frontend copy of a domain default.

Grammar is deliberately narrow: integers are signed base-10 digit strings,
numbers are finite decimal/exponent tokens, coordinate tuples are exactly two
numbers separated by one comma, and a bounding box is exactly four numbers.  All
other ranges, enums, times and zone validity belong to the core.
"""

import argparse
import json
import math
import re

from transit_core import APPLICATION_VERSION

PROG = "transitous"

TEXT = "text"
INT = "int"
NUMBER = "number"
FLAG = "flag"
PAIR = "pair"
BBOX = "bbox"
CONTINUATION = "continuation"

_MISSING = object()

_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_NUMBER_RE = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")

TUPLE_EPILOG = (
    "Leading-dash values use the standard `=` spelling, e.g. "
    "`--from=-1.2,-4.6`, `--from-lon=-4.6`, or `--` before an opaque identifier: "
    "`trip -- '-opaque-id'`."
)

ROOT_EPILOG = (
    "Human output is the default. `--json` may appear before the command or among "
    "its options; it selects one JSON operation document (backend failures included). "
    "Usage and input-file failures print one CLI diagnostic instead: stdout in JSON "
    "mode, stderr in human mode. `--help` and `--version` are always plain text. "
    "Optional metadata omitted from human output is available with `--json`.\n\n"
    + TUPLE_EPILOG
)


class UsageError(Exception):
    """A CLI syntax or numeric/tuple error, before any backend call."""

    def __init__(self, message, usage=None):
        super().__init__(message)
        self.usage = usage


class InputFileError(Exception):
    """A continuation file/stdin problem, before any backend call."""


class HelpRequested(Exception):
    def __init__(self, parser):
        self.parser = parser


class VersionRequested(Exception):
    pass


# ── declaration records ─────────────────────────────────────────────


class Operand:
    __slots__ = ("dest", "kind", "metavar", "help")

    def __init__(self, dest, kind=TEXT, metavar=None, help=""):
        self.dest = dest
        self.kind = kind
        self.metavar = metavar or dest.upper()
        self.help = help


class Option:
    __slots__ = ("dest", "flags", "kind", "repeat", "metavar", "help", "targets",
                 "value", "required")

    def __init__(self, dest, flags, kind=TEXT, *, repeat=False, metavar=None,
                 help="", targets=None, value=None, required=False):
        self.dest = dest
        self.flags = tuple(flags)
        self.kind = kind
        self.repeat = repeat
        self.metavar = metavar
        self.help = help
        self.targets = tuple(targets) if targets else None
        self.value = value
        self.required = required


class Command:
    __slots__ = ("name", "operation", "operands", "options", "renderer", "help")

    def __init__(self, name, operation, *, operands=(), options=(), renderer=None,
                 help=""):
        self.name = name
        self.operation = operation
        self.operands = tuple(operands)
        self.options = tuple(options)
        self.renderer = renderer or operation
        self.help = help


# ── typed tokens ────────────────────────────────────────────────────


def parse_int(token):
    if not isinstance(token, str) or not _INT_RE.match(token):
        raise argparse.ArgumentTypeError(f"expected a signed base-10 integer, got {token!r}")
    return int(token, 10)


def parse_number(token):
    if not isinstance(token, str) or not _NUMBER_RE.match(token):
        raise argparse.ArgumentTypeError(
            f"expected a finite decimal number, got {token!r}"
        )
    value = float(token)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"expected a finite decimal number, got {token!r}")
    return value


def _split_exactly(token, count, label):
    parts = token.split(",") if isinstance(token, str) else []
    if len(parts) != count:
        raise argparse.ArgumentTypeError(
            f"{label} expects {count} comma-separated numbers, got {token!r}"
        )
    return tuple(parse_number(part) for part in parts)


def parse_pair(token):
    return _split_exactly(token, 2, "a coordinate")


def parse_bbox(token):
    return _split_exactly(token, 4, "a bounding box")


# ── the declaration ─────────────────────────────────────────────────

_ARRIVE_BY = (
    Option("arrive_by", ("--arrive-by",), FLAG, value=True,
           help="arrive by the given time (default: depart after it)"),
    Option("arrive_by", ("--no-arrive-by",), FLAG, value=False,
           help="depart after the given time (the negative form)"),
)

_TIME = Option("time", ("--time",), TEXT, metavar="T",
               help="explicit instant, naive local clock, or date; omitted means now")
_TIMEZONE = Option("timezone", ("--timezone",), TEXT, metavar="Z",
                   help="caller IANA zone used to read a naive clock")

COMMANDS = (
    Command(
        "plan", "plan",
        options=(
            Option("from_stop_id", ("--from-stop-id",), TEXT, metavar="ID",
                   help="origin stop id, sent verbatim"),
            Option("to_stop_id", ("--to-stop-id",), TEXT, metavar="ID",
                   help="destination stop id, sent verbatim"),
            Option("from_lat", ("--from-lat",), NUMBER, metavar="LAT",
                   help="origin latitude"),
            Option("from_lon", ("--from-lon",), NUMBER, metavar="LON",
                   help="origin longitude"),
            Option("to_lat", ("--to-lat",), NUMBER, metavar="LAT",
                   help="destination latitude"),
            Option("to_lon", ("--to-lon",), NUMBER, metavar="LON",
                   help="destination longitude"),
            _TIME,
            *_ARRIVE_BY,
            _TIMEZONE,
            Option("origin_timezone", ("--origin-timezone",), TEXT, metavar="Z",
                   help="declared origin display zone"),
            Option("destination_timezone", ("--destination-timezone",), TEXT,
                   metavar="Z", help="declared destination display zone"),
            Option("max_transfers", ("--max-transfers",), INT, metavar="N"),
            Option("transit_modes", ("--transit-mode",), TEXT, repeat=True,
                   metavar="MODE", help="transit mode, repeatable in caller order"),
            Option("num_itineraries", ("--num-itineraries",), INT, metavar="N"),
            Option("max_itineraries", ("--max-itineraries",), INT, metavar="N"),
            Option("max_results", ("--max-results",), INT, metavar="N"),
            Option("search_window_seconds", ("--search-window-seconds",), INT,
                   metavar="N"),
            Option("continuation", ("--continuation",), CONTINUATION, metavar="FILE|-",
                   help="a previous/next continuation object (JSON), or - for stdin"),
            Option("include_direct", ("--include-direct",), FLAG, value=True,
                   help="include street-only direct alternatives"),
            Option("include_direct", ("--no-include-direct",), FLAG, value=False,
                   help="omit street-only direct alternatives"),
            Option("max_access_walk_minutes", ("--max-access-walk-minutes",), INT,
                   metavar="N"),
            Option("max_egress_walk_minutes", ("--max-egress-walk-minutes",), INT,
                   metavar="N"),
            Option("min_transfer_minutes", ("--min-transfer-minutes",), INT,
                   metavar="N"),
        ),
        help="plan a public transit journey",
    ),
    Command(
        "geocode", "geocode",
        operands=(Operand("query", TEXT, "QUERY", "place name, address or stop name"),),
        options=(
            Option("types", ("--type",), TEXT, repeat=True, metavar="KIND",
                   help="match kind, repeatable in caller order"),
            Option("modes", ("--mode",), TEXT, repeat=True, metavar="MODE",
                   help="transport mode filter, repeatable in caller order"),
            Option("near", ("--near",), PAIR, metavar="LAT,LON",
                   help="ranking bias coordinate; filters nothing"),
            Option("near_bias", ("--near-bias",), NUMBER, metavar="NUMBER"),
            Option("num_results", ("--num-results",), INT, metavar="N"),
            Option("language", ("--language",), TEXT, metavar="TEXT",
                   help="preferred result language; one string, commas kept"),
        ),
        help="resolve a place name, address or stop",
    ),
    Command(
        "departures", "departures",
        operands=(Operand("stop_id", TEXT, "STOP_ID", "stop id, verbatim"),),
        options=(
            Option("count", ("--count",), INT, metavar="N"),
            _TIME,
            Option("window", ("--window",), INT, metavar="SECONDS"),
            _TIMEZONE,
            Option("endpoint_timezone", ("--endpoint-timezone",), TEXT, metavar="Z",
                   help="declared stop display zone"),
        ),
        help="live departure board for one stop",
    ),
    Command(
        "nearby-stops", "nearby_stops",
        operands=(
            Operand("lat", NUMBER, "LAT", "latitude"),
            Operand("lon", NUMBER, "LON", "longitude"),
        ),
        options=(Option("radius", ("--radius",), INT, metavar="METRES",
                        help="search radius in metres"),),
        help="nearest stops around a coordinate",
    ),
    Command(
        "reverse-geocode", "reverse_geocode",
        operands=(
            Operand("lat", NUMBER, "LAT", "latitude"),
            Operand("lon", NUMBER, "LON", "longitude"),
        ),
        options=(
            Option("types", ("--type",), TEXT, repeat=True, metavar="KIND"),
            Option("num_results", ("--num-results",), INT, metavar="N"),
        ),
        help="closest places, stops or addresses to a coordinate",
    ),
    Command(
        "trip", "trip",
        operands=(Operand("trip_id", TEXT, "TRIP_ID", "trip id, verbatim"),),
        options=(),
        help="full stop sequence for one trip",
    ),
    Command("health", "health", options=(), help="check the upstream API health"),
    Command(
        "reachable", "reachable",
        operands=(
            Operand("lat", NUMBER, "LAT", "latitude of the one endpoint"),
            Operand("lon", NUMBER, "LON", "longitude of the one endpoint"),
        ),
        options=(
            Option("max_time_min", ("--max-time-min",), INT, metavar="N"),
            _TIME,
            *_ARRIVE_BY,
            _TIMEZONE,
            Option("endpoint_timezone", ("--endpoint-timezone",), TEXT, metavar="Z"),
        ),
        help="places reachable within a travel-time budget",
    ),
    Command(
        "refresh", "refresh",
        operands=(Operand("itinerary_id", TEXT, "ITINERARY_ID",
                          "itinerary id, verbatim"),),
        options=(),
        help="re-fetch one itinerary with realtime updates",
    ),
    Command(
        "rentals", "rentals",
        operands=(
            Operand("lat", NUMBER, "LAT", "latitude"),
            Operand("lon", NUMBER, "LON", "longitude"),
        ),
        options=(Option("radius", ("--radius",), INT, metavar="METRES"),),
        help="nearby shared bikes, scooters and cars",
    ),
    Command(
        "one-to-many", "one_to_many",
        options=(
            Option("from", ("--from",), PAIR, metavar="LAT,LON", required=True,
                   targets=("from_lat", "from_lon"),
                   help="the one endpoint coordinate"),
            Option("destinations", ("--destination",), PAIR, repeat=True,
                   metavar="LAT,LON", required=True,
                   help="destination coordinate, repeatable in caller order (at least one)"),
            Option("mode", ("--mode",), TEXT, metavar="PROFILE",
                   help="street profile; omitted leaves the core default in force"),
            Option("max_sec", ("--max-sec",), NUMBER, metavar="NUMBER",
                   help="maximum street duration in seconds; fractions allowed"),
            *_ARRIVE_BY,
        ),
        help="street durations from one coordinate to many",
    ),
    Command(
        "one-to-many-transit", "one_to_many_transit",
        options=(
            Option("from", ("--from",), PAIR, metavar="LAT,LON", required=True,
                   targets=("from_lat", "from_lon"),
                   help="the one endpoint coordinate"),
            Option("destinations", ("--destination",), PAIR, repeat=True,
                   metavar="LAT,LON", required=True,
                   help="destination coordinate, repeatable in caller order (at least one)"),
            _TIME,
            *_ARRIVE_BY,
            Option("max_transfers", ("--max-transfers",), INT, metavar="N"),
            Option("transit_modes", ("--transit-mode",), TEXT, repeat=True,
                   metavar="MODE"),
            Option("max_access_walk_minutes", ("--max-access-walk-minutes",), INT,
                   metavar="N"),
            Option("max_egress_walk_minutes", ("--max-egress-walk-minutes",), INT,
                   metavar="N"),
            Option("max_travel_minutes", ("--max-travel-minutes",), INT, metavar="N"),
            _TIMEZONE,
            Option("endpoint_timezone", ("--endpoint-timezone",), TEXT, metavar="Z"),
        ),
        help="street duration plus transit Pareto set for many destinations",
    ),
    Command(
        "map-trips", "map_trips",
        options=(
            Option("bbox", ("--bbox",), BBOX, required=True,
                   metavar="MIN_LAT,MIN_LON,MAX_LAT,MAX_LON",
                   targets=("min_lat", "min_lon", "max_lat", "max_lon"),
                   help="bounding box, in the stated order (not reordered or repaired)"),
            Option("start_time", ("--start-time",), TEXT, metavar="T", required=True),
            Option("end_time", ("--end-time",), TEXT, metavar="T", required=True),
            Option("zoom", ("--zoom",), INT, metavar="N"),
            _TIMEZONE,
        ),
        help="active trips inside a bounding box and time window",
    ),
)

COMMANDS_BY_NAME = {command.name: command for command in COMMANDS}


# ── parser construction ─────────────────────────────────────────────


class _HelpAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS,
                 default=argparse.SUPPRESS, help="show this help message and exit"):
        super().__init__(option_strings, dest, nargs=0, default=default, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        raise HelpRequested(parser)


class _VersionAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS,
                 default=argparse.SUPPRESS, help="show the application version and exit"):
        super().__init__(option_strings, dest, nargs=0, default=default, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        raise VersionRequested()


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message, self.format_usage())

    def exit(self, status=0, message=None):
        if message:
            raise UsageError(message.strip())
        raise SystemExit(status)


_CONVERTERS = {TEXT: str, INT: parse_int, NUMBER: parse_number, PAIR: parse_pair,
               BBOX: parse_bbox, CONTINUATION: str}


def _add_json(parser):
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        dest="json", help="write one JSON operation document")


def find_command(name, commands=None):
    """The declared command record for ``name`` (``None`` when unknown)."""
    commands = COMMANDS if commands is None else commands
    return next((command for command in commands if command.name == name), None)


def _value_taking_flags(commands):
    names = set()
    for command in commands:
        for option in command.options:
            if option.kind is not FLAG:
                names.update(option.flags)
    return names


_NEGATIVE_NUMBER = re.compile(r"^-[0-9]+$|^-[0-9]*\.[0-9]+$")


def _looks_like_option(token):
    return (
        token.startswith("-")
        and token != "-"
        and not _NEGATIVE_NUMBER.match(token)
    )


def scan_json_intent(argv, commands=None):
    """True when ``--json`` occupies an actual option-token role.

    The scan follows argparse's own reading: ``--`` ends option recognition, a
    token consumed as another option's value is not an output selector (so
    ``--time=--json`` and ``--time --json`` differ), and only a ``--json`` in
    option position selects JSON output.  Used to choose the diagnostic channel
    when parsing itself fails.
    """
    commands = tuple(COMMANDS if commands is None else commands)
    takers = _value_taking_flags(commands)
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            # Everything after `--` is an operand; no output selector there.
            return False
        if token.startswith("--") and len(token) > 2:
            name, separator, _value = token.partition("=")
            if name == "--json" and not separator:
                return True
            if name in takers and not separator:
                if index + 1 < len(argv) and not _looks_like_option(argv[index + 1]):
                    index += 2
                    continue
        index += 1
    return False


def build_parser(commands=None, prog=PROG):
    """Build the argparse parser from the declaration (defaults to :data:`COMMANDS`)."""
    commands = tuple(COMMANDS if commands is None else commands)
    root = _Parser(prog=prog, add_help=False, allow_abbrev=False,
                   description="Native Transitous command line: public transit "
                               "routing through the shared application backend.",
                   epilog=ROOT_EPILOG,
                   formatter_class=argparse.RawDescriptionHelpFormatter)
    root.add_argument("-h", "--help", action=_HelpAction)
    root.add_argument("--version", action=_VersionAction,
                      help=f"print 'transitous {APPLICATION_VERSION}' and exit")
    _add_json(root)

    subparsers = root.add_subparsers(dest="command", metavar="COMMAND", required=True,
                                     title="commands")
    for command in commands:
        parser = subparsers.add_parser(
            command.name, add_help=False, allow_abbrev=False,
            help=command.help, description=command.help,
            epilog=TUPLE_EPILOG,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument("-h", "--help", action=_HelpAction)
        _add_json(parser)
        for operand in command.operands:
            parser.add_argument(
                operand.dest, metavar=operand.metavar,
                type=_CONVERTERS[operand.kind], help=operand.help,
            )
        for option in command.options:
            kwargs = {
                "dest": option.dest,
                "default": argparse.SUPPRESS,
                "help": option.help,
            }
            if option.metavar:
                kwargs["metavar"] = option.metavar
            if option.kind is FLAG:
                kwargs["action"] = "store_true" if option.value is True else "store_false"
            else:
                kwargs["action"] = "append" if option.repeat else "store"
                kwargs["type"] = _CONVERTERS[option.kind]
            if option.required:
                kwargs["required"] = True
            parser.add_argument(*option.flags, **kwargs)
    return root


# ── continuation input ──────────────────────────────────────────────


class _DuplicateKeys(ValueError):
    pass


def _object_pairs(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKeys(f"duplicate object key: {key!r}")
        seen[key] = value
    return seen


def _reject_constant(token):
    raise ValueError(f"nonstandard JSON constant: {token}")


def decode_continuation(raw_bytes):
    """Decode one continuation document from UTF-8 bytes, strictly."""
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raise InputFileError("continuation input starts with a UTF-8 BOM; a BOM is not accepted")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputFileError(f"continuation input is not valid UTF-8: {exc}") from None
    try:
        document = json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except _DuplicateKeys as exc:
        raise InputFileError(str(exc)) from None
    except (ValueError, RecursionError) as exc:
        # Excessive nesting exhausts the decoder's recursion; that is a decoding
        # failure of this local input, not a frontend/internal fault.
        raise InputFileError(f"continuation input is not valid JSON: {exc}") from None
    if not isinstance(document, dict):
        raise InputFileError(
            "continuation input must be one JSON object {cursor, query}; "
            f"got {type(document).__name__}"
        )
    return document


def read_continuation(path, stdin=None):
    """Read ``path`` (or stdin for ``-``) and return the decoded object."""
    if path == "-":
        stream = stdin if stdin is not None else _sys_stdin()
        if stream is None:
            raise InputFileError("continuation input '-' requested but stdin is unavailable")
        try:
            buffer = getattr(stream, "buffer", None)
            if buffer is not None:
                raw = buffer.read()
            else:
                text = stream.read()
                raw = text.encode("utf-8") if isinstance(text, str) else bytes(text)
        except (OSError, ValueError, TypeError) as exc:
            raise InputFileError(
                f"cannot read continuation input from stdin: {exc}"
            ) from None
        return decode_continuation(raw)
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "strerror", None) or exc
        raise InputFileError(f"cannot read continuation file {path!r}: {reason}") from None
    return decode_continuation(raw)


def _sys_stdin():
    import sys

    return getattr(sys, "stdin", None)


# ── conversion ──────────────────────────────────────────────────────


def to_backend_args(namespace, commands=None, stdin=None):
    """Turn the parsed namespace into the backend argument mapping.

    Only attributes the caller supplied are copied.  Pair/bbox options expand to
    their declared targets; repeated pairs append one two-number list per value;
    a continuation path is read and decoded into the object itself.
    """
    commands = COMMANDS if commands is None else commands
    by_name = {command.name: command for command in commands}
    command = by_name.get(getattr(namespace, "command", None))
    if command is None:
        raise UsageError("no command supplied")

    args = {}
    for operand in command.operands:
        value = getattr(namespace, operand.dest, _MISSING)
        if value is not _MISSING:
            args[operand.dest] = value
    for option in command.options:
        value = getattr(namespace, option.dest, _MISSING)
        if value is _MISSING:
            continue
        if option.kind is PAIR:
            if option.repeat:
                args.setdefault(option.dest, []).extend([list(item) for item in value])
            elif option.targets:
                args[option.targets[0]], args[option.targets[1]] = value
            else:
                args[option.dest] = list(value)
        elif option.kind is BBOX:
            for target, part in zip(option.targets, value):
                args[target] = part
        elif option.kind is CONTINUATION:
            args[option.dest] = read_continuation(value, stdin=stdin)
        else:
            args[option.dest] = value
    return args


__all__ = [
    "PROG", "TEXT", "INT", "NUMBER", "FLAG", "PAIR", "BBOX", "CONTINUATION",
    "UsageError", "InputFileError", "HelpRequested", "VersionRequested",
    "Operand", "Option", "Command", "COMMANDS", "COMMANDS_BY_NAME",
    "parse_int", "parse_number", "parse_pair", "parse_bbox",
    "build_parser", "find_command", "scan_json_intent", "decode_continuation",
    "read_continuation", "to_backend_args",
]
