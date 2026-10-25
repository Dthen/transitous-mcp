"""External scratch fixture loader and manifest verifier.

Two rules this module exists to enforce:

1. Set TRANSITOUS_TEST_FIXTURES to the external scratch corpus directory.
   Captured response data is deliberately not stored in the repository.
2. Every fixture is loaded afresh from disk. A caller may mutate the returned
   object freely; no other call sees that mutation, and no module-level cache can
   turn a test into a consumer of another test's edits.

The corpus path is explicit, so the suite behaves identically whatever the
current working directory or host time zone is.

Bodies above 300 KB are stored gzipped (``*.json.gz``); :func:`read_bytes` opens
either form and returns the uncompressed logical bytes, which is what the
manifest hashes.
"""

import gzip
import hashlib
import json
import os
import re
import sys
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_fixture_root = os.environ.get("TRANSITOUS_TEST_FIXTURES")
if not _fixture_root:
    raise RuntimeError(
        "Capture-backed tests require TRANSITOUS_TEST_FIXTURES to point to an "
        "external scratch corpus; captured responses are not repository content."
    )
FIXTURES_DIR = os.path.realpath(os.path.expanduser(_fixture_root))
if not os.path.isdir(FIXTURES_DIR):
    raise RuntimeError("TRANSITOUS_TEST_FIXTURES is not an existing scratch corpus directory")
MANIFEST_NAME = "README.md"

# The production package lives at the repository root. pytest inserts only the
# test file's own directory for a non-package test tree, so put the root on the
# path here: importing this helper before ``transit_core`` is enough for every
# test module, whatever interpreter drives pytest.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

GZIP_THRESHOLD_BYTES = 300 * 1024

#: Observed fixtures that are *derived* rather than byte-identical copies. Their
#: manifest ``source_sha256`` describes the source package file, so it is not
#: expected to equal the fixture's own content hash.
DERIVED_OBSERVED = frozenset({"window_error.json"})

MANIFEST_COLUMNS = (
    "fixture",
    "kind",
    "source",
    "source_sha256",
    "captured_utc",
    "query_direction",
    "constraints",
    "asserts",
    "content_sha256",
)

#: Request parameters that *constrain* a plan/board query. A row's ``constraints``
#: cell is checked against the URL recorded in the same row for these names only:
#: the URL is the recorded request, so the cell may neither contradict it nor drop
#: a constraint it carries (§8.4 provenance applies to every row). Directions,
#: places and times are not constraint columns and are not compared here.
CONSTRAINT_PARAMETERS = (
    "numItineraries",
    "maxTransfers",
    "transitModes",
    "arriveBy",
    "maxPreTransitTime",
    "maxPostTransitTime",
    "searchWindow",
    "n",
)

_URL_RE = re.compile(r"https?://\S+")
_CONSTRAINT_PAIR_RE = re.compile(r"(?<![\w-])([A-Za-z][A-Za-z0-9_]*)=([^\s,;)]+)")


def fixture_path(name):
    """Absolute path of a fixture file. Raises FileNotFoundError if absent."""
    if not isinstance(name, str) or not name or os.path.basename(name) != name:
        raise FileNotFoundError(f"not a fixture filename: {name!r}")
    path = os.path.realpath(os.path.join(FIXTURES_DIR, name))
    if os.path.dirname(path) != os.path.realpath(FIXTURES_DIR):
        raise FileNotFoundError(f"fixture resolves outside {FIXTURES_DIR}: {name!r}")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no such fixture: {name} (looked in {FIXTURES_DIR})")
    return path


def fixture_names():
    """Sorted fixture filenames (the manifest itself excluded)."""
    return sorted(
        name
        for name in os.listdir(FIXTURES_DIR)
        if os.path.isfile(os.path.join(FIXTURES_DIR, name)) and name != MANIFEST_NAME
    )


def read_bytes(name):
    """Uncompressed logical bytes of a fixture, gzipped or not."""
    raw = open(fixture_path(name), "rb").read()
    if name.endswith(".gz"):
        return gzip.decompress(raw)
    return raw


def read_text(name):
    return read_bytes(name).decode("utf-8")


