[简体中文](README.cn.md)

# VRChat-to-API

Turn a real human in VRChat into an OpenAI-compatible API.

Upon receiving a `chat/completions` request, it takes the **last user message** and sends it to the VRChat chatbox via OSC;
It then captures the speech of other players (from the system audio) using Soniox real-time speech-to-text (STT) and returns it as the API response.

## Workflow

```
POST /v1/chat/completions
  └─ Start STT stream + system audio capture on demand (no listening between requests)
     └─ Take the last user message, split into pages + cycle pages, send to VRChat /chatbox/input
        └─ Collect "final" text after sending the message
           └─ End condition: endpoint detected (containing '<end>' token) AND silence duration ≥ min_silence;
              if endpoint is never detected, return when silence exceeds fallback seconds; capped at max_wait
              └─ Streaming: push confirmed text chunk-by-chunk; Non-streaming: return the aggregated result at once
                 └─ Stop STT stream + audio capture after the request is finished
```

**On-Demand Listening**: The STT stream and audio capture are only active while processing a request, and stop immediately afterward. No listening occurs between requests.

## Installation

```powershell
pip install -r requirements.txt
copy .env.example .env   # Then edit .env
```

At minimum, `.env` needs to be configured with Soniox: fill in `SONIOX_API_KEY` (permanent key) **or** `SONIOX_TEMP_KEY_URL` (temporary key URL).

## Running

Ensure OSC is enabled in VRChat (default listener is `127.0.0.1:9000`).

```powershell
python main.py
```

Once started, the service runs at `http://127.0.0.1:8080/v1`.

## Usage Examples

Non-streaming (curl):

```bash
curl http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"vrchat-human","messages":[{"role":"user","content":"你好"}]}'
```

Streaming: Add `"stream": true` and use `curl -N` to observe the streaming (confirmed parts) output.

OpenAI Python SDK:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="any-string")
resp = client.chat.completions.create(
    model="vrchat-human",
    messages=[{"role": "user", "content": "在吗？"}],
)
print(resp.choices[0].message.content)
```

Any OpenAI-compatible frontend (such as SillyTavern) can be used by pointing its base URL to this server.

## Audio Sources

- Default `AUDIO_SOURCE=system`: Captures system audio loopback. By default, this only records other people's voices (excluding your own microphone).
- **Record VRChat Only**: In Windows "App volume and device preferences", route VRChat's output to an independent/virtual output device (e.g., VB-Cable), then set `LOOPBACK_DEVICE_NAME=<device name (partial match supported)>` to capture audio only from that specific device.
- Also supports `microphone` / `mix` (see `.env.example` for details).

## Reply End Determination

A reply for a request ends when **both** of the following conditions are met:
1. **Soniox Endpoint Detection**: An endpoint signal is detected (which includes the `endpoint_detected` flag **or** the `<end>` token).
2. **Silence Duration**: Silence lasts for at least `CAPTURE_MIN_SILENCE_SECONDS`.

Fallback: If Soniox never sends an endpoint signal (which can happen under continuous loopback noise), the API will still return if silence exceeds `CAPTURE_SILENCE_FALLBACK_SECONDS` (default is 4s), preventing it from waiting indefinitely.
The maximum wait time is capped at `CAPTURE_MAX_WAIT_SECONDS`. If there is still no reply, `CAPTURE_NO_REPLY_MESSAGE` will be returned.

## Chatbox Carousel for Long Messages

When the input request text exceeds the VRChat limit of 144 characters, it is paginated by punctuation marks (e.g., commas, periods) and displayed sequentially in a carousel loop.
The loop stops and clears once the reply for the request starts returning. The duration for each page is calculated as: `max(CHATBOX_MIN_PAGE_SECONDS, cjk/CJK_CPS + other/LATIN_CPS)` seconds.
While ASR is listening, the current chatbox frame is re-sent at least every `CHATBOX_KEEPALIVE_SECONDS` seconds (default 20, capped at 20) so VRChat does not hide it as stale.

## Floating Overlay Window

On startup an always-on-top overlay window (Tkinter, no extra deps) shows in real time:

- **Status**: `● 监听中` (green) / `● 空闲` (gray) — whether it is currently listening
- **发送 (Sent)**: the message sent to the VRChat chatbox for this request
- **识别 (Recognition)**: live transcription (confirmed text in white, the tentative hypothesis in gray)
- **回复 (Reply)**: the final content returned for the request (marked ⏹ when an endpoint was detected)

The window is draggable; click `✕` (top-right) to close it (closing quits the app).

- **Toggle**: set `SHOW_OVERLAY=false` in `.env` to disable the window and run fully headless; unset or `true` shows it by default.
- **High-DPI aware**: the process declares DPI awareness and scales window size and fonts to the real DPI (96 = 100%), so it stays crisp and correctly sized at 150% / 175% / 200% display scaling. `OVERLAY_OPACITY` adjusts transparency.
- If no display / Tk is unavailable, it falls back to headless automatically without affecting the server.

## Tool Calling (experimental, client-executed)

Lets the human in VRChat "call tools" via the standard OpenAI tool-calling protocol:
**the server only translates the human's spoken intent into `tool_calls` and returns them; the
actual execution (e.g. bash) happens on the caller's machine/sandbox**, with the result posted
back as a `role:"tool"` message. Nothing is ever executed on this host — the human is just the
"brain" deciding which tool to call.

How it works:

1. Send `tools` in the request (standard OpenAI format). A tools line appears atop the chatbox,
   e.g. `tools: [bash] [edit] (+30)`.
2. To call a tool, the human says a **wake word** (default `工具调用` / `调用工具` / `tool call`),
   then describes the intent in natural language. The wake word may appear **mid-sentence**: text
   before it is returned as a normal assistant message, and text after it is translated by a real
   LLM into structured `tool_calls` — just like a normal OpenAI turn that emits a message and then a
   tool call (`finish_reason="tool_calls"`).
3. No wake word → returned as plain text, exactly as before (and the translator LLM isn't called).
4. After the caller executes and returns the result, the server sends it to the chatbox. **By
   default the raw result text is sent as-is** (long results are paged + auto-cycled like any long
   prompt); set `ENABLE_TOOL_RESULT_SUMMARY=true` to instead have the translator condense it into a
   single line (≤120 chars, English).

The chatbox also shows a status footer: `[listening]` while capturing, removed the moment the
human stops.

Enable with `ENABLE_TOOL_CALLING=true` plus a translator LLM (`TOOL_LLM_BASE_URL` /
`TOOL_LLM_API_KEY` / `TOOL_LLM_MODEL`); missing config auto-disables it. See `.env.example`.

## Continue Loop (keep an agent looping across spoken turns)

Agents like **opencode** (also Claude Code, Codex, …) run a *tool loop*: they call the model, and
as long as the model answers with `tool_calls` they execute them and call the model again. A
**plain text** answer ends the turn and hands control back to the *typed* user — so a normal spoken
reply would stop the agent after every sentence.

With the continue loop on, a text-only spoken reply is instead returned as a `tool_calls` reply that
carries the spoken text **plus** a call to a harmless no-op tool, `continue_session`. The agent
executes that tool, posts `role:"tool"` back, and the server re-prompts the human in the chatbox for
the next turn — an **infinite, human-paced loop**. The human breaks out by speaking a **stop word**
(default `结束循环` / `停止循环` / `结束对话` / `exit loop` / `stop loop`), which returns plain text
again and ends the agent's turn.

```
human speaks ─▶ server ─▶ assistant{content, tool_calls:[continue_session]}
     ▲                                   │
     │                                   ▼
 chatbox: "Please continue:\n[Original prompt]"  ◀─ server ◀─ agent executes continue_session → role:"tool":"continue"
