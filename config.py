"""
Configuration for VRChat-to-API.

Reads settings from environment / .env. Ported (and trimmed) from the
realtime-subtitle reference project: only the Soniox + audio bits are kept,
plus the new OSC chatbox and OpenAI-API server settings.
"""
import json
import os
import sys

from dotenv import load_dotenv

# Load .env so vars are available to every module importing this config.
load_dotenv()


# ----------------------------------------------------------------------------
# env helpers
# ----------------------------------------------------------------------------
def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    value = str(value).strip().lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    return default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(str(value).strip())
    except Exception:
        return default


def _env_optional_float(name: str) -> float | None:
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).strip())
    except Exception:
        print(f"⚠️  {name} is not a valid number, ignoring")
        return None


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None else str(value)


# ----------------------------------------------------------------------------
# Soniox STT
# ----------------------------------------------------------------------------
SONIOX_WEBSOCKET_URL = _env_str("SONIOX_WEBSOCKET_URL", "wss://stt-rt.soniox.com/transcribe-websocket")
SONIOX_TEMP_KEY_URL = os.environ.get("SONIOX_TEMP_KEY_URL")
# SONIOX_API_KEY / SONIOX_TEMP_KEY_HEADERS are read directly in soniox_client.

# Optional stream rollover for temporary API keys whose websocket streams have a
# hard lifetime. Set this in .env (e.g. 170) to proactively roll to a fresh stream.
_STREAM_DURATION_RAW = _env_optional_float("SONIOX_STREAM_DURATION_SECONDS")
if _STREAM_DURATION_RAW is not None and _STREAM_DURATION_RAW <= 0:
    print("⚠️  SONIOX_STREAM_DURATION_SECONDS must be greater than 0, ignoring")
    _STREAM_DURATION_RAW = None
SONIOX_STREAM_DURATION_SECONDS = _STREAM_DURATION_RAW

# Optional cost saver: close the websocket after long local silence, reopen on speech.
SONIOX_SLEEP_ON_SILENCE = _env_bool("SONIOX_SLEEP_ON_SILENCE", False)
SONIOX_SLEEP_IDLE_SECONDS = max(1.0, _env_float("SONIOX_SLEEP_IDLE_SECONDS", 30.0))
SONIOX_SLEEP_PRE_ROLL_SECONDS = max(0.0, _env_float("SONIOX_SLEEP_PRE_ROLL_SECONDS", 0.5))
SONIOX_SLEEP_SPEECH_GRACE_SECONDS = max(0.0, _env_float("SONIOX_SLEEP_SPEECH_GRACE_SECONDS", 0.25))

ENABLE_SPEAKER_DIARIZATION = _env_bool("ENABLE_SPEAKER_DIARIZATION", False)

# Soniox language hints (comma-separated ISO 639-1 codes).
_LANG_HINTS_RAW = _env_str("LANGUAGE_HINTS", "en,zh,ja,ko,ru")
LANGUAGE_HINTS = [c.strip() for c in _LANG_HINTS_RAW.split(",") if c.strip()]

# Audio format streamed to Soniox.
SAMPLE_RATE = _env_int("SAMPLE_RATE", 16000)
CHUNK_SIZE = _env_int("CHUNK_SIZE", 3840)


# ----------------------------------------------------------------------------
# Audio capture
# ----------------------------------------------------------------------------
# system | microphone | mix. Default "system" (loopback) -> only captures others.
_AUDIO_SOURCE_RAW = _env_str("AUDIO_SOURCE", "system").strip().lower()
AUDIO_SOURCE = _AUDIO_SOURCE_RAW if _AUDIO_SOURCE_RAW in ("system", "microphone", "mix") else "system"
if AUDIO_SOURCE != _AUDIO_SOURCE_RAW:
    print(f"⚠️  Invalid AUDIO_SOURCE: {_AUDIO_SOURCE_RAW}, fallback to: system")

# Optional: capture a specific output device's loopback instead of the default
# speaker (route VRChat to a dedicated/virtual device to isolate its audio).
LOOPBACK_DEVICE_NAME = _env_str("LOOPBACK_DEVICE_NAME", "").strip()

