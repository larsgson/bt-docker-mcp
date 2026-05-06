"""MCP HTTP transport — JSON-RPC 2.0 over POST /mcp.

Hand-rolled JSON-RPC. The MCP spec is small enough that a custom handler is
~150 lines, gives full control over auth/CORS, and avoids version coupling
with any third-party MCP SDK.

Methods supported:
  initialize        protocol handshake
  tools/list        catalog of registered tools
  tools/call        invoke a tool with arguments
  ping              liveness
  notifications/*   acknowledged silently
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request

from server.deps import get_db
from server.mcp.tools import call_tool, list_tools

router = APIRouter()

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "bt-docker-mcp", "version": "2.0.0"}


@router.post("/mcp")
async def mcp_endpoint(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Any:
    """JSON-RPC 2.0 entrypoint."""
    try:
        body = await request.json()
    except Exception:
        return _err(None, -32700, "Parse error: invalid JSON")

    if isinstance(body, list):
        # Batch request
        return [_handle_one(msg, db) for msg in body]
    return _handle_one(body, db)


@router.get("/mcp")
def mcp_get_info() -> dict:
    """Discovery / liveness for clients that probe before POSTing."""
    return {
        "protocol": "MCP / JSON-RPC 2.0",
        "transport": "Streamable HTTP",
        "protocolVersion": PROTOCOL_VERSION,
        "server": SERVER_INFO,
        "endpoint": "POST /mcp with a JSON-RPC envelope",
        "methods": ["initialize", "tools/list", "tools/call", "ping"],
    }


# ---------- core dispatcher ----------

def _handle_one(msg: dict, db: sqlite3.Connection) -> dict:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(msg.get("id") if isinstance(msg, dict) else None,
                    -32600, "Invalid Request: jsonrpc must be '2.0'")

    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    # Notifications: no id, no response. We acknowledge by returning an empty dict.
    is_notification = "id" not in msg
    if is_notification and method and method.startswith("notifications/"):
        return {}  # silently consumed

    if method == "initialize":
        return _ok(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })

    if method == "ping":
        return _ok(msg_id, {})

    if method == "tools/list":
        return _ok(msg_id, {"tools": list_tools()})

    if method == "tools/call":
        name = params.get("name")
        if not name:
            return _err(msg_id, -32602, "Invalid params: 'name' is required")
        arguments = params.get("arguments") or {}
        try:
            result = call_tool(name, arguments, db)
        except ValueError as e:
            return _err(msg_id, -32602, f"Tool error: {e}")
        except Exception as e:
            return _err(msg_id, -32603, f"Internal error: {type(e).__name__}: {e}")
        # Per MCP: result has `content[]` (array of TextContent / ImageContent etc.)
        # We return JSON-as-text for maximum client compatibility, plus
        # `structuredContent` for clients that support it.
        text = json.dumps(result, ensure_ascii=False, indent=2)
        return _ok(msg_id, {
            "content": [{"type": "text", "text": text}],
            "structuredContent": result,
            "isError": False,
        })

    return _err(msg_id, -32601, f"Method not found: {method}")


# ---------- envelope helpers ----------

def _ok(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
