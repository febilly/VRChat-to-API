"""
Soniox client — API key acquisition (permanent or ephemeral) and STT config.

Ported from the realtime-subtitle reference project. The temp-key logic is kept
verbatim (env var first, otherwise fetch from a dispenser URL with optional
headers). get_config is trimmed to pure recognition (no translation).
"""
import os
import json
import requests

from config import (
    SONIOX_TEMP_KEY_URL,
    LANGUAGE_HINTS,
    ENABLE_SPEAKER_DIARIZATION,
    SAMPLE_RATE,
)


def _get_temp_key_request_headers() -> dict | None:
    """Read optional temp-key request headers from env.

    Expected format:
    SONIOX_TEMP_KEY_HEADERS={"Authorization":"Bearer xxx","X-Token":"yyy"}
    """
    raw = os.environ.get("SONIOX_TEMP_KEY_HEADERS", "").strip()
    if not raw:
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid SONIOX_TEMP_KEY_HEADERS JSON: {e}")

    if not isinstance(parsed, dict):
        raise RuntimeError("SONIOX_TEMP_KEY_HEADERS must be a JSON object")

    headers = {}
    for key, value in parsed.items():
        header_name = str(key).strip()
        header_value = str(value).strip()
        if header_name and header_value:
            headers[header_name] = header_value

    return headers or None


def get_api_key() -> str:
    """
    Get an API key.
    1. Try the SONIOX_API_KEY environment variable (permanent key).
    2. Otherwise fetch a temporary key from SONIOX_TEMP_KEY_URL.
    """
    api_key = os.environ.get("SONIOX_API_KEY")
    if api_key:
        return api_key

    if not SONIOX_TEMP_KEY_URL:
        raise RuntimeError("No SONIOX_API_KEY and no SONIOX_TEMP_KEY_URL configured")

    try:
        headers = _get_temp_key_request_headers()
        request_kwargs = {"timeout": 10}
        if headers:
            request_kwargs["headers"] = headers

        response = requests.get(SONIOX_TEMP_KEY_URL, **request_kwargs)
        response.raise_for_status()

        temp_key = response.text.strip()
        if temp_key:
            return temp_key
        raise RuntimeError("Temporary key response is empty")

    except requests.RequestException as e:
        raise RuntimeError(f"Failed to fetch temporary API Key: {e}")
    except Exception as e:
        raise RuntimeError(f"Failed to parse temporary API Key: {e}")


def get_config(api_key: str, audio_format: str = "pcm_s16le") -> dict:
    """Build the Soniox STT session config (pure recognition, no translation)."""
    config = {
        "api_key": api_key,
        "model": "stt-rt-v4",
        "language_hints": list(LANGUAGE_HINTS),
        "enable_language_identification": True,
        "enable_speaker_diarization": bool(ENABLE_SPEAKER_DIARIZATION),
        "enable_endpoint_detection": True,
    }

    if audio_format == "auto":
        config["audio_format"] = "auto"
    elif audio_format == "pcm_s16le":
        config["audio_format"] = "pcm_s16le"
        config["sample_rate"] = int(SAMPLE_RATE)
        config["num_channels"] = 1
    else:
        raise ValueError(f"Unsupported audio_format: {audio_format}")

    return config
