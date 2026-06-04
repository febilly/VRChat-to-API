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

## Floating Overlay Window

On startup an always-on-top overlay window (Tkinter, no extra deps) shows in real time:

- **Status**: `● 监听中` (green) / `● 空闲` (gray) — whether it is currently listening
- **发送 (Sent)**: the message sent to the VRChat chatbox for this request
- **识别 (Recognition)**: live transcription (confirmed text in white, the tentative hypothesis in gray)
- **回复 (Reply)**: the final content returned for the request (marked ⏹ when an endpoint was detected)

The window is draggable; click `✕` (top-right) to close it (closing quits the app).
Set `SHOW_OVERLAY=false` to run fully headless.

## File Structure

| File | Description / Role |
|------|-------------|
| `config.py` | Configuration (reads from environment variables) |
| `soniox_client.py` | Temporary/Permanent key acquisition + STT configuration |
| `audio_router.py` | Seamlessly rotated audio routing + silence detection (ported) |
| `audio_capture.py` | Loopback / microphone / mix capture (ported) |
| `stt_engine.py` | On-demand STT engine + seamless stream rotation + event publishing |
| `osc_sender.py` | OSC chatbox sender + page carousel |
| `capture.py` | Single-request reply capture and end determination |
| `api_server.py` | FastAPI OpenAI-compatible endpoints |
| `overlay.py` | Always-on-top overlay window (live recognition + status) |
| `main.py` | Entry point |

## Notes

- `ten-vad` is an optional dependency (for more accurate voice activity detection). If it is missing, the system automatically falls back to energy-based silence detection, without affecting functionality.
- Since physically there is only one real human, requests are processed serially using a global lock.
