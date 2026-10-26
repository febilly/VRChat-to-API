"""
Qwen realtime ASR engine behind the same event contract as the Soniox engine.

The upper API layer does not know which provider is active. It receives only:
    {"type": "final", "text": <confirmed text>, "ts": <monotonic>}
    {"type": "partial", "text": <current hypothesis>, "ts": <monotonic>}
    {"type": "endpoint", "ts": <monotonic>}
"""
from __future__ import annotations

import base64
import logging
import os
import threading
import time
from contextlib import suppress
from typing import Any, Callable, Optional

import dashscope
from dashscope.audio.qwen_omni import (
    MultiModality,
    OmniRealtimeCallback,
    OmniRealtimeConversation,
)

try:
    from dashscope.audio.qwen_omni.omni_realtime import TranscriptionParams
except ImportError:  # pragma: no cover
    class TranscriptionParams:  # type: ignore[override]
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

from audio_capture import AudioStreamer
from config import (
    AUDIO_SOURCE,
    CHUNK_SIZE,
    QWEN_ASR_ENABLE_VAD,
    QWEN_ASR_LANGUAGE,
    QWEN_ASR_MODEL,
    QWEN_ASR_URL,
    QWEN_ASR_URL_INTERNATIONAL,
    QWEN_ASR_USE_INTERNATIONAL,
    QWEN_ASR_VAD_SILENCE_DURATION_MS,
    QWEN_ASR_VAD_THRESHOLD,
    SAMPLE_RATE,
)

logger = logging.getLogger(__name__)


def _normalize_qwen_language(language: str | None) -> str | None:
    if not language:
        return None
    normalized = language.strip().lower()
    if not normalized or normalized in ("auto", "auto-detect"):
        return None
    aliases = {
        "zh-cn": "zh",
        "zh-tw": "zh",
        "zh-hans": "zh",
        "zh-hant": "zh",
        "en-us": "en",
        "en-gb": "en",
        "ja-jp": "ja",
        "ko-kr": "ko",
    }
    return aliases.get(normalized, normalized)


class _QwenAudioTarget:
    """Websocket-like audio sink used by AudioStreamer."""

    def __init__(self, engine: "QwenSTTEngine"):
        self._engine = engine

    def send(self, data: bytes) -> None:
        self._engine.send_audio(data)


class _QwenCallback(OmniRealtimeCallback):
    def __init__(self, engine: "QwenSTTEngine"):
        self._engine = engine
        self._items: dict[str, str] = {}

    def on_open(self) -> None:  # type: ignore[override]
        logger.info("Qwen ASR websocket opened.")

    def on_close(self, code, msg) -> None:  # type: ignore[override]
        self._engine._on_connection_closed(f"connection closed: code={code}, msg={msg}")

    def on_event(self, message: dict[str, Any]) -> None:  # type: ignore[override]
        if not isinstance(message, dict):
            return
        event_type = message.get("type")
        if event_type == "input_audio_buffer.speech_stopped":
            self._engine._publish_endpoint()
        elif event_type == "conversation.item.input_audio_transcription.text":
            self._handle_transcription_text(message)
        elif event_type == "conversation.item.input_audio_transcription.completed":
            self._handle_transcription_completed(message)
        elif event_type == "conversation.item.input_audio_transcription.failed":
            self._handle_transcription_failed(message)
        elif event_type == "session.finished":
            self._engine._on_session_finished()
        elif event_type == "error":
            self._handle_error(message)

    def _clean_text(self, text: str) -> str:
        if text.startswith("<asr_text>"):
            return text[len("<asr_text>") :]
        return text

    def _handle_transcription_text(self, message: dict[str, Any]) -> None:
        item_id = str(message.get("item_id") or "")
        fixed = message.get("text") or ""
        stash = message.get("stash") or ""
        combined = self._clean_text(f"{fixed}{stash}")
        if item_id:
            self._items[item_id] = combined
        if combined:
            self._engine._publish({"type": "partial", "text": combined, "ts": time.monotonic()})

    def _handle_transcription_completed(self, message: dict[str, Any]) -> None:
        item_id = str(message.get("item_id") or "")
        transcript = message.get("transcript") or ""
        if not transcript and item_id:
            transcript = self._items.get(item_id, "")
        if item_id:
            self._items.pop(item_id, None)
        transcript = self._clean_text(transcript)
        now = time.monotonic()
        if transcript:
            self._engine._publish({"type": "final", "text": transcript, "ts": now})
        self._engine._publish({"type": "endpoint", "ts": now})

    def _handle_transcription_failed(self, message: dict[str, Any]) -> None:
        error = message.get("error") or {}
        detail = error.get("message") or "Qwen ASR transcription failed"
        code = error.get("code")
        if code:
            detail = f"{detail} (code={code})"
        self._engine._on_error(RuntimeError(detail))

    def _handle_error(self, message: dict[str, Any]) -> None:
        error = message.get("error") or {}
        detail = error.get("message") or "Qwen ASR error"
        code = error.get("code")
        if code:
            detail = f"{detail} (code={code})"
        self._engine._on_error(RuntimeError(detail))


