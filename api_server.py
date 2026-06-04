"""
OpenAI-compatible API server.

POST /v1/chat/completions takes the last user message, sends it to the VRChat
chatbox (paged + rotated for long text), then captures the spoken reply via the
STT engine and returns it as the assistant message. Supports streaming
(confirmed deltas) and non-streaming.

Listening is on-demand: the STT stream + audio capture start when a request
arrives and stop when it finishes, so nothing is captured (and no temp-key
stream sits idle) between requests.

Requests are serialized with a global lock — physically there's only one human.
"""
import asyncio
import json
import time
import uuid
import logging

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from config import (
    MODEL_NAME,
    CAPTURE_MIN_SILENCE_SECONDS,
    CAPTURE_MAX_WAIT_SECONDS,
    CAPTURE_NO_REPLY_MESSAGE,
    CAPTURE_SILENCE_FALLBACK_SECONDS,
    ENGINE_START_TIMEOUT_SECONDS,
)
from capture import CaptureSession
from osc_sender import ChatboxRotator

logger = logging.getLogger(__name__)

app = FastAPI(title="VRChat-to-API")

_engine = None
_sender = None
_status_cb = None
_request_lock = asyncio.Lock()


def configure(engine, sender, status_cb=None) -> None:
    global _engine, _sender, _status_cb
    _engine = engine
    _sender = sender
    _status_cb = status_cb


def _emit_status(event: dict) -> None:
    """Push a UI status event (request_start / request_end) to the overlay, if any."""
    if _status_cb is not None:
        try:
            _status_cb(event)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def _last_user_message(messages: list) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            # OpenAI content parts: join text segments.
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            return "".join(parts).strip()
    return ""


def _completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def _completion_payload(model: str, text: str, cid: str, created: int) -> dict:
    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": len(text),
            "total_tokens": len(text),
        },
    }


def _chunk(cid: str, created: int, model: str, delta: dict, finish_reason) -> str:
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _new_capture(loop: asyncio.AbstractEventLoop) -> CaptureSession:
    return CaptureSession(
        _engine,
        loop,
        min_silence=CAPTURE_MIN_SILENCE_SECONDS,
        max_wait=CAPTURE_MAX_WAIT_SECONDS,
        no_reply_message=CAPTURE_NO_REPLY_MESSAGE,
        silence_fallback=CAPTURE_SILENCE_FALLBACK_SECONDS,
    )


async def _begin_listening(prompt: str, loop: asyncio.AbstractEventLoop):
    """Start the STT stream on demand, send the prompt, open a capture.

    Caller must hold _request_lock. Returns (capture, rotator) or (None, None)
    if the STT stream failed to come up in time.
    """
    await asyncio.to_thread(_engine.start)
    ready = await asyncio.to_thread(_engine.healthy.wait, ENGINE_START_TIMEOUT_SECONDS)
    if not ready:
        await asyncio.to_thread(_engine.stop)
        return None, None
    rotator = ChatboxRotator(_sender, prompt)
    rotator.start()
    capture = _new_capture(loop)
    capture.start()
    return capture, rotator


def _end_listening(capture, rotator) -> None:
    # All steps are quick and non-awaiting (engine.stop only signals), so this
    # can't be interrupted by request cancellation — guaranteeing teardown +
    # lock release even when the client disconnects right after [DONE].
    if capture is not None:
        capture.close()
    if rotator is not None:
        rotator.stop()
    _engine.stop()


# ----------------------------------------------------------------------------
# endpoints
# ----------------------------------------------------------------------------
@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": MODEL_NAME, "object": "model", "created": 0, "owned_by": "vrchat"}
        ],
    }


@app.get("/health")
async def health():
    listening = bool(_engine and _engine.is_running())
    return {
        "status": "ok",
        "listening": listening,
        "last_disconnect_reason": getattr(_engine, "last_disconnect_reason", None),
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if _engine is None or _sender is None:
        raise HTTPException(status_code=503, detail="server not initialized")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    prompt = _last_user_message(body.get("messages", []))
    if not prompt:
        raise HTTPException(status_code=400, detail="no user message found")

    model = body.get("model") or MODEL_NAME
    stream = bool(body.get("stream", False))
    loop = asyncio.get_running_loop()
    created = int(time.time())
    cid = _completion_id()

    logger.info("Request -> chatbox: %r (stream=%s)", prompt[:80], stream)

    if stream:
        async def event_stream():
            await _request_lock.acquire()
            capture = rotator = None
            _emit_status({"type": "request_start", "prompt": prompt})
            reply_parts: list[str] = []
            try:
                capture, rotator = await _begin_listening(prompt, loop)
                if capture is None:
                    reason = getattr(_engine, "last_disconnect_reason", None) or "timeout"
                    yield _chunk(cid, created, model, {"role": "assistant"}, None)
                    yield _chunk(cid, created, model, {"content": f"[STT 未就绪: {reason}]"}, None)
                    yield _chunk(cid, created, model, {}, "stop")
                    yield "data: [DONE]\n\n"
                    return
                yield _chunk(cid, created, model, {"role": "assistant"}, None)
                async for delta in capture.stream():
                    reply_parts.append(delta)
                    yield _chunk(cid, created, model, {"content": delta}, None)
                yield _chunk(cid, created, model, {}, "stop")
                yield "data: [DONE]\n\n"
            finally:
                # Nested finally: release the lock no matter what (even if the
                # task is cancelled when the client disconnects after [DONE]).
                try:
                    _emit_status({"type": "request_end", "reply": "".join(reply_parts)})
                    _end_listening(capture, rotator)
                finally:
                    _request_lock.release()

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    async with _request_lock:
        _emit_status({"type": "request_start", "prompt": prompt})
        text = ""
        capture, rotator = await _begin_listening(prompt, loop)
        if capture is None:
            _emit_status({"type": "request_end", "reply": ""})
            reason = getattr(_engine, "last_disconnect_reason", None) or "timeout"
            raise HTTPException(status_code=503, detail=f"STT stream failed to start: {reason}")
        try:
            text = await capture.wait_complete()
        finally:
            _emit_status({"type": "request_end", "reply": text})
            _end_listening(capture, rotator)

    logger.info("Reply <- VRChat: %r", text[:80])
    return JSONResponse(_completion_payload(model, text, cid, created))
