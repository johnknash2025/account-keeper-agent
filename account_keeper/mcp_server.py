"""依存ゼロの最小 MCP サーバー（stdio / JSON-RPC 2.0）。

Hermes Agent・Claude Code・その他 MCP クライアントから
`python -m account_keeper.mcp_server` で起動して使う。
"""
from __future__ import annotations

import json
import os
import sys

from . import __version__
from .store import DEFAULT_DB, Store
from .tools import build_tools

PROTOCOL_VERSION = "2025-06-18"


def handle(msg: dict, tools: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notification
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        return ok({"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "account-keeper", "version": __version__}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, **spec} for n, (spec, _) in tools.items()]})
    if method == "tools/call":
        p = msg.get("params", {})
        name, args = p.get("name"), p.get("arguments") or {}
        if name not in tools:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            result = tools[name][1](**args)
            return ok({"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]})
        except Exception as e:  # ツールエラーは isError で返す
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    store = Store(os.environ.get("ACCOUNT_KEEPER_DB", DEFAULT_DB))
    tools = build_tools(store)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line), tools)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