def load(name):
    """Parse a fixture and return a fresh object on every call."""
    return json.loads(read_text(name))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def fixture_content_sha(name):
    return sha256_bytes(read_bytes(name))


def load_manifest(fixtures_dir=None):
    """Parse the provenance table into a list of row dicts.

    Each row is a Markdown table line whose first cell is a backticked filename.
    The parser is strict about the column count so a malformed row is a failure
    rather than silently dropped.
    """
    directory = fixtures_dir or FIXTURES_DIR
    with open(os.path.join(directory, MANIFEST_NAME), encoding="utf-8") as fh:
        text = fh.read()
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("| `"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != len(MANIFEST_COLUMNS):
            raise AssertionError(
                f"manifest row has {len(cells)} cells, expected {len(MANIFEST_COLUMNS)}: {stripped[:120]}"
            )
        row = dict(zip(MANIFEST_COLUMNS, cells))
        row["fixture"] = row["fixture"].strip("`").strip()
        for key in ("source_sha256", "content_sha256"):
            row[key] = row[key].strip("`").strip()
        rows.append(row)
    return rows


def recorded_url_constraints(query_direction):
    """Constraint parameters of the request URL recorded in a ``query_direction``.

    Returns ``None`` when the cell records no URL at all — a derived or
    constructed body has no single request to compare a constraint cell against —
    otherwise ``{name: value}`` for the :data:`CONSTRAINT_PARAMETERS` the URL
    carries. The last occurrence wins, matching last-supplied-wins URL semantics.
    """
    match = _URL_RE.search(query_direction or "")
    if match is None:
        return None
    query = urlsplit(match.group(0).rstrip(".,;")).query
    recorded = parse_qs(query, keep_blank_values=True)
    return {name: recorded[name][-1] for name in CONSTRAINT_PARAMETERS if name in recorded}


def stated_constraints(cell):
    """The ``name=value`` pairs written in a manifest ``constraints`` cell."""
    return dict(_CONSTRAINT_PAIR_RE.findall(cell or ""))


def check_constraints(rows):
    """Problems where a ``constraints`` cell contradicts or drops its recorded URL.

    Only rows that record a URL are compared, and only for the parameters in
    :data:`CONSTRAINT_PARAMETERS`. Prose in the cell (``deadline …``), unit
    suffixes (``maxPreTransitTime=60 s``) and rows without a URL (derived or
    constructed bodies) are left alone: the check polices the constraint column,
    not the wording around it.
    """
    problems = []
    for row in rows:
        recorded = recorded_url_constraints(row.get("query_direction"))
        if not recorded:
            continue
        stated = stated_constraints(row.get("constraints"))
        for name, value in recorded.items():
            if name not in stated:
                problems.append(
                    f"{row['fixture']}: constraints omits {name}={value} "
                    f"recorded in the query_direction URL"
                )
            elif stated[name] != value:
                problems.append(
                    f"{row['fixture']}: constraints says {name}={stated[name]} "
                    f"but the recorded URL says {name}={value}"
                )
    return problems


def verify_manifest(fixtures_dir=None):
    """Return a list of integrity problems; an empty list means the manifest holds.

    Checks, in the order the specification states them: every file has a row,
    every row has a file, every row's ``content_sha256`` matches the file's
    uncompressed bytes, the ``kind`` label agrees with the filename, observed
    pass-through rows are byte-identical to their recorded source hash, the
    gzip storage rule was applied by recorded size, and every ``constraints``
    cell agrees with the request URL recorded in the same row
    (:func:`check_constraints`).
    """
    directory = fixtures_dir or FIXTURES_DIR
    problems = []
    rows = load_manifest(directory)
    by_name = {}
    for row in rows:
        name = row["fixture"]
        if name in by_name:
            problems.append(f"duplicate manifest row: {name}")
        by_name[name] = row

    on_disk = {
        name
        for name in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, name)) and name != MANIFEST_NAME
    }
    for name in sorted(on_disk - set(by_name)):
        problems.append(f"unlisted fixture file: {name}")
    for name in sorted(set(by_name) - on_disk):
        problems.append(f"manifest row without a file: {name}")

    for name, row in sorted(by_name.items()):
        if name not in on_disk:
            continue
        path = os.path.join(directory, name)
        raw = open(path, "rb").read()
        logical = gzip.decompress(raw) if name.endswith(".gz") else raw
        actual = sha256_bytes(logical)
        if row["content_sha256"] != actual:
            problems.append(f"{name}: content_sha256 {row['content_sha256']} != {actual}")
        kind = row["kind"]
        if kind not in ("observed", "synthetic"):
            problems.append(f"{name}: unknown kind {kind!r}")
        if kind == "synthetic":
            if not name.startswith("synthetic_"):
                problems.append(f"{name}: synthetic row lacking the synthetic_ prefix")
            if row["source_sha256"] != "n/a":
                problems.append(f"{name}: synthetic row must record source_sha256 n/a")
        else:
            if name.startswith("synthetic_"):
                problems.append(f"{name}: observed row carries the synthetic_ prefix")
            if name not in DERIVED_OBSERVED and row["source_sha256"] != actual:
                problems.append(
                    f"{name}: observed pass-through row is not byte-identical "
                    f"(source {row['source_sha256']} != fixture {actual})"
                )
        if len(logical) > GZIP_THRESHOLD_BYTES and not name.endswith(".json.gz"):
            problems.append(f"{name}: {len(logical)} bytes exceeds 300 KB but is not gzipped")
        if name.endswith(".json.gz") and len(logical) <= GZIP_THRESHOLD_BYTES:
            problems.append(f"{name}: gzipped although {len(logical)} bytes is under 300 KB")
        for column in ("captured_utc", "query_direction", "constraints", "asserts", "source"):
            if not row[column]:
                problems.append(f"{name}: empty {column}")
    problems.extend(check_constraints(rows))
    return problems


