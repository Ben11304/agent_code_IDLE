from __future__ import annotations

import json
import subprocess
import sys


def send(process: subprocess.Popen, message: dict) -> dict:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    return json.loads(process.stdout.readline())


def main() -> int:
    process = subprocess.Popen(
        [sys.executable, "-m", "openconstruction_mcp.server"],
        cwd=".",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        init = send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        tools = send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        print(json.dumps({"initialize": init, "tool_count": len(tools["result"]["tools"])}, indent=2))
    finally:
        process.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
