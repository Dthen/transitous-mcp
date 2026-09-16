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

def test_initialize_returns_32601_and_never_hangs_or_closes():
    # D2 stateless-only: no initialize branch; catch-all answers -32601.
    # The SAME pipes must then serve discover (Hermes auto-mode fallback path).
    p = start()
    try:
        r = rpc(p, {"jsonrpc":"2.0","id":1,"method":"initialize",
                    "params":{"protocolVersion":"2024-11-05","capabilities":{},
                              "clientInfo":{"name":"probe","version":"0"}}})
        assert r["id"] == 1 and r["error"]["code"] == -32601
        assert isinstance(r["error"]["message"], str) and r["error"]["message"]
        d = discover(p, rid=2)                       # liveness on same pipes
        assert d["id"] == 2 and d["result"]["supportedVersions"] == [ERA]
        assert p.poll() is None                      # never exit on a rejected method
    finally:
        p.kill(); p.wait()

def _tools_list(p, rid=3):
    return rpc(p, {"jsonrpc":"2.0","id":rid,"method":"tools/list"})["result"]

def test_tools_list_carries_the_triple():            # REFERENCE §4 triple + §1 pinned
    p = start()
    try:
        res = _tools_list(p)
        assert res["resultType"] == "complete" and res["ttlMs"] == 0 \
               and res["cacheScope"] == "private"
        assert "nextCursor" not in res               # unpaginated: omit
    finally: p.kill(); p.wait()

def test_tools_list_declares_no_outputSchema():      # §4 trap: a declared schema makes
    p = start()                                      # validate_tool_result require
    try:                                             # structuredContent on EVERY call
        for t in _tools_list(p)["tools"]:
            assert "outputSchema" not in t
    finally: p.kill(); p.wait()

def test_tools_list_byte_identical_to_golden():      # D4 freeze — UNCONDITIONAL load:
    with open(os.path.join(ROOT, "golden", "transitous.tools.json")) as f:
        golden = json.load(f)                        # no skipif/exists-guard: a missing
    p = start()                                      # golden is a defect, not a skip
    try:
        tools = _tools_list(p)["tools"]
        assert len(tools) == len(golden) == 13
        for g, a in zip(golden, tools):
            for key in ("name", "description", "inputSchema"):
                assert json.dumps(g[key], sort_keys=True) == \
                       json.dumps(a[key], sort_keys=True)
            assert "outputSchema" not in a
    finally: p.kill(); p.wait()

def _call(p, name, arguments=None, rid=4):
    return rpc(p, {"jsonrpc":"2.0","id":rid,"method":"tools/call",
        "params":{"name":name,"arguments":arguments or {}}})

def test_tools_call_result_carries_the_triple():
    p = start()
    try:
        r = _call(p, "transit_one_to_many",
                  {"from_lat": 51.5, "from_lon": -0.12, "destinations": []})
        res = r["result"]                          # tool-level errors are RESULTS
        assert res["resultType"] == "complete" and res["ttlMs"] == 0 \
               and res["cacheScope"] == "private"
        assert isinstance(res["content"], list) and res["content"][0]["type"] == "text"
    finally: p.kill(); p.wait()

def test_tools_call_unknown_tool_is_error_result_not_crash():
    p = start()
    try:
        res = _call(p, "no_such_tool_xyz")["result"]
        assert res.get("isError") is True          # legacy lacks it → RED (pin flips green in T09)
        assert "Unknown tool" in res["content"][0]["text"]
        assert "structuredContent" not in res      # §4 trap: never emit
    finally: p.kill(); p.wait()

def test_tools_call_missing_params_is_jsonrpc_32602():
    p = start()
    try:
        r = rpc(p, {"jsonrpc":"2.0","id":5,"method":"tools/call"})   # no params at all
        assert r["error"]["code"] == -32602        # legacy KeyErrors → -32603 → RED
        assert "result" not in r
    finally: p.kill(); p.wait()

def test_tools_call_non_string_name_is_32602():
    p = start()
    try:
        r = rpc(p, {"jsonrpc":"2.0","id":6,"method":"tools/call",
                    "params":{"name": 42}})
        assert r["error"]["code"] == -32602
    finally: p.kill(); p.wait()
