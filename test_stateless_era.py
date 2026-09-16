#!/usr/bin/env python3
"""Stateless 2026-07-28 era conformance suite — card A.1 (11 era + 6 regression).
Subprocess-driven against ./server.py via /usr/bin/python3; network-free."""
import json, os, select, subprocess, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(ROOT, "server.py")
PROD_PY = "/usr/bin/python3"          # production interpreter (REFERENCE §7)
ERA = "2026-07-28"

def start():
    return subprocess.Popen([PROD_PY, SERVER], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=ROOT)

def read_line_with_timeout(f, sec):
    """select-based deadline per read: next line, or None if nothing arrives in `sec`."""
    if not select.select([f], [], [], sec)[0]:
        return None
    return f.readline()

def rpc(p, obj):
    p.stdin.write(json.dumps(obj) + "\n"); p.stdin.flush()
    line = read_line_with_timeout(p.stdout, 15)
    assert line, f"no response within 15 s (pending method: {obj.get('method')})"
    return json.loads(line)

def discover(p, rid=1):
    return rpc(p, {"jsonrpc":"2.0","id":rid,"method":"server/discover","params":{}})

def test_discover_serves_era_strict_shape():     # REFERENCE §2
    p = start()
    try:
        r = discover(p)
        assert r["id"] == 1
        res = r["result"]
        assert res["supportedVersions"] == [ERA]
        assert res["capabilities"] == {"tools": {}}
        assert res["resultType"] == "complete" and res["ttlMs"] == 0 \
               and res["cacheScope"] == "private"                       # defensive triple, D3
    finally:
        p.kill(); p.wait()

def test_discover_works_without_params():        # §2: "may arrive with no params at all"
    p = start()
    try:
        r = rpc(p, {"jsonrpc":"2.0","id":7,"method":"server/discover"})
        assert r["id"] == 7 and r["result"]["supportedVersions"] == [ERA]
    finally:
        p.kill(); p.wait()
