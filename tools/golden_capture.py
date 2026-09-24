#!/usr/bin/env python3
"""Capture the pre-migration Transitous MCP tool surface."""

import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "server.py"
OUT = REPO / "golden" / "transitous.tools.json"
PROD_PY = "/usr/bin/python3"


def rpc(process, payload):
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        raise RuntimeError("server closed stdout before responding")
    return json.loads(line)


def main():
    process = subprocess.Popen(
        [PROD_PY, str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=REPO,
    )
    try:
        initialize = rpc(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        if "error" in initialize and initialize["error"].get("code") != -32601:
            raise RuntimeError(f"initialize failed: {initialize['error']}")
        response = rpc(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        if "error" in response:
            raise RuntimeError(f"tools/list failed: {response['error']}")
        tools = response["result"]["tools"]
        if len(tools) != 13:
            raise ValueError(f"expected 13 tools, got {len(tools)}")
        OUT.write_text(json.dumps(tools, indent=2) + "\n")
        print(f"captured {len(tools)} tools -> {OUT}")
    finally:
        if process.stdin and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    main()
