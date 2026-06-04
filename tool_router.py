"""
Translator LLM client for voice-driven tool calling.

This module turns a VRChat human's spoken reply into the OpenAI tool-calling
protocol, without ever executing anything locally:

  - match_wake_word(transcript)   -> split a spoken wake word (pure string op)
  - translate_intent(intent, tools) -> structured tool_calls via a real LLM
  - tool_results_text(messages)   -> raw trailing tool-result text
  - summarize_tool_results(messages) -> short spoken-friendly result for the human

The translator is any OpenAI-compatible /chat/completions endpoint configured via
TOOL_LLM_* in config. We reuse `requests` (already a dependency); callers run
these from async code with asyncio.to_thread.
"""
import json
import logging
import uuid

import requests

from config import (
    TOOL_WAKE_WORDS,
    TOOL_LLM_BASE_URL,
    TOOL_LLM_API_KEY,
    TOOL_LLM_MODEL,
    TOOL_LLM_TIMEOUT,
    TOOL_LLM_TOOL_CHOICE,
    CONTINUE_TOOL_NAME,
    CONTINUE_STOP_WORDS,
)

logger = logging.getLogger(__name__)

# Leading separators to drop between the wake word and the actual intent.
_WAKE_SEPARATORS = " \t,，、:：。.-—~"

_TRANSLATE_SYSTEM = (
    "You are an intent-to-tool-call translator. The user message is what a real "
    "person said in natural language describing an action they want performed. "
    "Pick the single most appropriate tool from the available tools and issue a "
    "tool call whose arguments match the user's intent as closely as possible. "
    "Issue only the tool call; do not output any extra text."
)

# Summary rules: prefer fidelity over brevity, only compress / truncate when the
# 120-char budget forces it, and report how much was dropped.
_SUMMARY_SYSTEM = (
    "You relay a tool call's execution result to a person who reads it in a "
    "VRChat chatbox. Output in English, at most 120 characters total. Preserve "
    "the original formatting and content as faithfully as possible. Only condense "
    "non-essential parts if the result does not fit in 120 characters. If it "
    "still does not fit, drop content from the end and append a short note of how "
    "much was dropped, e.g. '...(truncated 3 files)'. Do not use code blocks."
)


def _llm_ready() -> bool:
    return bool(TOOL_LLM_BASE_URL and TOOL_LLM_MODEL)


def _post(payload: dict) -> dict:
    """POST to the translator's /chat/completions and return parsed JSON.

    On an HTTP error, include the response body in the raised error so the
    underlying provider message (e.g. an invalid-tools 400) is visible in logs.
    """
    url = f"{TOOL_LLM_BASE_URL}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if TOOL_LLM_API_KEY:
        headers["Authorization"] = f"Bearer {TOOL_LLM_API_KEY}"
    resp = requests.post(url, headers=headers, json=payload, timeout=TOOL_LLM_TIMEOUT)
    if resp.status_code >= 400:
        body = (resp.text or "").strip().replace("\n", " ")[:600]
        raise RuntimeError(f"HTTP {resp.status_code} from {url}: {body}")
    return resp.json()


def _sanitize_tools(tools: list) -> list:
    """Normalize caller tools to the exact OpenAI shape providers expect.

    Strict providers (e.g. DeepSeek) 400 on missing `function.parameters` or on
    unexpected extra fields. Keep only name/description/parameters and ensure a
    valid (possibly empty) JSON-schema object for parameters.
    """
    cleaned: list[dict] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        params = fn.get("parameters")
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
        new_fn = {"name": fn["name"], "parameters": params}
        if fn.get("description"):
            new_fn["description"] = fn["description"]
        cleaned.append({"type": "function", "function": new_fn})
    return cleaned


