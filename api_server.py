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
import random
import string
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
    ENABLE_TOOL_CALLING,
    ENABLE_TOOL_RESULT_SUMMARY,
    ENABLE_CONTINUE_LOOP,
    CONTINUE_PROMPT_EN,
    CONTINUE_PROMPT_ZH,
    CONTINUE_ON_TIMEOUT,
    OSC_TEMPLATE_LANGUAGE,
    OSC_TOOLS_LABEL_EN,
    OSC_TOOLS_LABEL_ZH,
    OSC_TOOLS_AVAILABLE_EN,
    OSC_TOOLS_AVAILABLE_ZH,
    OSC_TOOL_HINT_EN,
    OSC_TOOL_HINT_ZH,
    OSC_LISTENING_FOOTER_EN,
    OSC_LISTENING_FOOTER_ZH,
    INTERCEPT_TITLE_REQUESTS,
    TITLE_TEXT,
    TITLE_RANDOM_LEN,
    TITLE_REQUEST_PATTERNS,
)
from capture import CaptureSession
from osc_sender import ChatboxRotator, format_tools_header
from tool_router import (
    match_wake_word,
    find_wake_word,
    split_intent,
    safe_stream_prefix_len,
    translate_intent,
    summarize_tool_results,
    tool_results_text,
    contains_stop_word,
    make_continue_call,
    resolve_continue_tool_name,
    is_continue_tool_result,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="VRChat-to-API")

_engine = None
_sender = None
_status_cb = None
_request_lock = asyncio.Lock()


def _osc_template(en: str, zh: str):
    if OSC_TEMPLATE_LANGUAGE == "chinese":
        return zh
    if OSC_TEMPLATE_LANGUAGE == "rotate":
        return (en, zh)
    return en


def _osc_template_for_log(value) -> str:
    if isinstance(value, tuple):
        return value[0] if value else ""
    return value


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