# Mix gains (only used when AUDIO_SOURCE=mix). "self"=mic, "others"=system.
MIX_OWN_VOLUME = min(1.0, max(0.0, _env_float("MIX_OWN_VOLUME", 0.5)))
MIX_OTHER_VOLUME = 1.0 - MIX_OWN_VOLUME


# ----------------------------------------------------------------------------
# VRChat OSC (chatbox)
# ----------------------------------------------------------------------------
VRCHAT_OSC_HOST = _env_str("VRCHAT_OSC_HOST", "127.0.0.1")
VRCHAT_OSC_PORT = _env_int("VRCHAT_OSC_PORT", 9000)
CHATBOX_MAX_LENGTH = 144  # VRChat chatbox hard limit (characters)

# Chatbox paging rotator: per-page dwell estimated by reading speed.
CHATBOX_CJK_CPS = max(0.1, _env_float("CHATBOX_CJK_CPS", 6.0))
CHATBOX_LATIN_CPS = max(0.1, _env_float("CHATBOX_LATIN_CPS", 15.0))
CHATBOX_MIN_PAGE_SECONDS = max(1.5, _env_float("CHATBOX_MIN_PAGE_SECONDS", 3.0))
CHATBOX_MAX_PAGE_SECONDS = _env_optional_float("CHATBOX_MAX_PAGE_SECONDS")  # optional cap


# ----------------------------------------------------------------------------
# Reply capture (when does the spoken reply "finish")
# ----------------------------------------------------------------------------
CAPTURE_MIN_SILENCE_SECONDS = max(0.0, _env_float("CAPTURE_MIN_SILENCE_SECONDS", 1.5))
CAPTURE_MAX_WAIT_SECONDS = max(1.0, _env_float("CAPTURE_MAX_WAIT_SECONDS", 45.0))
CAPTURE_NO_REPLY_MESSAGE = _env_str("CAPTURE_NO_REPLY_MESSAGE", "（对方没有回复）")
# Safety net: complete even if Soniox never fires an endpoint, once speech has
# been heard and silence has lasted this long. Prevents hanging until max_wait.
CAPTURE_SILENCE_FALLBACK_SECONDS = max(0.0, _env_float("CAPTURE_SILENCE_FALLBACK_SECONDS", 4.0))
# How long to wait for the on-demand STT stream to connect when a request starts.
ENGINE_START_TIMEOUT_SECONDS = max(1.0, _env_float("ENGINE_START_TIMEOUT_SECONDS", 12.0))


# ----------------------------------------------------------------------------
# OpenAI-compatible API server
# ----------------------------------------------------------------------------
SERVER_HOST = _env_str("SERVER_HOST", "127.0.0.1")
SERVER_PORT = _env_int("SERVER_PORT", 8080)
MODEL_NAME = _env_str("MODEL_NAME", "vrchat-human")


# ----------------------------------------------------------------------------
# Floating overlay window (always-on-top live recognition + status)
# ----------------------------------------------------------------------------
SHOW_OVERLAY = _env_bool("SHOW_OVERLAY", True)
OVERLAY_OPACITY = min(1.0, max(0.3, _env_float("OVERLAY_OPACITY", 0.92)))


# ----------------------------------------------------------------------------
# Hard validation
# ----------------------------------------------------------------------------
if not os.environ.get("SONIOX_API_KEY") and not SONIOX_TEMP_KEY_URL:
    print(
        "❌ Configuration error: neither SONIOX_API_KEY nor SONIOX_TEMP_KEY_URL is set.\n"
        "Please set one of them in your environment or in the .env file."
    )
    sys.exit(1)


def describe() -> str:
    """One-line human-readable summary for startup logging."""
    rollover = (
        f"{SONIOX_STREAM_DURATION_SECONDS:.0f}s" if SONIOX_STREAM_DURATION_SECONDS else "off"
    )
    return (
        f"audio={AUDIO_SOURCE}"
        + (f"(device={LOOPBACK_DEVICE_NAME})" if LOOPBACK_DEVICE_NAME else "")
        + f", rollover={rollover}, diarization={ENABLE_SPEAKER_DIARIZATION}, "
        f"min_silence={CAPTURE_MIN_SILENCE_SECONDS}s, max_wait={CAPTURE_MAX_WAIT_SECONDS}s"
    )
