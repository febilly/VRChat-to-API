# continue_session — the no-op MCP tool for VRChat-to-API's continue loop

A tiny, zero-dependency MCP server that exposes a single tool, **`continue_session`**,
which does nothing and returns the string `"continue"`. It exists only to keep an
agent's tool loop alive so a human can keep driving by voice.

## Why this exists

Agents like **opencode** (also Claude Code, Codex, …) run a *tool loop*:

1. call the model endpoint,
2. if the model answers with `tool_calls`, execute them and post the results back,
3. repeat — until the model answers with **plain text**, which ends the turn and
   hands control back to the *typed* user.

VRChat-to-API replaces the "model" with a real human speaking in VRChat. A normal
spoken reply is plain text, so it would end the turn after every sentence. With
the **continue loop** enabled, VRChat-to-API instead answers with a `tool_calls`
reply that carries the spoken text *and* a call to `continue_session`. The agent
executes this no-op, posts `role:"tool"` back, and VRChat-to-API re-prompts the
human for the next turn — an infinite, human-paced loop. The human ends it by
speaking a **stop word** (e.g. "结束循环"), which makes the server answer with
plain text again.

```
human speaks ──▶ VRChat-to-API ──▶ assistant{content, tool_calls:[continue_session]}
      ▲                                          │
      │                                          ▼
   chatbox: "（继续）请说下一步"  ◀── VRChat-to-API ◀── agent executes continue_session,
                                                        posts role:"tool":"continue"
```

This server is the piece the **agent** runs locally so it has something real to
execute. It never touches your machine or VRChat — all the real work is the round
trip back to VRChat-to-API.

## What it does

- Speaks **MCP over stdio** (newline-delimited JSON-RPC 2.0) directly, so it needs
  **no pip install** — any Python 3.8+ works.
- Exposes exactly one tool, `continue_session`, with an empty argument schema.
- Every call returns `{"content": [{"type": "text", "text": "continue"}]}`.
- Logs to **stderr** only (stdout is reserved for the protocol).

## Configure opencode

opencode reads MCP servers from its config (`opencode.json` in your project, or
`~/.config/opencode/opencode.json` globally). Add a **local** MCP server pointing
at `server.py` — see [`opencode.example.json`](./opencode.example.json):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "vrchat-continue": {
      "type": "local",
      "command": ["python", "/abs/path/to/mcp_continue_session/server.py"],
      "enabled": true
    }
  }
}
```

Use an **absolute path** to `server.py` (on Windows, escape backslashes: `C:\\...`,
or use forward slashes). If `python` isn't on PATH, give the full interpreter path.

> **Tool naming.** opencode namespaces MCP tools as `<server>_<tool>`, so the tool
> the model sees here is `vrchat-continue_continue_session`. You don't need to
> configure that anywhere: VRChat-to-API reads the advertised name out of each
> request's `tools` list and emits the matching name automatically. (If you rename
> the server key, no change is needed.)

### Other agents

Any MCP-capable agent works the same way — register `server.py` as a local
(stdio) MCP server. If your agent uses a *different* naming scheme and the
advertised name does **not** end in `continue_session`, set `CONTINUE_TOOL_NAME`
in VRChat-to-API's `.env` to whatever the agent advertises.

## Enable the loop in VRChat-to-API

In the project `.env`:

```ini
ENABLE_CONTINUE_LOOP=true
# Optional overrides:
# CONTINUE_TOOL_NAME=continue_session
# CONTINUE_STOP_WORDS=结束循环,停止循环,结束对话,exit loop,stop loop
# CONTINUE_PROMPT=（继续）请说下一步，或说"结束循环"停止
```

That's it. The loop works whether or not voice **tool calling**
(`ENABLE_TOOL_CALLING`) is on — `continue_session` takes no arguments, so the
translator LLM isn't involved.

## Test it standalone

You can drive the server by hand to confirm it speaks MCP:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"continue_session","arguments":{}}}' \
  | python server.py
```

You should see an `initialize` result, the tool in `tools/list`, and a
`tools/call` result whose content text is `"continue"`.

## How the loop ends

- **Stop word** — say any phrase in `CONTINUE_STOP_WORDS`; the reply is returned
  as plain text, so the agent's turn ends normally.
- **No reply** — if a turn captures no speech (the `CAPTURE_NO_REPLY_MESSAGE`
  sentinel), the server does **not** issue a continue call, so an empty room ends
  the loop instead of spinning forever.

## Safety

`continue_session` has no parameters, performs no I/O, and returns a constant
string. It cannot run code, read files, or reach the network. The only effect of
"executing" it is that the agent makes one more round trip to VRChat-to-API.