def _message_text(content) -> str:
    """Flatten a message content (string or OpenAI content-parts) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _is_title_request(messages: list) -> bool:
    """Detect agent-tool background 'generate a title' requests by pattern."""
    if not INTERCEPT_TITLE_REQUESTS or not TITLE_REQUEST_PATTERNS:
        return False
    blob = " ".join(
        _message_text(m.get("content"))
        for m in (messages or [])
        if m.get("role") in ("system", "user")
    ).lower()
    if not blob:
        return False
    return any(pattern in blob for pattern in TITLE_REQUEST_PATTERNS)


def _make_title() -> str:
    """Canned title: fixed name + random suffix, e.g. 'VRChat-x7k2m9'."""
    if TITLE_RANDOM_LEN <= 0:
        return TITLE_TEXT
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(random.choices(alphabet, k=TITLE_RANDOM_LEN))
    return f"{TITLE_TEXT}-{suffix}"


def _tool_chatbox_header(tools: list):
    """Header shown above the prompt when callable tools are available."""
    tools_line_en = format_tools_header(tools, label=OSC_TOOLS_LABEL_EN) or OSC_TOOLS_AVAILABLE_EN
    tools_line_zh = format_tools_header(tools, label=OSC_TOOLS_LABEL_ZH) or OSC_TOOLS_AVAILABLE_ZH
    return _osc_template(
        f"{tools_line_en}\n{OSC_TOOL_HINT_EN}",
        f"{tools_line_zh}\n{OSC_TOOL_HINT_ZH}",
    )


def _continue_prompt(messages: list):
    """Prompt shown when the continue-loop asks for the next spoken turn."""
    original_prompt = _last_user_message(messages)
    if not original_prompt:
        return _osc_template(CONTINUE_PROMPT_EN, CONTINUE_PROMPT_ZH)
    return _osc_template(
        f"{CONTINUE_PROMPT_EN}\n{original_prompt}",
        f"{CONTINUE_PROMPT_ZH}\n{original_prompt}",
    )


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


def _tool_calls_payload(
    model: str, tool_calls: list, cid: str, created: int, content: str | None = None
) -> dict:
    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": tool_calls,
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _tool_calls_delta(tool_calls: list) -> dict:
    """Build a streaming delta carrying tool calls (with stream indices)."""
    return {
        "tool_calls": [
            {
                "index": i,
                "id": call.get("id"),
                "type": "function",
                "function": call.get("function", {}),
            }
            for i, call in enumerate(tool_calls)
        ]
    }


def _tool_call_summary(tool_calls: list) -> str:
    """Short human/overlay string describing the emitted tool calls."""
    parts = []
    for call in tool_calls:
        fn = call.get("function", {})
        parts.append(f"{fn.get('name', '?')}({fn.get('arguments', '')})")
    return "🔧 " + "; ".join(parts)


def _new_capture(loop: asyncio.AbstractEventLoop) -> CaptureSession:
    return CaptureSession(
        _engine,
        loop,
        min_silence=CAPTURE_MIN_SILENCE_SECONDS,
        max_wait=CAPTURE_MAX_WAIT_SECONDS,
        no_reply_message=CAPTURE_NO_REPLY_MESSAGE,
        silence_fallback=CAPTURE_SILENCE_FALLBACK_SECONDS,
    )


async def _begin_listening(
    prompt,
    loop: asyncio.AbstractEventLoop,
    *,
    header: str = "",
    footer: str = "",
):
    """Start the STT stream on demand, send the prompt, open a capture.

    Caller must hold _request_lock. Returns (capture, rotator) or (None, None)
    if the STT stream failed to come up in time.
    """
    await asyncio.to_thread(_engine.start)
    ready = await asyncio.to_thread(_engine.healthy.wait, ENGINE_START_TIMEOUT_SECONDS)
    if not ready:
        await asyncio.to_thread(_engine.stop)
        return None, None
    rotator = ChatboxRotator(_sender, prompt, header=header, footer=footer)
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

    messages = body.get("messages", [])
    model = body.get("model") or MODEL_NAME
    stream = bool(body.get("stream", False))
    created = int(time.time())
    cid = _completion_id()

    # --- Title-request interception (before lock / STT / human) ------------
    # Agent tools fire frequent background "title this conversation" requests;
    # answer them instantly with a canned title so they never tie up the single
    # human or the engine.
    if _is_title_request(messages):
        title = _make_title()
        logger.info("Intercepted title request -> %r (stream=%s)", title, stream)
        if stream:
            async def title_stream():
                yield _chunk(cid, created, model, {"role": "assistant"}, None)
                yield _chunk(cid, created, model, {"content": title}, None)
                yield _chunk(cid, created, model, {}, "stop")
                yield "data: [DONE]\n\n"

            return StreamingResponse(title_stream(), media_type="text/event-stream")
        return JSONResponse(_completion_payload(model, title, cid, created))

    # --- Tool calling / continue-loop setup --------------------------------
    tools = body.get("tools") or []
    tool_choice = body.get("tool_choice")
    tools_enabled = bool(ENABLE_TOOL_CALLING and tools and tool_choice != "none")
    loop_enabled = bool(ENABLE_CONTINUE_LOOP)
    # "interactive" = a reply may become a tool_calls turn (wake-word translation
    # and/or a continue_session no-op call), so the human reply must be inspected.
    interactive = tools_enabled or loop_enabled
    # Emit the continue tool under the exact name the agent advertises it as.
    continue_tool_name = resolve_continue_tool_name(tools) if loop_enabled else ""
    loop = asyncio.get_running_loop()

    # Chatbox prompt selection:
    #   - continue-loop result: the no-op tool just looped us back, so re-prompt
    #     the human for the next turn (don't relay the "continue" no-op output).
    #   - other tool results: relay them to the human — summarized if enabled,
    #     else the raw text (long results are paged + auto-cycled by the rotator).
    #   - otherwise: send the last user message as before.
    if loop_enabled and is_continue_tool_result(messages):
        prompt = _continue_prompt(messages)
    elif messages and messages[-1].get("role") == "tool":
        if ENABLE_TOOL_RESULT_SUMMARY:
            prompt = await asyncio.to_thread(summarize_tool_results, messages)
        else:
            prompt = tool_results_text(messages)
    else:
        prompt = _last_user_message(messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="no user message found")

    prompt_for_log = _osc_template_for_log(prompt)
    header = _tool_chatbox_header(tools) if tools_enabled else ""
    footer = _osc_template(OSC_LISTENING_FOOTER_EN, OSC_LISTENING_FOOTER_ZH) if interactive else ""

    logger.info(
        "Request -> chatbox: %r (stream=%s, tools=%s, loop=%s)",
        prompt_for_log[:80], stream, tools_enabled, loop_enabled,
    )

    def _should_continue(text: str) -> bool:
        """Whether a text-only reply should loop back via a continue call.

        True only when the loop is on, the reply has content, the human didn't
        say a stop word, and timeout/no-reply behavior allows continuing.
        """
        clean = (text or "").strip()
        if not (loop_enabled and clean):
            return False
        if clean == CAPTURE_NO_REPLY_MESSAGE and not CONTINUE_ON_TIMEOUT:
            return False
        return bool(
            not contains_stop_word(text)
        )

    def _maybe_continue(text: str) -> dict:
        """Wrap a plain text reply in a continue_session call to keep the loop.

        With the continue loop on, a text-only reply (which would otherwise end
        the agent's turn) becomes a tool_calls reply carrying the spoken text as
        content plus a no-op continue call.
        """
        if _should_continue(text):
            return {
                "type": "tool_calls",
                "tool_calls": [make_continue_call(continue_tool_name)],
                "content": text or None,
            }
        return {"type": "text", "content": text}

    async def _decide(text: str) -> dict:
        """Turn a buffered transcript into a text or tool_calls decision.

        A wake word may appear mid-sentence: text before it becomes the
        assistant's message (returned alongside the tool call, like a normal
        OpenAI turn), text after it becomes the tool-call intent. A text-only
        reply is routed through the continue loop (a no-op if it's disabled).
        """
        if tools_enabled:
            match = match_wake_word(text)
            if match is not None:
                preamble, intent = match
                decision = await asyncio.to_thread(translate_intent, intent, tools)
                if decision["type"] == "tool_calls":
                    decision["content"] = preamble
                    return decision
                # No tool call produced; fold the preamble back into the text.
                tail = decision.get("content", "")
                text = f"{preamble} {tail}".strip() if preamble else tail
        return _maybe_continue(text)

    async def _stream_decision(capture, rotator):
        """Stream the human reply live, switching to tool-call buffering only
        once a wake word appears mid-sentence.

        The preamble (text before the wake word) is a normal assistant message
        and is streamed as it's confirmed, holding back a short tail so a wake
        word straddling two confirmed deltas is never leaked as content. Once
        the wake word lands, the trailing intent is buffered (not streamed) and,
        at end of speech, translated into tool calls — the only inherently
        non-streamed part, since a tool call is atomic in the OpenAI protocol.

        Yields ("content", delta) for assistant text, then exactly one terminal
        ("tool_calls", calls) or ("stop", None).
        """
        full = ""
        emitted = 0          # chars of `full` already streamed as content
        found = None         # (index, word) once the wake word is located

        async for delta in capture.stream():
            full += delta
            if not tools_enabled:
                # Loop-only mode: no wake words to watch for, stream everything.
                yield ("content", delta)
                emitted = len(full)
                continue
            if found is None:
                found = find_wake_word(full)
                if found is None:
                    # Stream confirmed preamble, holding back only a trailing run
                    # that could still grow into a wake word on the next delta.
                    upto = safe_stream_prefix_len(full)
                    if upto > emitted:
                        yield ("content", full[emitted:upto])
                        emitted = upto
                    continue
                # Wake word just landed: flush any preamble up to it, then stop
                # emitting — everything after is buffered intent.
                idx = found[0]
                if idx > emitted:
                    yield ("content", full[emitted:idx])
                    emitted = idx

        # ASR done: drop the [listening] footer right away (before translation).
        rotator.set_footer("")

        # Terminal: a text-only reply either ends the turn or — with the loop on —
        # loops back via a no-op continue call (content stays as the streamed
        # text). A real wake-word tool call already keeps the agent looping.
        terminal = (
            ("tool_calls", [make_continue_call(continue_tool_name)])
            if _should_continue(full)
            else ("stop", None)
        )

        if found is None:
            # No wake word at all: flush the held-back tail as the final text.
            if len(full) > emitted:
                yield ("content", full[emitted:])
            yield terminal
            return

        _, intent = split_intent(full, *found)
        decision = await asyncio.to_thread(translate_intent, intent, tools)
        if decision["type"] == "tool_calls":
            yield ("tool_calls", decision["tool_calls"])
        else:
            tail = decision.get("content", "")
            if tail:
                yield ("content", tail)
            yield terminal

    if stream:
        async def event_stream():
            await _request_lock.acquire()
            capture = rotator = None
            _emit_status({"type": "request_start", "prompt": prompt_for_log})
            status_reply = ""
            try:
                capture, rotator = await _begin_listening(
                    prompt, loop, header=header, footer=footer
                )
                if capture is None:
                    reason = getattr(_engine, "last_disconnect_reason", None) or "timeout"
                    yield _chunk(cid, created, model, {"role": "assistant"}, None)
                    yield _chunk(cid, created, model, {"content": f"[STT 未就绪: {reason}]"}, None)
                    yield _chunk(cid, created, model, {}, "stop")
                    yield "data: [DONE]\n\n"
                    return

                yield _chunk(cid, created, model, {"role": "assistant"}, None)

                if interactive:
                    # Stream the preamble live; only the post-wake-word intent is
                    # buffered and translated into an (atomic) tool call. With the
                    # continue loop on, a text-only reply ends with a no-op call.
                    content_parts: list[str] = []
                    tcs = None
                    async for kind, data in _stream_decision(capture, rotator):
                        if kind == "content":
                            content_parts.append(data)
                            yield _chunk(cid, created, model, {"content": data}, None)
                        elif kind == "tool_calls":
                            tcs = data
                            yield _chunk(cid, created, model, _tool_calls_delta(tcs), None)
                            yield _chunk(cid, created, model, {}, "tool_calls")
                        else:  # "stop"
                            yield _chunk(cid, created, model, {}, "stop")
                    yield "data: [DONE]\n\n"
                    content = "".join(content_parts)
                    if tcs is not None:
                        status_reply = (f"{content} " if content else "") + _tool_call_summary(tcs)
                    else:
                        status_reply = content
                else:
                    parts: list[str] = []
                    async for delta in capture.stream():
                        parts.append(delta)
                        yield _chunk(cid, created, model, {"content": delta}, None)
                    yield _chunk(cid, created, model, {}, "stop")
                    yield "data: [DONE]\n\n"
                    status_reply = "".join(parts)
            finally:
                # Nested finally: release the lock no matter what (even if the
                # task is cancelled when the client disconnects after [DONE]).
                try:
                    _emit_status({"type": "request_end", "reply": status_reply})
                    _end_listening(capture, rotator)
                finally:
                    _request_lock.release()

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    async with _request_lock:
        _emit_status({"type": "request_start", "prompt": prompt_for_log})
        capture, rotator = await _begin_listening(prompt, loop, header=header, footer=footer)
        if capture is None:
            _emit_status({"type": "request_end", "reply": ""})
            reason = getattr(_engine, "last_disconnect_reason", None) or "timeout"
            raise HTTPException(status_code=503, detail=f"STT stream failed to start: {reason}")
        decision: dict = {"type": "text", "content": ""}
        try:
            text = await capture.wait_complete()
            if interactive:
                rotator.set_footer("")  # ASR done: drop [listening]
                decision = await _decide(text)
            else:
                decision = {"type": "text", "content": text}
        finally:
            if decision["type"] == "tool_calls":
                preamble = decision.get("content") or ""
                status_reply = (f"{preamble} " if preamble else "") + _tool_call_summary(decision["tool_calls"])
            else:
                status_reply = decision.get("content", "")
            _emit_status({"type": "request_end", "reply": status_reply})
            _end_listening(capture, rotator)

    if decision["type"] == "tool_calls":
        logger.info("Reply <- VRChat tool_calls: %s", status_reply[:120])
        return JSONResponse(
            _tool_calls_payload(
                model, decision["tool_calls"], cid, created, content=decision.get("content")
            )
        )

    text = decision.get("content", "")
    logger.info("Reply <- VRChat: %r", text[:80])
    return JSONResponse(_completion_payload(model, text, cid, created))