def _content_text(content) -> str:
    """Flatten an OpenAI message content (string or content-parts) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


# ----------------------------------------------------------------------------
# wake word (pure string logic — no LLM)
# ----------------------------------------------------------------------------
def safe_stream_prefix_len(text: str) -> int:
    """How many leading chars of `text` are safe to stream as plain content.

    A streaming caller must not emit any trailing run that could still grow into
    a wake word on the next delta. Only the longest suffix of `text` that is a
    *proper prefix* of some wake word needs to be held back; everything before it
    can never become a wake word, so it streams immediately. Ordinary text
    (whose tail matches no wake-word prefix) is fully emittable.
    """
    if not text:
        return 0
    lowered = text.lower()
    hold = 0
    for word in TOOL_WAKE_WORDS:
        w = word.lower()
        # Proper prefixes only: a full match is handled by find_wake_word.
        for k in range(min(len(w) - 1, len(lowered)), hold, -1):
            if lowered.endswith(w[:k]):
                hold = k
                break
    return len(text) - hold


def find_wake_word(transcript: str) -> tuple[int, str] | None:
    """Locate the first wake word anywhere in the transcript.

    Returns (index, matched_word) for the earliest occurrence, or None when no
    wake word is present. Case-insensitive; lower() preserves length for the
    relevant characters, so slicing the original by index/length is safe.
    """
    if not transcript:
        return None
    lowered = transcript.lower()
    best_idx: int | None = None
    best_word = ""
    for word in TOOL_WAKE_WORDS:
        idx = lowered.find(word.lower())
        if idx != -1 and (best_idx is None or idx < best_idx):
            best_idx = idx
            best_word = word
    if best_idx is None:
        return None
    return best_idx, best_word


def split_intent(transcript: str, index: int, word: str) -> tuple[str, str]:
    """Split a transcript around a located wake word into (preamble, intent)."""
    preamble = transcript[:index].strip()
    intent = transcript[index + len(word):].lstrip(_WAKE_SEPARATORS).strip()
    return preamble, intent


def match_wake_word(transcript: str) -> tuple[str, str] | None:
    """Split a transcript at the first wake word, anywhere in the sentence.

    Returns (preamble, intent):
      - preamble: text before the wake word — returned as a normal assistant
        message (may be empty when the wake word is at the start).
      - intent:   text after the wake word — the tool-call intent.
    Returns None when no wake word is present.
    """
    found = find_wake_word(transcript)
    if found is None:
        return None
    return split_intent(transcript, *found)


# ----------------------------------------------------------------------------
# continue loop (no-op tool call to keep an agent's tool loop alive)
# ----------------------------------------------------------------------------
def contains_stop_word(text: str) -> bool:
    """True when the transcript contains a loop-breaking stop word."""
    if not text:
        return False
    lowered = text.lower()
    return any(word.lower() in lowered for word in CONTINUE_STOP_WORDS)


def _is_continue_name(name: str) -> bool:
    """Match the continue tool by exact name or by an agent-applied prefix.

    Agents often namespace MCP tools (e.g. opencode exposes a server's tool as
    ``<server>_<tool>``), so match the bare name or any ``*_<tool>`` suffix.
    """
    if not name:
        return False
    return name == CONTINUE_TOOL_NAME or name.endswith("_" + CONTINUE_TOOL_NAME)


def resolve_continue_tool_name(tools: list) -> str:
    """Find the continue tool's *advertised* name in the caller's tools list.

    Agents namespace MCP tools (e.g. opencode exposes the server's tool as
    ``<server>_continue_session``). We must emit the exact name the agent knows
    or it can't execute the call, so prefer the advertised name; fall back to the
    configured bare name when the tool isn't in the list.
    """
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") or {}
        name = fn.get("name") or tool.get("name") or ""
        if _is_continue_name(name):
            return name
    return CONTINUE_TOOL_NAME


def make_continue_call(name: str = CONTINUE_TOOL_NAME) -> dict:
    """A well-formed, no-argument tool_call invoking the continue tool."""
    return {
        "id": "call_" + uuid.uuid4().hex[:24],
        "type": "function",
        "function": {"name": name or CONTINUE_TOOL_NAME, "arguments": "{}"},
    }


def is_continue_tool_result(messages: list) -> bool:
    """True when the trailing tool message(s) answer a continue_session call.

    Looks up the function names of the most recent assistant ``tool_calls`` and
    checks whether the trailing ``role:"tool"`` results belong to the continue
    tool, so we re-prompt the human instead of relaying the no-op output.
    """
    if not messages or messages[-1].get("role") != "tool":
        return False

    # Names of the most recent assistant tool_calls, keyed by call id.
    id_to_name: dict[str, str] = {}
    last_call_names: list[str] = []
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for call in msg["tool_calls"]:
                fn = (call or {}).get("function") or {}
                name = fn.get("name") or ""
                if call.get("id"):
                    id_to_name[call["id"]] = name
                last_call_names.append(name)
            break

    # Trailing tool results.
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            break
        call_id = msg.get("tool_call_id")
        if call_id and call_id in id_to_name:
            if _is_continue_name(id_to_name[call_id]):
                return True
        elif msg.get("name") and _is_continue_name(msg["name"]):
            # Some clients echo the tool name on the result message itself.
            return True

    # Fallback: no id linkage, but the only call made was the continue tool.
    if not id_to_name and last_call_names:
        return all(_is_continue_name(n) for n in last_call_names)
    return False


# ----------------------------------------------------------------------------
# intent -> tool_calls
# ----------------------------------------------------------------------------
def _normalize_tool_calls(raw_calls: list) -> list:
    """Coerce LLM tool_calls into well-formed OpenAI tool_call objects."""
    normalized: list[dict] = []
    for call in raw_calls or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        arguments = fn.get("arguments", "{}")
        if not isinstance(arguments, str):
            # Some servers return an object; serialize it.
            try:
                arguments = json.dumps(arguments, ensure_ascii=False)
            except Exception:
                arguments = "{}"
        normalized.append(
            {
                "id": call.get("id") or ("call_" + uuid.uuid4().hex[:24]),
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        )
    return normalized


def translate_intent(intent_text: str, tools: list) -> dict:
    """Translate a natural-language intent into tool_calls (or fall back to text).

    Returns either:
      {"type": "tool_calls", "tool_calls": [...]}
      {"type": "text", "content": "..."}
    """
    fallback = {"type": "text", "content": intent_text}
    if not intent_text or not _llm_ready():
        return fallback

    safe_tools = _sanitize_tools(tools)
    if not safe_tools:
        return fallback

    payload = {
        "model": TOOL_LLM_MODEL,
        "messages": [
            {"role": "system", "content": _TRANSLATE_SYSTEM},
            {"role": "user", "content": intent_text},
        ],
        "tools": safe_tools,
        "tool_choice": TOOL_LLM_TOOL_CHOICE,
        "temperature": 0,
    }
    try:
        data = _post(payload)
    except Exception as error:
        # Some models (e.g. DeepSeek thinking mode) only accept tool_choice
        # "auto"; retry once with "auto" if the provider rejected the choice.
        if payload["tool_choice"] != "auto" and "tool_choice" in str(error).lower():
            logger.info("Provider rejected tool_choice=%s; retrying with 'auto'.", payload["tool_choice"])
            payload["tool_choice"] = "auto"
            try:
                data = _post(payload)
            except Exception as retry_error:
                logger.warning("Tool intent translation failed: %s", retry_error)
                return fallback
        else:
            logger.warning("Tool intent translation failed: %s", error)
            return fallback

    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as error:
        logger.warning("Unexpected translator response shape: %s", error)
        return fallback

    normalized = _normalize_tool_calls(message.get("tool_calls"))
    if normalized:
        return {"type": "tool_calls", "tool_calls": normalized}

    # No tool call produced — relay any text, else the raw intent.
    content = _content_text(message.get("content")).strip()
    return {"type": "text", "content": content or intent_text}


# ----------------------------------------------------------------------------
# tool results -> spoken-friendly summary for the human
# ----------------------------------------------------------------------------
def _collect_trailing_tool_results(messages: list) -> tuple[str, str]:
    """Return (results_text, calls_context) from the tail tool messages."""
    tool_msgs: list[dict] = []
    for msg in reversed(messages or []):
        if msg.get("role") == "tool":
            tool_msgs.append(msg)
        else:
            break
    tool_msgs.reverse()
    results_text = "\n".join(_content_text(m.get("content")) for m in tool_msgs).strip()

    calls_context = ""
    for msg in reversed(messages or []):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            parts = []
            for call in msg["tool_calls"]:
                fn = (call or {}).get("function") or {}
                parts.append(f"{fn.get('name', '?')}({fn.get('arguments', '')})")
            calls_context = "; ".join(parts)
            break
    return results_text, calls_context


def tool_results_text(messages: list) -> str:
    """Raw trailing tool-result text, verbatim (used when summary is disabled).

    Long results are paged + cycled by the chatbox rotator just like any long
    prompt, so no truncation is applied here.
    """
    results_text, _ = _collect_trailing_tool_results(messages)
    return results_text or "(no tool output)"


def summarize_tool_results(messages: list) -> str:
    """Summarize trailing tool results into a short English prompt for the human."""
    results_text, calls_context = _collect_trailing_tool_results(messages)
    fallback = (results_text[:120] or "(no tool output)").strip()
    if not results_text or not _llm_ready():
        return fallback

    user_content = (
        f"Tool call(s) just executed: {calls_context or '(unknown)'}\n"
        f"Execution result:\n{results_text}"
    )
    payload = {
        "model": TOOL_LLM_MODEL,
        "messages": [
            {"role": "system", "content": _SUMMARY_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
    }
    try:
        data = _post(payload)
        summary = _content_text(data["choices"][0]["message"].get("content")).strip()
        return summary or fallback
    except Exception as error:
        logger.warning("Tool result summarization failed: %s", error)
        return fallback