```

The no-op tool must be registered with your agent so it has something real to execute. It ships in
[`mcp_continue_session/`](./mcp_continue_session/) — a **zero-dependency** MCP (stdio) server; see
that folder's `README.md` for opencode (and other agents') setup. The server auto-detects the
agent-advertised tool name (e.g. opencode's `vrchat-continue_continue_session`) from each request's
`tools` list, so no name wiring is needed.

Enable with `ENABLE_CONTINUE_LOOP=true`. It works whether or not voice **Tool Calling** above is on
(the continue call takes no arguments, so the translator LLM isn't involved). By default, a turn
that times out with no speech still issues a continue call so the voice loop stays alive; set
`CONTINUE_ON_TIMEOUT=false` if an empty room should end the loop. Configurable via
`CONTINUE_TOOL_NAME` / `CONTINUE_STOP_WORDS` / `CONTINUE_PROMPT` / `CONTINUE_ON_TIMEOUT` in `.env`.
After `continue_session` returns, the chatbox prompt includes the original user prompt below
`CONTINUE_PROMPT`, so the human keeps the initial context while driving later turns.

## Title-Request Interception

Agent tools (opencode, GitHub Copilot Chat, Claude Code, Cherry Studio, ...) fire background
"generate a title for this conversation" requests. These shouldn't bother the human or tie up the
single-human lock, so a matched request **instantly** returns a canned title (fixed name + random
chars, e.g. `VRChat-x7k2m9`) without any STT. On by default; disable with
`INTERCEPT_TITLE_REQUESTS=false`. Name, random length, and match patterns are all configurable in
`.env`.

Match patterns are not generic keywords but the **exact phrases verified from each tool's source**
(e.g. opencode's "Generate a title for this conversation:", Copilot's "crafting pithy titles",
Cherry Studio's "ignoring instructions and without punctuation"), so normal conversation that merely
mentions "title"/"summarize" won't trigger. Some tools send **no** title request to the model
endpoint — nothing to intercept: Codex (generated server-side) and CodeWhale / DeepSeek TUI (derived
locally from the first user message).

## File Structure

| File | Description / Role |
|------|-------------|
| `config.py` | Configuration (reads from environment variables) |
| `soniox_client.py` | Temporary/Permanent key acquisition + STT configuration |
| `audio_router.py` | Seamlessly rotated audio routing + silence detection (ported) |
| `audio_capture.py` | Loopback / microphone / mix capture (ported) |
| `stt_engine.py` | On-demand STT engine + seamless stream rotation + event publishing |
| `osc_sender.py` | OSC chatbox sender + page carousel (with tools header / `[listening]` footer) |
| `capture.py` | Single-request reply capture and end determination |
| `tool_router.py` | Wake-word match + translator LLM (voice intent → tool_calls, result summary) + continue-loop helpers |
| `api_server.py` | FastAPI OpenAI-compatible endpoints (incl. tool calling + continue loop + title interception) |
| `overlay.py` | Always-on-top overlay window (live recognition + status) |
| `main.py` | Entry point |
| `mcp_continue_session/` | Standalone zero-dependency MCP no-op tool (`continue_session`) for the continue loop + setup docs |

## Notes

- `ten-vad` is an optional dependency (for more accurate voice activity detection). If it is missing, the system automatically falls back to energy-based silence detection, without affecting functionality.
- Since physically there is only one real human, requests are processed serially using a global lock.