#: Token-shaped strings that must never appear in a fixture or test. Written as
#: constants so the scan is auditable; this module is excluded from its own scan
#: because the patterns themselves are the only place these literals occur.
TOKEN_PATTERNS = (
    r"(?i)authorization\s*:",
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}",
    r"(?i)set-cookie\s*:",
    r"(?i)\bcookie\s*:\s*\S",
    r"(?i)api[_-]?key\s*[=:]",
    r"(?i)client[_-]?secret",
    r"(?i)private[_-]?key",
    r"(?i)pb_state",
    r"ghp_[A-Za-z0-9]{20,}",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"sk-[A-Za-z0-9]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}",
)

_SCANNER_FILES = frozenset({os.path.abspath(__file__)})


def scan_for_tokens(root=None):
    """Return ``(path, pattern, line_number)`` hits for token-shaped content.

    Every regular file under ``tests/`` is scanned as text (fixtures are decoded
    after decompression so a gzipped body cannot hide a hit). The scanner's own
    module is exempt: it is the only file that contains the pattern literals.
    Compiled bytecode caches are skipped — they are not test content, and a
    ``.pyc`` of this module would otherwise "find" its own pattern table.
    """
    base = root or os.path.join(REPO_ROOT, "tests")
    hits = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name != "__pycache__"]
        for filename in sorted(filenames):
            if filename.endswith((".pyc", ".pyo")):
                continue
            path = os.path.abspath(os.path.join(dirpath, filename))
            if path in _SCANNER_FILES:
                continue
            try:
                if filename.endswith(".gz"):
                    text = gzip.decompress(open(path, "rb").read()).decode("utf-8", "replace")
                else:
                    text = open(path, "rb").read().decode("utf-8", "replace")
            except OSError:
                continue
            for pattern in TOKEN_PATTERNS:
                for match in re.finditer(pattern, text):
                    line_number = text.count("\n", 0, match.start()) + 1
                    hits.append((os.path.relpath(path, base), pattern, line_number))
    return hits


__all__ = [
    "REPO_ROOT",
    "FIXTURES_DIR",
    "MANIFEST_COLUMNS",
    "CONSTRAINT_PARAMETERS",
    "fixture_path",
    "fixture_names",
    "read_bytes",
    "read_text",
    "load",
    "load_manifest",
    "recorded_url_constraints",
    "stated_constraints",
    "check_constraints",
    "verify_manifest",
    "scan_for_tokens",
    "fixture_content_sha",
    "sha256_bytes",
]
