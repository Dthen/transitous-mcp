"""Process-byte checks for the native CLI frontend.

These cases run a real child interpreter (the module form, ``python -m
transitous_cli``) from outside the checkout with ``PYTHONPATH`` naming this
candidate.  They assert actual stdout/stderr bytes and exit status for the
transport-free management/usage/input-file paths, stream faults, a broken pipe
and genuine SIGINT delivery.  The installed console-script form is exercised by
the artifact harness against the wheel; this file owns the source-module bytes.

No production test switch is used: the only patches are test-owned bootstrap
modules written into ``tmp_path`` and loaded before ``transitous_cli.cli.main``.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import fixture_support
import pytest

REPO = fixture_support.REPO_ROOT
PY = sys.executable


def _env(**extra):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": REPO,
        "TZ": "UTC",
    }
    env.update(extra)
    return env


def run_module(args, *, stdin=subprocess.DEVNULL, timeout=60, env=None, cwd="/"):
    return subprocess.run(
        [PY, "-B", "-m", "transitous_cli", *args],
        input=stdin if isinstance(stdin, (str, bytes)) else None,
        stdin=None if isinstance(stdin, (str, bytes)) else stdin,
        capture_output=True,
        text=False,
        timeout=timeout,
        cwd=cwd,
        env=_env() if env is None else env,
    )


def test_module_form_help_and_version_are_plain_management_text():
    help_run = run_module(["--help"])
    assert help_run.returncode == 0
    assert help_run.stdout.startswith(b"usage: transitous")
    assert help_run.stderr == b""
    assert b"--json" in help_run.stdout

    version = run_module(["--version"])
    assert version.returncode == 0
    assert version.stdout == b"transitous 3.0.0\n"
    assert version.stderr == b""

    sub = run_module(["plan", "--help"])
    assert sub.returncode == 0
    assert sub.stdout.startswith(b"usage: transitous plan")
    assert b"--continuation" in sub.stdout


def test_module_form_usage_error_is_human_on_stderr_and_json_on_stdout():
    human = run_module([])
    assert human.returncode == 2 and human.stdout == b""
    assert b"COMMAND" in human.stderr

    machine = run_module(["--json", "trip", "--nope"])
    assert machine.returncode == 2
    assert machine.stderr == b""
    document = json.loads(machine.stdout)
    assert set(document) == {"error", "outcome", "error_type", "error_details"}
    assert document["outcome"] == "cli_error" and document["error_type"] == "usage"
    assert not document["error"].startswith("usage:")
    assert machine.stdout.count(b"\n") == 1


def test_module_form_numeric_and_tuple_syntax_errors_are_usage_errors():
    for args in (["nearby-stops", "nan", "0", "--json"],
                 ["departures", "s", "--count", "1.5", "--json"],
                 ["one-to-many", "--from", "1,2,3", "--destination", "0,0", "--json"]):
        done = run_module(args)
        assert done.returncode == 2, args
        assert json.loads(done.stdout)["error_type"] == "usage"
        assert done.stderr == b""


def test_module_form_bad_continuation_is_one_input_file_diagnostic_without_body_leak(tmp_path):
    secret = "DO-NOT-ECHO-THIS-BODY"
    path = tmp_path / "bad.json"
    path.write_text('{"cursor": "' + secret + '", "query": {}} trailing', encoding="utf-8")

    machine = run_module(["plan", "--continuation", str(path), "--json"])
    assert machine.returncode == 2
    assert machine.stderr == b""
    assert machine.stdout.count(b"\n") == 1
    document = json.loads(machine.stdout)
    assert document["error_type"] == "input_file"
    assert secret.encode() not in machine.stdout

    human = run_module(["plan", "--continuation", str(path)])
    assert human.returncode == 2 and human.stdout == b""
    assert secret.encode() not in human.stderr


def test_module_form_missing_continuation_and_stdin_object_rule(tmp_path):
    missing = run_module(["plan", "--continuation", str(tmp_path / "absent.json"), "--json"])
    assert missing.returncode == 2
    assert json.loads(missing.stdout)["error_type"] == "input_file"

    for body in (b"null", b"[]", b"\xef\xbb\xbf{}", b'{"cursor":"c","cursor":"d","query":{}}'):
        done = run_module(["plan", "--continuation", "-", "--json"], stdin=body)
        assert done.returncode == 2, body
        assert json.loads(done.stdout)["error_type"] == "input_file"


BOOTSTRAP = """\
import json, os, sys
sys.path.insert(0, os.environ["CLI_REPO"])
import transitous_cli.cli as cli
{prefix}
raise SystemExit(cli.main([{argv}]))
"""


def _bootstrap(tmp_path, name, prefix, argv):
    path = tmp_path / name
    path.write_text(
        BOOTSTRAP.replace("{prefix}", prefix).replace("{argv}", ", ".join(repr(a) for a in argv)),
        encoding="utf-8",
    )
    return str(path)


def test_missing_stdout_is_an_output_failure_not_a_traceback(tmp_path):
    child = _bootstrap(
        tmp_path, "no_stdout.py",
        "sys.stdout = None\ntransit_core_stub = None\n", ["health", "--json"],
    )
    # `health` would reach the network; the fake transport answers before that.
    text = open(child, encoding="utf-8").read().replace(
        "transit_core_stub = None",
        "import transit_core.http as _h\n_h.api_get = lambda *a, **k: {'status': 'pass'}",
    )
    open(child, "w", encoding="utf-8").write(text)
    done = subprocess.run([PY, "-B", child], capture_output=True, text=True, cwd="/",
                          env=_env(CLI_REPO=REPO), timeout=60)
    assert done.returncode == 1
    assert done.stdout == ""
    assert "Traceback" not in done.stderr


def test_broken_pipe_does_not_append_a_document_or_print_a_traceback():
    process = subprocess.Popen(
        [PY, "-B", "-m", "transitous_cli", "--help"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        cwd="/", env=_env(),
    )
    process.stdout.close()
    stderr = process.stderr.read()
    code = process.wait(timeout=60)
    assert code == 1
    assert b"Traceback" not in stderr


def test_non_utf8_stdout_still_carries_the_escaped_json_document(tmp_path):
    child = _bootstrap(
        tmp_path, "ascii_out.py",
        "import transit_core.http as _h\n_h.api_get = lambda *a, **k: {'name': 'Ayr \\u00e9'}",
        ["health", "--json"],
    )
    done = subprocess.run(
        [PY, "-B", child], capture_output=True, cwd="/",
        env=_env(CLI_REPO=REPO, PYTHONIOENCODING="ascii"), timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.decode("ascii"))["name"] == "Ayr \u00e9"
    assert done.stderr == b""


def test_real_sigint_before_output_exits_130_with_no_document(tmp_path):
    child = _bootstrap(
        tmp_path, "slow.py",
        "import time\nimport transit_core.http as _h\n"
        "def _block(*a, **k):\n    time.sleep(30)\n_h.api_get = _block",
        ["health", "--json"],
    )
    process = subprocess.Popen(
        [PY, "-B", child], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, cwd="/", env=_env(CLI_REPO=REPO),
    )
    time.sleep(1.0)
    process.send_signal(signal.SIGINT)
    out, err = process.communicate(timeout=60)
    assert process.returncode == 130
    # Nothing was emitted to stdout: the only document is the local diagnostic,
    # which for a pre-emission interruption is the contract's cli_error shape.
    assert out.count(b"\n") == 1
    document = json.loads(out)
    assert document["outcome"] == "cli_error" and document["error_type"] == "interrupted"
    assert b"Traceback" not in err


def test_real_sigint_after_emission_leaves_exactly_one_document(tmp_path):
    child = _bootstrap(
        tmp_path, "kill_after_write.py",
        textwrap.dedent(
            """\
            import os, signal
            import transit_core.http as _h
            _h.api_get = lambda *a, **k: {"status": "pass"}
            class Killer:
                def __init__(self, real):
                    self._real = real
                def write(self, text):
                    written = self._real.write(text)
                    self._real.flush()
                    os.kill(os.getpid(), signal.SIGINT)
                    return written
                def flush(self):
                    return self._real.flush()
            sys.stdout = Killer(sys.stdout)
            """
        ),
        ["health", "--json"],
    )
    done = subprocess.run([PY, "-B", child], capture_output=True, cwd="/",
                          env=_env(CLI_REPO=REPO), timeout=60)
    assert done.returncode == 130, done.stderr
    assert json.loads(done.stdout) == {"status": "pass"}
    assert done.stdout.count(b"\n") == 1
    assert b"Traceback" not in done.stderr


# ── closed descriptors after startup (F1/F2/F3) and stderr SIGINT (F4) ──
#
# Each case is a real child interpreter.  Descriptors are closed AFTER startup
# (distinct from the missing-`sys.stdout = None` case above, which never opens a
# descriptor).  The fake GET boundary writes a marker before answering, so a case
# that must not reach the backend proves it by the marker's absence rather than by
# a payload shape.

_CHILD_HEADER = """\
import json, os, signal, sys
sys.path.insert(0, os.environ["CLI_REPO"])
import transit_core.http as _http

