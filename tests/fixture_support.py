"""Checked-in synthetic test bodies and their manifest verifier.

The suite is self-contained. Every API-shaped body it reads is a small, durable
JSON file under ``tests/fixtures/`` that was **constructed for the tests** -- not
captured from the live service, not a copy of a production response, and not
derived from one. The values in those files exist to exercise the contracts in
``tests/``; where a test asserts a numeric vector, the body encodes that vector
directly so the assertion stays legible beside its input.

Two rules this module enforces:

1. There is no environment variable and no external corpus path. The fixtures
   directory is resolved from this file's own location, so the suite behaves the
   same whatever the working directory or host time zone is. A missing required
   file raises ``FileNotFoundError`` naming the checked-in path; it never skips.
2. Every fixture is read afresh from disk. A caller may mutate the returned
   object freely; no other call sees that mutation, and no module-level cache can
   turn a test into a consumer of another test's edits.

``tests/fixtures/README.md`` is the manifest: one row per file, each recording
the kind (always ``synthetic``), the file's SHA-256 and what the body is for.
:func:`verify_manifest` fails when the files and rows disagree, when a hash
drifts, or when a body claims a provenance it cannot have.
"""

import hashlib
import json
import os
import re
import sys

REPO_ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")
MANIFEST_NAME = "README.md"

MANIFEST_COLUMNS = ("fixture", "kind", "content_sha256", "purpose")
SYNTHETIC_KIND = "synthetic"
SYNTHETIC_PREFIX = "synthetic_"

# The production package lives at the repository root. pytest inserts only the
# test file's own directory for a non-package test tree, so put the root on the
# path here: importing this helper before ``transit_core`` is enough for every
# test module, whatever interpreter drives pytest.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


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
    with open(fixture_path(name), "rb") as handle:
        return handle.read()


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
    """Parse the manifest table into a list of row dicts.

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
        row["content_sha256"] = row["content_sha256"].strip("`").strip()
        rows.append(row)
    return rows


def verify_manifest(fixtures_dir=None):
    """Return a list of integrity problems; an empty list means the manifest holds.

    Checks: every file has a row and every row a file, every row's
    ``content_sha256`` matches the file's bytes, every row is labelled
    ``synthetic`` and carries the ``synthetic_`` filename prefix, every row has
    a stated purpose, and no body contains a provenance claim it cannot support.
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
        with open(path, "rb") as handle:
            raw = handle.read()
        actual = sha256_bytes(raw)
        if row["content_sha256"] != actual:
            problems.append(f"{name}: content_sha256 {row['content_sha256']} != {actual}")
        kind = row["kind"]
        if kind != SYNTHETIC_KIND:
            problems.append(f"{name}: kind {kind!r} is not {SYNTHETIC_KIND!r}")
        if not name.startswith(SYNTHETIC_PREFIX):
            problems.append(f"{name}: fixture filename lacks the {SYNTHETIC_PREFIX} prefix")
        if not row["purpose"].strip():
            problems.append(f"{name}: empty purpose")
        text = raw.decode("utf-8", "replace")
        for claim in PROVENANCE_CLAIMS:
            if claim in text.lower():
                problems.append(f"{name}: body makes a provenance claim ({claim!r})")
    return problems


#: Substrings that would assert real-world provenance inside a fixture body. The
#: corpus is constructed, so a body may never describe itself as captured or
#: observed. (The manifest's own prose is not scanned; it states the opposite.)
PROVENANCE_CLAIMS = (
    "captured",
    "recorded response",
    "live response",
    "production response",
)

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

_SCANNER_FILES = frozenset({os.path.realpath(__file__)})


def scan_for_tokens(root=None):
    """Return ``(path, pattern, line_number)`` hits for token-shaped content.

    Every regular file under ``tests/`` is scanned as text. The scanner's own
    module is exempt: it is the only file that contains the pattern literals.
    Compiled bytecode caches are skipped.
    """
    base = root or os.path.join(REPO_ROOT, "tests")
    hits = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name != "__pycache__"]
        for filename in sorted(filenames):
            if filename.endswith((".pyc", ".pyo")):
                continue
            path = os.path.realpath(os.path.join(dirpath, filename))
            if path in _SCANNER_FILES:
                continue
            try:
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
    "fixture_path",
    "fixture_names",
    "read_bytes",
    "read_text",
    "load",
    "load_manifest",
    "verify_manifest",
    "scan_for_tokens",
    "fixture_content_sha",
    "sha256_bytes",
]
