#!/usr/bin/env python3
"""Stateless 2026-07-28 era conformance suite — card A.1 (11 era + 7 regression).
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

# ── Card T06: ping, notifications, catch-all, §1 loop-killer regressions ──

def test_ping_answered_with_empty_result_and_loop_survives():
    p = start()
    try:
        r = rpc(p, {"jsonrpc":"2.0","id":9,"method":"ping"})
        assert r["id"] == 9 and r["result"] == {}             # §6: copy the {} form
        assert p.poll() is None
    finally: p.kill(); p.wait()      # GREEN today (ping branch exists — VERIFIED server.py:512): anti-regression pin

def test_notifications_swallowed_without_phantom_response():
    p = start()
    try:
        for m in ("notifications/initialized","notifications/cancelled"):
            p.stdin.write(json.dumps({"jsonrpc":"2.0","method":m})+"\n")
        p.stdin.flush()
        r = rpc(p, {"jsonrpc":"2.0","id":11,"method":"tools/list"})
        assert r["id"] == 11          # correlation: any phantom id-less reply breaks this
        assert p.poll() is None
    finally: p.kill(); p.wait()       # GREEN today (notifications/ branch pass-through, VERIFIED :511)

def test_known_method_notifications_are_silent_before_next_response():
    p = start()
    try:
        requests = (
            {"jsonrpc":"2.0","method":"server/discover"},
            {"jsonrpc":"2.0","method":"tools/list"},
            {"jsonrpc":"2.0","method":"tools/call","params":{"name":"no_such_tool_xyz","arguments":{}}},
            {"jsonrpc":"2.0","method":"ping"},
        )
        for rid, notification in enumerate(requests, start=31):
            p.stdin.write(json.dumps(notification)+"\n")
            p.stdin.flush()
            r = rpc(p, {"jsonrpc":"2.0","id":rid,"method":"tools/list"})
            assert r["id"] == rid     # any synthetic id-null response would be read first
            assert "result" in r
    finally: p.kill(); p.wait()

def test_era_absent_method_gets_32601():
    p = start()
    try:
        r = rpc(p, {"jsonrpc":"2.0","id":12,"method":"resources/list"})
        assert r["error"]["code"] == -32601                   # §6 bullet 3: no such capability
    finally: p.kill(); p.wait()       # GREEN today via legacy catch-all; pin guards T09–T10 rewrites

def test_non_dict_json_line_is_skipped_not_fatal():
    p = start()                       # legacy: req.get on int/None/list → AttributeError → dies
    try:
        for junk in ('5', 'null', '"str"', '[1,2]'):
            p.stdin.write(junk + "\n")
        p.stdin.flush()
        r = rpc(p, {"jsonrpc":"2.0","id":13,"method":"tools/list"})
        assert r["id"] == 13 and "result" in r
    finally: p.kill(); p.wait()       # RED today (server-killer regression pin)

def test_non_string_method_routes_as_unknown_not_crash():
    p = start()                       # legacy: None.startswith → AttributeError
    try:
        r = rpc(p, {"jsonrpc":"2.0","id":14,"method":None})
        assert r["error"]["code"] == -32601
        assert p.poll() is None
    finally: p.kill(); p.wait()       # RED today (server-killer regression pin)

# ── Card T07: §7 binary G→D→G→L + §1 id-less silence ──

def test_binary_garbage_line_does_not_kill_the_server():
    # REFERENCE §1 reconfigure + §7 G→D→G→L; legacy lacks errors="replace" → RED
    discover_line = json.dumps({"jsonrpc":"2.0","id":1,"method":"server/discover"})
    tools_line    = json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/list"})
    p = subprocess.Popen([PROD_PY, SERVER], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=ROOT)
    try:
        p.stdin.write(b"\xff\xfe\x00garbage\n")                     # G — invalid UTF-8 first
        p.stdin.write(discover_line.encode() + b"\n"); p.stdin.flush()      # D
        resp = json.loads(p.stdout.readline())
        assert resp["id"] == 1 \
               and resp["result"]["supportedVersions"] == ["2026-07-28"]  # §7 skeleton-verbatim (UNCONDITIONAL)
        p.stdin.write(b"\x00\xff\n")                                # G — mid-stream garbage
        p.stdin.write(tools_line.encode() + b"\n"); p.stdin.flush()         # L
        resp2 = json.loads(p.stdout.readline())                     # id==2 ⇒ no phantom reply to G
        assert resp2["id"] == 2 and "result" in resp2
        assert p.poll() is None
        p.stdin.close(); assert p.wait(timeout=5) == 0              # clean EOF exit (§7)
    finally:
        if p.poll() is None: p.kill(); p.wait()

def test_id_less_unknown_request_gets_no_response():
    # §1: no `id` ⇒ notification — even unknown methods must NOT get an error reply
    p = start()
    try:
        p.stdin.write(json.dumps({"jsonrpc":"2.0","method":"definitely/not/a/method"})+"\n")
        p.stdin.flush()
        r = rpc(p, {"jsonrpc":"2.0","id":21,"method":"tools/list"})
        assert r["id"] == 21            # legacy answers the phantom with "id": null → r["id"]==None ⇒ RED
    finally: p.kill(); p.wait()
