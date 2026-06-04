#!/usr/bin/env python3
"""
continue_session — a deliberately empty MCP tool.

This is the "no-op" half of VRChat-to-API's continue loop. An agent (opencode,
Claude Code, ...) drives a tool loop: it calls the model, and as long as the
model answers with tool_calls it executes them and calls the model again. A
plain text answer ends the turn.

VRChat-to-API exploits that: after a human's spoken turn it can answer with a
tool_calls reply that invokes THIS tool. The agent executes it (we just return
"continue"), posts the result back to the model endpoint, and VRChat-to-API
re-prompts the human for the next turn — an infinite, human-paced loop. The tool
itself does nothing and touches nothing; all the real work is the round trip.

Transport: MCP stdio (newline-delimited JSON-RPC 2.0). Zero dependencies — it
speaks the protocol directly so it runs under any Python 3.8+ with nothing to
install. Logs go to stderr so they never corrupt the stdout JSON stream.
"""
import json
import sys

SERVER_NAME = "vrchat-continue"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2025-06-18"

# The single no-op tool. Takes no arguments; always returns "continue".
TOOL = {
    "name": "continue_session",
    "description": (
        "Continue the session: keep the loop alive and hand control back so the "
        "human can give the next instruction. Takes no arguments and has no side "
        "effects — call it whenever the turn should continue rather than end."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


def _log(message: str) -> None:
    print(f"[continue_session] {message}", file=sys.stderr, flush=True)


def _send(message: dict) -> None:
    """Write one JSON-RPC message as a single line to stdout."""
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _handle(message: dict) -> None:
    method = message.get("method")
    req_id = message.get("id")
    is_request = req_id is not None  # notifications have no id -> no reply

    if method == "initialize":
        params = message.get("params") or {}
        protocol = params.get("protocolVersion") or DEFAULT_PROTOCOL
        _result(
            req_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
        return

    if method in ("notifications/initialized", "initialized"):
        return  # notification: nothing to answer

    if method == "ping":
        if is_request:
            _result(req_id, {})
        return

    if method == "tools/list":
        _result(req_id, {"tools": [TOOL]})
        return

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        if name not in (TOOL["name"],):
            _error(req_id, -32602, f"unknown tool: {name!r}")
            return
        # The whole point: do nothing, just acknowledge so the loop continues.
        _result(
            req_id,
            {"content": [{"type": "text", "text": "continue"}], "isError": False},
        )
        return

    # Unknown method. Answer requests with an error; ignore stray notifications.
    if is_request:
        _error(req_id, -32601, f"method not found: {method}")


def main() -> None:
    _log("started (stdio); exposing tool 'continue_session'")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            _log(f"ignoring non-JSON line: {error}")
            continue
        try:
            _handle(message)
        except Exception as error:  # never let one bad message kill the server
            _log(f"error handling message: {error}")
    _log("stdin closed; exiting")


if __name__ == "__main__":
    main()