class QwenSTTEngine:
    """Continuously transcribes captured audio through Qwen realtime ASR."""

    def __init__(self):
        self.sample_rate = int(SAMPLE_RATE)
        self.chunk_size = int(CHUNK_SIZE)
        self.audio_source = AUDIO_SOURCE

        self.stop_event: Optional[threading.Event] = None
        self.thread: Optional[threading.Thread] = None
        self.audio_streamer: Optional[AudioStreamer] = None
        self.audio_lock = threading.Lock()
        self.healthy = threading.Event()
        self.last_disconnect_reason: Optional[str] = None

        self._subscribers: list[Callable[[dict], None]] = []
        self._sub_lock = threading.Lock()
        self._conversation: Optional[OmniRealtimeConversation] = None
        self._conversation_lock = threading.Lock()
        self._stopping = False

    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        with self._sub_lock:
            self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._sub_lock:
                with suppress(ValueError):
                    self._subscribers.remove(callback)

        return _unsubscribe

    def _publish(self, event: dict) -> None:
        with self._sub_lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception as error:
                logger.warning("STT subscriber raised: %s", error)

    def _publish_endpoint(self) -> None:
        self._publish({"type": "endpoint", "ts": time.monotonic()})

    def start(self) -> None:
        old = self.thread
        if old and old.is_alive() and old is not threading.current_thread():
            if self.stop_event:
                self.stop_event.set()
            old.join(timeout=8.0)
            if old.is_alive():
                logger.warning("Previous Qwen STT thread still alive after 8s; starting a new one anyway")
        self.healthy.clear()
        self.thread = threading.Thread(target=self._run_session, name="QwenSTTEngine", daemon=True)
        self.thread.start()

    def is_running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def stop(self) -> None:
        if self.stop_event:
            self.stop_event.set()

    def get_audio_source(self) -> str:
        return self.audio_source

    def send_audio(self, data: bytes) -> None:
        if not data:
            return
        with self._conversation_lock:
            conversation = self._conversation
        if conversation is None:
            raise RuntimeError("Qwen ASR conversation is not connected")
        audio_b64 = base64.b64encode(data).decode("ascii")
        conversation.append_audio(audio_b64)

    def _start_audio_streamer(self) -> None:
        with self.audio_lock:
            existing = self.audio_streamer
            self.audio_streamer = None
        if existing:
            existing.stop()

        streamer = AudioStreamer(
            _QwenAudioTarget(self),
            initial_source=self.get_audio_source(),
            sample_rate=self.sample_rate,
            chunk_size=self.chunk_size,
            mute_mic_when_vrchat_muted=False,
        )
        with self.audio_lock:
            self.audio_streamer = streamer
        streamer.start()

    def _stop_audio_streamer(self) -> None:
        with self.audio_lock:
            streamer = self.audio_streamer
            self.audio_streamer = None
        if streamer:
            streamer.stop()

    def _session_url(self) -> str:
        return QWEN_ASR_URL_INTERNATIONAL if QWEN_ASR_USE_INTERNATIONAL else QWEN_ASR_URL

    def _build_update_session_kwargs(self) -> dict[str, Any]:
        transcription_args: dict[str, Any] = {
            "sample_rate": self.sample_rate,
            "input_audio_format": "pcm",
        }
        language = _normalize_qwen_language(QWEN_ASR_LANGUAGE)
        if language:
            transcription_args["language"] = language

        update_kwargs: dict[str, Any] = {
            "output_modalities": [MultiModality.TEXT],
            "enable_input_audio_transcription": True,
            "transcription_params": TranscriptionParams(**transcription_args),
        }
        if QWEN_ASR_ENABLE_VAD:
            update_kwargs.update(
                {
                    "enable_turn_detection": True,
                    "turn_detection_type": "server_vad",
                    "turn_detection_threshold": QWEN_ASR_VAD_THRESHOLD,
                    "turn_detection_silence_duration_ms": QWEN_ASR_VAD_SILENCE_DURATION_MS,
                }
            )
        else:
            update_kwargs.update(
                {
                    "enable_turn_detection": False,
                    "turn_detection_type": None,
                }
            )
        return update_kwargs

    def _run_session(self) -> None:
        self.stop_event = threading.Event()
        self._stopping = False
        self.last_disconnect_reason = None
        disconnect_reason = "session ended"
        conversation: Optional[OmniRealtimeConversation] = None
        try:
            api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("DASHSCOPE_API_KEY is not set")
            dashscope.api_key = api_key

            callback = _QwenCallback(self)
            conversation = OmniRealtimeConversation(
                callback=callback,
                model=QWEN_ASR_MODEL,
                url=self._session_url(),
            )
            logger.info("Connecting to Qwen ASR (%s)...", QWEN_ASR_MODEL)
            conversation.connect()
            conversation.update_session(**self._build_update_session_kwargs())
            with self._conversation_lock:
                self._conversation = conversation
            self._start_audio_streamer()
            self.healthy.set()
            logger.info("Qwen ASR session started.")

            while self.stop_event and not self.stop_event.is_set():
                time.sleep(0.05)
            disconnect_reason = "stopped"
        except Exception as error:
            disconnect_reason = f"connection error: {error}"
            logger.warning("Qwen ASR session error: %s", error)
        finally:
            self._stopping = True
            self.healthy.clear()
            if self.stop_event:
                self.stop_event.set()
            self.stop_event = None
            self._stop_audio_streamer()
            with self._conversation_lock:
                if conversation is None:
                    conversation = self._conversation
                self._conversation = None
            if conversation is not None:
                if not QWEN_ASR_ENABLE_VAD:
                    with suppress(Exception):
                        conversation.commit()
                with suppress(Exception):
                    conversation.end_session()
                with suppress(Exception):
                    conversation.close()
            self.last_disconnect_reason = self.last_disconnect_reason or disconnect_reason
            logger.info("STT engine session ended: %s", self.last_disconnect_reason)

    def _on_error(self, error: Exception) -> None:
        self.last_disconnect_reason = str(error)
        logger.warning("Qwen ASR error: %s", error)

    def _on_connection_closed(self, reason: str) -> None:
        if self._stopping:
            return
        self.last_disconnect_reason = reason
        if self.stop_event:
            self.stop_event.set()

    def _on_session_finished(self) -> None:
        if self._stopping:
            return
        self.last_disconnect_reason = "session finished"
        if self.stop_event:
            self.stop_event.set()