CALLS = []

def _fake_get(path, params=None, *, transport=None, base=None):
    CALLS.append(path)
    with open(os.environ["CALLS_PATH"], "w", encoding="utf-8") as handle:
        json.dump(CALLS, handle)
    return json.loads(os.environ["PAYLOAD"])

_http.api_get = _fake_get
import transitous_cli.cli as _cli
"""


def _stream_child(tmp_path, name, action, argv):
    path = tmp_path / name
    path.write_text(
        _CHILD_HEADER + action + "\nraise SystemExit(_cli.main(" + repr(argv) + "))\n",
        encoding="utf-8",
    )
    return str(path)


def _run_child(child, calls_path, payload):
    return subprocess.run(
        [PY, "-B", child], capture_output=True, text=False, cwd="/",
        env=_env(CLI_REPO=REPO, CALLS_PATH=str(calls_path),
                 PAYLOAD=json.dumps(payload)),
        timeout=60,
    )


_ERROR_BODY = {"error": "synthetic marker", "outcome": "invalid_input",
               "error_type": "unknown_mode", "error_details": {}}


def _assert_clean_shutdown(stderr):
    assert b"Traceback" not in stderr
    assert b"Exception ignored" not in stderr
    assert b"KeyboardInterrupt" not in stderr


def test_closed_stdout_after_startup_management_exits_1_quietly(tmp_path):
    calls = tmp_path / "calls.json"
    child = _stream_child(tmp_path, "closed_stdout_version.py", "os.close(1)\n",
                          ["--version"])
    done = _run_child(child, calls, {})
    assert done.returncode == 1, done
    assert done.stdout == b""
    _assert_clean_shutdown(done.stderr)


def test_closed_stdout_after_startup_operation_exits_1_without_document(tmp_path):
    calls = tmp_path / "calls.json"
    child = _stream_child(tmp_path, "closed_stdout_health.py", "os.close(1)\n",
                          ["health", "--json"])
    done = _run_child(child, calls, {"status": "pass"})
    assert done.returncode == 1, done
    assert done.stdout == b""
    assert calls.exists()  # the backend did run; only the output failed
    _assert_clean_shutdown(done.stderr)


def test_closed_stderr_keeps_usage_status_2_quietly(tmp_path):
    calls = tmp_path / "calls.json"
    child = _stream_child(tmp_path, "closed_stderr_usage.py", "os.close(2)\n", [])
    done = _run_child(child, calls, {})
    assert done.returncode == 2, done
    assert done.stdout == b""
    _assert_clean_shutdown(done.stderr)


def test_closed_stderr_keeps_backend_error_status_quietly(tmp_path):
    calls = tmp_path / "calls.json"
    child = _stream_child(tmp_path, "closed_stderr_error.py", "os.close(2)\n",
                          ["health"])
    done = _run_child(child, calls, _ERROR_BODY)
    assert done.returncode == 2, done
    assert done.stdout == b""
    _assert_clean_shutdown(done.stderr)


def test_closed_stdin_descriptor_continuation_is_input_file_2(tmp_path):
    calls = tmp_path / "calls.json"
    child = _stream_child(tmp_path, "closed_stdin_fd.py", "os.close(0)\n",
                          ["plan", "--continuation", "-", "--json"])
    done = _run_child(child, calls, {})
    assert done.returncode == 2, done
    assert not calls.exists(), "a local stdin failure must not reach the backend"
    assert done.stderr == b""
    document = json.loads(done.stdout)
    assert document["outcome"] == "cli_error"
    assert document["error_type"] == "input_file"
    assert done.stdout.count(b"\n") == 1


def test_closed_stdin_object_continuation_is_input_file_2(tmp_path):
    calls = tmp_path / "calls.json"
    child = _stream_child(tmp_path, "closed_stdin_object.py", "sys.stdin.close()\n",
                          ["plan", "--continuation", "-", "--json"])
    done = _run_child(child, calls, {})
    assert done.returncode == 2, done
    assert not calls.exists()
    assert json.loads(done.stdout)["error_type"] == "input_file"
    _assert_clean_shutdown(done.stderr)


_STDERR_KILLER = """\
_REAL_STDERR = sys.stderr

class _Killer:
    def __init__(self, real):
        self._real = real

    def write(self, text):
        written = self._real.write(text)
        self._real.flush()
        os.kill(os.getpid(), signal.SIGINT)
        return written

    def flush(self):
        return self._real.flush()

sys.stderr = _Killer(sys.stderr)
"""


def test_real_sigint_during_human_backend_error_diagnostic_exits_130(tmp_path):
    calls = tmp_path / "calls.json"
    child = _stream_child(tmp_path, "sigint_error_stderr.py", _STDERR_KILLER,
                          ["health"])
    done = _run_child(child, calls, _ERROR_BODY)
    assert done.returncode == 130, done
    assert done.stdout == b""
    assert b"invalid_input" in done.stderr
    _assert_clean_shutdown(done.stderr)


def test_real_sigint_during_warning_emission_keeps_one_document_and_130(tmp_path):
    calls = tmp_path / "calls.json"
    child = _stream_child(tmp_path, "sigint_warning_stderr.py", _STDERR_KILLER,
                          ["health"])
    body = {"status": "pass", "warnings": [{"code": "w", "message": "m"}]}
    done = _run_child(child, calls, body)
    assert done.returncode == 130, done
    # Human mode prints one pretty-JSON document; no replacement was appended.
    assert json.loads(done.stdout) == body
    assert b"warning:" in done.stderr
    _assert_clean_shutdown(done.stderr)


def test_deeply_nested_continuation_is_input_file_2_not_a_traceback(tmp_path):
    """Q1: decoder recursion is a local input error in both output modes."""
    depth = 10000
    payload = b'{"cursor": "c", "query": ' + b"[" * depth + b"]" * depth + b"}"
    path = tmp_path / "deep.json"
    path.write_bytes(payload)

    machine = run_module(["plan", "--continuation", str(path), "--json"])
    assert machine.returncode == 2, machine
    assert machine.stderr == b""
    assert machine.stdout.count(b"\n") == 1
    document = json.loads(machine.stdout)
    assert document["outcome"] == "cli_error"
    assert document["error_type"] == "input_file"
    assert b"Traceback" not in machine.stdout
    assert b"[[[[" not in machine.stdout
    assert len(machine.stdout) < 1000

    human = run_module(["plan", "--continuation", "-"], stdin=payload)
    assert human.returncode == 2, human
    assert human.stdout == b""
    assert b"not valid JSON" in human.stderr
    assert b"Traceback" not in human.stderr
    assert b"[[[[" not in human.stderr
    assert len(human.stderr) < 2000
