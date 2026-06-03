"""
Soniox STT engine with seamless stream rollover.

Ported and trimmed from the realtime-subtitle reference project's
SonioxSession. The seamless rollover state machine (warm a fresh stream in the
background, then atomically switch during a quiet gap) is preserved so that
temp-key streams with a hard lifetime never interrupt recognition.

Stripped: translation, LLM refine, web-frontend broadcast, IPC, Twitch.
Instead of broadcasting to a frontend, the engine publishes transcript events
to subscribers:

    {"type": "final",    "text": <newly finalized text>, "ts": <monotonic>}
    {"type": "partial",  "text": <current non-final text>, "ts": <monotonic>}
    {"type": "endpoint", "ts": <monotonic>}            # endpoint_detected fired

The engine runs continuously in a background thread; CaptureSession consumes
the events to assemble one spoken reply per API request.
"""
import json
import threading
import time
import concurrent.futures
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from websockets import ConnectionClosed, ConnectionClosedOK
from websockets.sync.client import connect as sync_connect

from config import (
    SONIOX_WEBSOCKET_URL,
    SONIOX_STREAM_DURATION_SECONDS,
    SONIOX_SLEEP_ON_SILENCE,
    SONIOX_SLEEP_IDLE_SECONDS,
    SONIOX_SLEEP_PRE_ROLL_SECONDS,
    SONIOX_SLEEP_SPEECH_GRACE_SECONDS,
    SAMPLE_RATE,
    CHUNK_SIZE,
    AUDIO_SOURCE,
)
from audio_router import AudioSendRouter
from audio_capture import AudioStreamer
from soniox_client import get_config, get_api_key

logger = logging.getLogger(__name__)

STREAM_ROLLOVER_RECV_TIMEOUT_SECONDS = 0.25
STREAM_ROLLOVER_FINALIZE_TIMEOUT_SECONDS = 1.5
STREAM_ROLLOVER_AUDIO_BUFFER_CHUNKS = 200
STREAM_ROLLOVER_NEAR_LIMIT_RATIO = 0.8
STREAM_ROLLOVER_SWITCH_PATIENCE_SECONDS = 25.0
STREAM_ROLLOVER_FORCE_GUARD_SECONDS = 2.0
STREAM_ROLLOVER_SILENCE_HOLD_SECONDS = 0.7
STREAM_ROLLOVER_WARMUP_DRAIN_LIMIT = 8
SONIOX_INTERNAL_TOKEN_TEXTS = {"<end>", "<fin>"}


def _mask_key(key: str) -> str:
    """Short, non-sensitive preview of an API key for logging."""
    if not key:
        return "<none>"
    return f"{key[:8]}…{key[-4:]}" if len(key) > 14 else key[:4] + "…"


class _RealtimeSilenceSender:
    """Send realtime-paced PCM silence to a warming Soniox stream."""

    def __init__(
        self,
        ws,
        *,
        bytes_per_chunk: int,
        chunk_interval_seconds: float,
        session_stop_event: threading.Event | None,
    ):
        self.ws = ws
        self.payload = b"\0" * max(2, int(bytes_per_chunk))
        self.chunk_interval_seconds = max(0.01, float(chunk_interval_seconds))
        self.session_stop_event = session_stop_event
        self.error: Exception | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="SonioxRolloverSilence", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    def _run(self) -> None:
        next_send_at = time.monotonic()
        while not self._stop_event.is_set():
            if self.session_stop_event and self.session_stop_event.is_set():
                break
            try:
                self.ws.send(self.payload)
            except Exception as error:
                self.error = error
                break
            next_send_at += self.chunk_interval_seconds
            delay = next_send_at - time.monotonic()
            if delay < 0:
                next_send_at = time.monotonic()
                delay = self.chunk_interval_seconds
            self._stop_event.wait(delay)


@dataclass
class _SonioxStreamState:
    ws: Any
    index: int
    api_key: str
    started_at: float
    all_final_tokens: list[dict]
    sent_count: int = 0
    ready_at: float | None = None
    silence_sender: _RealtimeSilenceSender | None = None
    silence_started_at: float = 0.0


class STTEngine:
    """Continuously transcribes loopback audio and publishes transcript events."""

    def __init__(self):
        self.sample_rate = int(SAMPLE_RATE)
        self.chunk_size = int(CHUNK_SIZE)
        self.audio_source = AUDIO_SOURCE
        self.audio_format = "pcm_s16le"

        self.stop_event: Optional[threading.Event] = None
        self.thread: Optional[threading.Thread] = None
        self.ws = None
        self.api_key: Optional[str] = None
        self.audio_streamer: Optional[AudioStreamer] = None
        self.audio_lock = threading.Lock()

        self._subscribers: list[Callable[[dict], None]] = []
        self._sub_lock = threading.Lock()
        self.last_disconnect_reason: Optional[str] = None
        self.healthy = threading.Event()

    # -- subscription -------------------------------------------------------
    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        """Register an event callback. Returns an unsubscribe function."""
        with self._sub_lock:
            self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._sub_lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return _unsubscribe

    def _publish(self, event: dict) -> None:
        with self._sub_lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception as error:
                logger.warning("STT subscriber raised: %s", error)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        # Reap a previous session thread that may still be tearing down (its
        # teardown — audio stop ~1.5s + ws close ~2s — happens here, off the
        # request's hot path, so stop() can return instantly).
        old = self.thread
        if old and old.is_alive() and old is not threading.current_thread():
            if self.stop_event:
                self.stop_event.set()
            old.join(timeout=8.0)
            if old.is_alive():
                logger.warning("Previous STT thread still alive after 8s; starting a new one anyway")
        self.healthy.clear()
        self.thread = threading.Thread(target=self._run_session, name="STTEngine", daemon=True)
        self.thread.start()

    def is_running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def stop(self) -> None:
        # Signal only — never blocks. The thread tears itself down; the next
        # start() reaps it. This keeps stop() free of cancellation points so a
        # request's cleanup can't be interrupted mid-teardown.
        if self.stop_event:
            self.stop_event.set()

    def get_audio_source(self) -> str:
        return self.audio_source

    # -- audio streamer -----------------------------------------------------
    def _start_audio_streamer(self, target) -> None:
        with self.audio_lock:
            existing = self.audio_streamer
            self.audio_streamer = None
        if existing:
            existing.stop()

        streamer = AudioStreamer(
            target,
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

    # -- token parsing ------------------------------------------------------
    @staticmethod
    def _is_internal(text) -> bool:
        return text in SONIOX_INTERNAL_TOKEN_TEXTS

    def _process_soniox_response(
        self,
        res: dict,
        all_final_tokens: list[dict],
        sent_count: int,
    ) -> tuple[int, bool, str | None]:
        """Process one Soniox response. Returns (sent_count, should_end, reason)."""
        if res.get("error_code") is not None:
            message = res.get("error_message", "")
            return sent_count, True, f"server error {res['error_code']}: {message}"

        non_final_parts: list[str] = []
        for token in res.get("tokens", []):
            text = token.get("text")
            if not text:
                continue
            if token.get("is_final"):
                all_final_tokens.append(token)
            elif not self._is_internal(text):
                non_final_parts.append(text)

        new_final = all_final_tokens[sent_count:]
        now = time.monotonic()

        # Soniox marks an endpoint with either the endpoint_detected flag OR an
        # internal "<end>" final token. The flag alone is unreliable on a
        # continuous loopback feed, so honor both (matching the reference).
        endpoint_detected = bool(res.get("endpoint_detected", False)) or any(
            t.get("text") == "<end>" for t in new_final
        )

        new_final_text = "".join(
            t.get("text", "")
            for t in new_final
            if t.get("text") and not self._is_internal(t.get("text"))
        )
        if new_final_text:
            self._publish({"type": "final", "text": new_final_text, "ts": now})
        if non_final_parts:
            self._publish({"type": "partial", "text": "".join(non_final_parts), "ts": now})
        if endpoint_detected:
            self._publish({"type": "endpoint", "ts": now})

        sent_count = len(all_final_tokens)

        if res.get("finished"):
            return sent_count, True, "session finished"
        return sent_count, False, None

    # -- rollover timing helpers (verbatim from reference) ------------------
    def _stream_rollover_seconds(self) -> float | None:
        if SONIOX_STREAM_DURATION_SECONDS is None:
            return None
        try:
            value = float(SONIOX_STREAM_DURATION_SECONDS)
        except Exception:
            return None
        return value if value > 0 else None

    def _sleep_idle_seconds(self) -> float | None:
        if not SONIOX_SLEEP_ON_SILENCE:
            return None
        try:
            value = float(SONIOX_SLEEP_IDLE_SECONDS)
        except Exception:
            return None
        return value if value > 0 else None

    def _stream_is_near_rollover_limit(self, started_at, rollover_seconds) -> bool:
        if started_at is None or rollover_seconds is None:
            return False
        return (time.monotonic() - started_at) >= (rollover_seconds * STREAM_ROLLOVER_NEAR_LIMIT_RATIO)

    def _stream_rollover_prepare_age(self, rollover_seconds: float) -> float:
        return max(0.0, self._stream_rollover_force_age(rollover_seconds) - self._stream_rollover_switch_patience(rollover_seconds))

    def _stream_rollover_switch_patience(self, rollover_seconds: float) -> float:
        return max(0.0, min(STREAM_ROLLOVER_SWITCH_PATIENCE_SECONDS, rollover_seconds * 0.5))

    def _stream_rollover_force_age(self, rollover_seconds: float) -> float:
        guard_seconds = min(STREAM_ROLLOVER_FORCE_GUARD_SECONDS, max(0.5, rollover_seconds * 0.1))
        return max(0.0, rollover_seconds - guard_seconds)

    def _should_prepare_rollover_stream(self, started_at, rollover_seconds) -> bool:
        if started_at is None or rollover_seconds is None:
            return False
        return (time.monotonic() - started_at) >= self._stream_rollover_prepare_age(rollover_seconds)

    def _should_force_rollover_switch(self, started_at, rollover_seconds) -> bool:
        if started_at is None or rollover_seconds is None:
            return False
        return (time.monotonic() - started_at) >= self._stream_rollover_force_age(rollover_seconds)

    def _make_rollover_silence_sender(self, ws) -> _RealtimeSilenceSender:
        bytes_per_chunk = int(self.chunk_size) * 2
        chunk_interval_seconds = int(self.chunk_size) / max(1, int(self.sample_rate))
        return _RealtimeSilenceSender(
            ws,
            bytes_per_chunk=bytes_per_chunk,
            chunk_interval_seconds=chunk_interval_seconds,
            session_stop_event=self.stop_event,
        )

    # -- stream open/close --------------------------------------------------
    def _open_soniox_stream_state(self, api_key: str, stream_index: int, *, warming: bool = False) -> _SonioxStreamState:
        config = get_config(api_key, self.audio_format)
        label = f"stream #{stream_index}"
        purpose = " warmup" if warming else ""
        logger.info("Connecting to Soniox (%s%s) with key %s...", label, purpose, _mask_key(api_key))
        # Short close_timeout so on-demand teardown doesn't block on the
        # websocket close handshake (default is 10s).
        ws = sync_connect(SONIOX_WEBSOCKET_URL, open_timeout=10, close_timeout=2)
        ws.send(json.dumps(config))
        now = time.monotonic()
        state = _SonioxStreamState(
            ws=ws,
            index=stream_index,
            api_key=api_key,
            started_at=now,
            ready_at=now,
            all_final_tokens=[],
        )
        logger.info("Session started (%s%s).", label, purpose)
        return state

    def _close_soniox_stream_state(self, stream: _SonioxStreamState | None) -> None:
        if stream is None:
            return
        if stream.silence_sender is not None:
            stream.silence_sender.stop()
            stream.silence_sender = None
        try:
            stream.ws.close()
        except Exception as close_error:
            logger.warning("Error closing Soniox stream #%s: %s", stream.index, close_error)

    def _drain_warmup_stream(self, stream: _SonioxStreamState) -> bool:
        """Read and discard silence warmup responses. Returns False if stream ended."""
        for _ in range(STREAM_ROLLOVER_WARMUP_DRAIN_LIMIT):
            try:
                message = stream.ws.recv(timeout=0.001)
            except TimeoutError:
                return True
            except ConnectionClosedOK:
                return False
            except ConnectionClosed as error:
                logger.warning("Soniox warmup stream #%s closed: %s", stream.index, error)
                return False
            except Exception as error:
                logger.warning("Error reading Soniox warmup stream #%s: %s", stream.index, error)
                return False

            try:
                res = json.loads(message)
            except Exception as error:
                logger.warning("Failed to parse Soniox warmup response: %s", error)
                continue

            if res.get("error_code") is not None:
                logger.warning("Soniox warmup error %s: %s", res.get("error_code"), res.get("error_message", ""))
                return False
            if res.get("finished"):
                return False
        return True

    def _fetch_api_key_for_next_stream(self, current_api_key: str) -> str:
        """Refresh temp keys between rollovers while preserving permanent keys."""
        try:
            next_key = get_api_key()
        except Exception as error:
            logger.warning("Failed to refresh Soniox API key for stream rollover: %s", error)
            return current_api_key
        next_key = (next_key or "").strip()
        if next_key:
            self.api_key = next_key
            return next_key
        return current_api_key

    def _prepare_warmup_stream(self, current_api_key: str, stream_index: int) -> _SonioxStreamState:
        """Run in a background thread: fetch key + connect + send config."""
        next_api_key = self._fetch_api_key_for_next_stream(current_api_key)
        ws_state = self._open_soniox_stream_state(next_api_key, stream_index, warming=True)
        ws_state.silence_sender = self._make_rollover_silence_sender(ws_state.ws)
        return ws_state

    def _finalize_stream_before_rollover(self, ws, all_final_tokens: list[dict], sent_count: int) -> int:
        """Ask Soniox to finalize pending tokens before switching streams."""
        try:
            ws.send(json.dumps({"type": "finalize"}))
        except Exception as error:
            logger.warning("Failed to request Soniox finalization before rollover: %s", error)
            return sent_count

        deadline = time.monotonic() + STREAM_ROLLOVER_FINALIZE_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                message = ws.recv(timeout=remaining)
            except TimeoutError:
                break
            except ConnectionClosedOK:
                break
            except Exception as error:
                logger.warning("Error while waiting for Soniox rollover finalization: %s", error)
                break
            try:
                res = json.loads(message)
            except Exception as error:
                logger.warning("Failed to parse Soniox finalization response: %s", error)
                continue
            sent_count, should_end, _reason = self._process_soniox_response(res, all_final_tokens, sent_count)
            if should_end:
                break
        return sent_count

    def _finalize_and_close_stream(self, old_stream: _SonioxStreamState) -> None:
        """Finalize and close an old stream after rollover, in a background thread."""
        try:
            old_stream.sent_count = self._finalize_stream_before_rollover(
                old_stream.ws, old_stream.all_final_tokens, old_stream.sent_count,
            )
        except Exception as error:
            logger.warning("Error finalizing old stream #%s: %s", old_stream.index, error)
        self._close_soniox_stream_state(old_stream)

    def _open_and_switch_to_replacement_stream(
        self,
        audio_router: AudioSendRouter,
        old_stream: _SonioxStreamState,
        current_api_key: str,
        stream_index: int,
        reason: str,
    ) -> tuple[_SonioxStreamState, str, int] | None:
        next_stream_index = stream_index + 1
        replacement_stream: _SonioxStreamState | None = None
        try:
            next_api_key = self._fetch_api_key_for_next_stream(current_api_key)
            replacement_stream = self._open_soniox_stream_state(next_api_key, next_stream_index)
            logger.info(
                "🔁 Switching Soniox audio from stream #%s to stream #%s at %s.",
                old_stream.index, replacement_stream.index, reason,
            )
            if not audio_router.switch_target(replacement_stream.ws, expected_current=old_stream.ws):
                self._close_soniox_stream_state(replacement_stream)
                return None
            old_stream.sent_count = self._finalize_stream_before_rollover(
                old_stream.ws, old_stream.all_final_tokens, old_stream.sent_count,
            )
            self._close_soniox_stream_state(old_stream)
            return replacement_stream, next_api_key, next_stream_index
        except Exception as error:
            if replacement_stream is not None:
                self._close_soniox_stream_state(replacement_stream)
            logger.warning("Failed to switch Soniox stream at %s: %s", reason, error)
            return None

    # -- main loop ----------------------------------------------------------
    def _run_session(self) -> None:
        try:
            api_key = get_api_key()
        except Exception as error:
            self.last_disconnect_reason = f"failed to acquire API key: {error}"
            logger.error("❌ %s", self.last_disconnect_reason)
            return

        self.api_key = api_key
        rollover_seconds = self._stream_rollover_seconds()
        if rollover_seconds is not None:
            logger.info("🔁 Soniox stream rollover enabled: %.1fs per stream", rollover_seconds)
        sleep_idle_seconds = self._sleep_idle_seconds()
        if sleep_idle_seconds is not None:
            logger.info("💤 Soniox silence sleep enabled: %.1fs idle", sleep_idle_seconds)

        self.stop_event = threading.Event()
        disconnect_reason = "connection ended"
        current_api_key = api_key
        stream_index = 1
        active_stream: _SonioxStreamState | None = None
        warmup_stream: _SonioxStreamState | None = None
        dormant_for_silence = False
        next_prepare_attempt_at = 0.0
        warmup_future: concurrent.futures.Future | None = None
        key_fetch_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="soniox-key")
        audio_router = AudioSendRouter(
            max_buffered_chunks=STREAM_ROLLOVER_AUDIO_BUFFER_CHUNKS,
            sample_rate=self.sample_rate,
            chunk_size=self.chunk_size,
            silence_hold_seconds=STREAM_ROLLOVER_SILENCE_HOLD_SECONDS,
            sleep_idle_seconds=sleep_idle_seconds,
            sleep_pre_roll_seconds=SONIOX_SLEEP_PRE_ROLL_SECONDS,
            sleep_speech_grace_seconds=SONIOX_SLEEP_SPEECH_GRACE_SECONDS,
        )

        try:
            active_stream = self._open_soniox_stream_state(current_api_key, stream_index)
            self.ws = active_stream.ws

            if not audio_router.set_target(active_stream.ws):
                disconnect_reason = "failed to attach audio to Soniox stream"
                return

            self._start_audio_streamer(audio_router)
            self.healthy.set()

            while True:
                if self.stop_event and self.stop_event.is_set():
                    break

                if active_stream is None:
                    if dormant_for_silence:
                        if audio_router.wake_ready():
                            buffered_count = audio_router.buffered_count()
                            try:
                                next_api_key = self._fetch_api_key_for_next_stream(current_api_key)
                                resumed_stream = self._open_soniox_stream_state(next_api_key, stream_index + 1)
                                if not audio_router.set_target(resumed_stream.ws):
                                    self._close_soniox_stream_state(resumed_stream)
                                    disconnect_reason = "failed to attach audio after silence sleep"
                                    break
                                active_stream = resumed_stream
                                stream_index = active_stream.index
                                current_api_key = active_stream.api_key
                                self.ws = active_stream.ws
                                dormant_for_silence = False
                                logger.info(
                                    "▶️  Speech detected after silence; reopened Soniox stream "
                                    "#%s and flushed %s buffered chunks.",
                                    active_stream.index, buffered_count,
                                )
                            except Exception as error:
                                disconnect_reason = f"failed to reopen Soniox stream after silence: {error}"
                                logger.warning("%s", disconnect_reason)
                                break
                        else:
                            time.sleep(0.05)
                            continue
                    else:
                        disconnect_reason = "stream rollover failed"
                        break

                if (
                    sleep_idle_seconds is not None
                    and not dormant_for_silence
                    and active_stream is not None
                    and audio_router.sleep_ready()
                ):
                    if warmup_stream is not None:
                        if warmup_stream.silence_sender is not None:
                            warmup_stream.silence_sender.stop()
                            warmup_stream.silence_sender = None
                        self._close_soniox_stream_state(warmup_stream)
                        warmup_stream = None
                    if warmup_future is not None:
                        warmup_future.cancel()
                        warmup_future = None

                    logger.info(
                        "💤 No speech for %.1fs; closing Soniox stream.",
                        audio_router.sleep_confirmed_silence_seconds(),
                    )
                    sleeping_stream = active_stream
                    if not audio_router.enter_sleep_buffering(sleeping_stream.ws):
                        disconnect_reason = "failed to detach audio for silence sleep"
                        break
                    active_stream = None
                    self.ws = None
                    dormant_for_silence = True
                    sleeping_stream.sent_count = self._finalize_stream_before_rollover(
                        sleeping_stream.ws, sleeping_stream.all_final_tokens, sleeping_stream.sent_count,
                    )
                    self._close_soniox_stream_state(sleeping_stream)
                    continue

                if active_stream is None:
                    disconnect_reason = "stream rollover failed"
                    break

                if (
                    rollover_seconds is not None
                    and warmup_stream is None
                    and warmup_future is None
                    and time.monotonic() >= next_prepare_attempt_at
                    and self._should_prepare_rollover_stream(active_stream.started_at, rollover_seconds)
                ):
                    warmup_future = key_fetch_executor.submit(
                        self._prepare_warmup_stream, current_api_key, stream_index,
                    )
                    stream_index += 1
                    next_prepare_attempt_at = time.monotonic() + 1.0

                if warmup_future is not None and warmup_future.done():
                    try:
                        warmup_stream = warmup_future.result(timeout=0)
                        warmup_future = None
                        warmup_stream.silence_sender.start()
                        warmup_stream.silence_started_at = time.monotonic()
                        logger.info(
                            "🔁 Soniox stream #%s warming with realtime silence; "
                            "waiting for a quiet gap to switch.",
                            warmup_stream.index,
                        )
                    except Exception as error:
                        logger.warning("Failed to prepare next Soniox stream for rollover: %s", error)
                        warmup_future = None
                        warmup_stream = None
                        stream_index -= 1
                        next_prepare_attempt_at = time.monotonic() + 1.0

                if (
                    rollover_seconds is not None
                    and warmup_stream is None
                    and self._should_force_rollover_switch(active_stream.started_at, rollover_seconds)
                ):
                    switched = self._open_and_switch_to_replacement_stream(
                        audio_router, active_stream, current_api_key, stream_index,
                        "rollover guard deadline without warmup",
                    )
                    if switched is None:
                        disconnect_reason = "failed to switch Soniox stream before configured duration"
                        break
                    active_stream, current_api_key, stream_index = switched
                    self.ws = active_stream.ws
                    continue

                if warmup_stream is not None:
                    warmup_alive = True
                    silence_sender = warmup_stream.silence_sender
                    if silence_sender is not None and silence_sender.error is not None:
                        logger.warning("Soniox warmup silence failed: %s", silence_sender.error)
                        warmup_alive = False
                    elif not self._drain_warmup_stream(warmup_stream):
                        warmup_alive = False

                    if not warmup_alive:
                        self._close_soniox_stream_state(warmup_stream)
                        warmup_stream = None
                        next_prepare_attempt_at = time.monotonic() + 1.0
                    else:
                        switch_on_silence = audio_router.silence_ready(min_observed_at=warmup_stream.ready_at)
                        force_switch = self._should_force_rollover_switch(active_stream.started_at, rollover_seconds)
                        silence_elapsed = (
                            time.monotonic() - warmup_stream.silence_started_at
                            if warmup_stream.silence_started_at else 0.0
                        )
                        if silence_elapsed >= 2.0 and (switch_on_silence or force_switch):
                            switch_reason = (
                                f"quiet gap ({audio_router.consecutive_silence_seconds():.2f}s, "
                                f"silence sent {silence_elapsed:.1f}s)"
                                if switch_on_silence else "rollover guard deadline"
                            )
                            logger.info(
                                "🔁 Switching Soniox audio from stream #%s to stream #%s at %s.",
                                active_stream.index, warmup_stream.index, switch_reason,
                            )
                            old_stream = active_stream
                            if warmup_stream.silence_sender is not None:
                                warmup_stream.silence_sender.stop()
                                warmup_stream.silence_sender = None

                            if not audio_router.switch_target(warmup_stream.ws, expected_current=old_stream.ws):
                                disconnect_reason = "failed to switch audio to warmed Soniox stream"
                                break

                            active_stream = warmup_stream
                            warmup_stream = None
                            current_api_key = active_stream.api_key
                            self.ws = active_stream.ws

                            threading.Thread(
                                target=self._finalize_and_close_stream,
                                args=(old_stream,),
                                daemon=True,
                                name=f"soniox-finalize-{old_stream.index}",
                            ).start()
                            continue

                try:
                    recv_timeout = (
                        STREAM_ROLLOVER_RECV_TIMEOUT_SECONDS
                        if rollover_seconds is not None or sleep_idle_seconds is not None
                        else None
                    )
                    message = active_stream.ws.recv(timeout=recv_timeout)
                except TimeoutError:
                    continue
                except ConnectionClosed as error:
                    if rollover_seconds is not None and self._stream_is_near_rollover_limit(
                        active_stream.started_at, rollover_seconds,
                    ):
                        logger.info(
                            "🔁 Soniox stream #%s closed near configured duration; rolling over...",
                            active_stream.index,
                        )
                        audio_router.clear_target(active_stream.ws)
                        self._close_soniox_stream_state(active_stream)

                        if warmup_stream is not None:
                            if warmup_stream.silence_sender is not None:
                                warmup_stream.silence_sender.stop()
                                warmup_stream.silence_sender = None
                            active_stream = warmup_stream
                            warmup_stream = None
                            current_api_key = active_stream.api_key
                            self.ws = active_stream.ws
                            if not audio_router.set_target(active_stream.ws):
                                disconnect_reason = "failed to attach warmed Soniox stream after closure"
                                break
                            continue

                        replacement = self._open_and_switch_to_replacement_stream(
                            audio_router, active_stream, current_api_key, stream_index,
                            "stream closed near configured duration",
                        )
                        if replacement is None:
                            disconnect_reason = "failed to attach replacement Soniox stream"
                            break
                        active_stream, current_api_key, stream_index = replacement
                        self.ws = active_stream.ws
                        continue

                    disconnect_reason = f"connection closed: {error}"
                    break
                except Exception as error:
                    disconnect_reason = f"connection error: {error}"
                    logger.warning("Error connecting to Soniox: %s", error)
                    break

                try:
                    res = json.loads(message)
                except Exception as error:
                    logger.warning("Failed to parse Soniox response: %s", error)
                    continue

                active_stream.sent_count, should_end, reason = self._process_soniox_response(
                    res, active_stream.all_final_tokens, active_stream.sent_count,
                )
                if should_end:
                    disconnect_reason = reason or "stream ended"
                    if rollover_seconds is not None and self._stream_is_near_rollover_limit(
                        active_stream.started_at, rollover_seconds,
                    ):
                        logger.info(
                            "🔁 Soniox stream #%s ended near configured duration; rolling over...",
                            active_stream.index,
                        )
                        audio_router.clear_target(active_stream.ws)
                        self._close_soniox_stream_state(active_stream)

                        if warmup_stream is not None:
                            if warmup_stream.silence_sender is not None:
                                warmup_stream.silence_sender.stop()
                                warmup_stream.silence_sender = None
                            active_stream = warmup_stream
                            warmup_stream = None
                            current_api_key = active_stream.api_key
                            self.ws = active_stream.ws
                            if not audio_router.set_target(active_stream.ws):
                                disconnect_reason = "failed to attach warmed Soniox stream after finish"
                                break
                            continue

                        replacement = self._open_and_switch_to_replacement_stream(
                            audio_router, active_stream, current_api_key, stream_index,
                            "stream finished near configured duration",
                        )
                        if replacement is None:
                            disconnect_reason = "failed to attach replacement Soniox stream after finish"
                            break
                        active_stream, current_api_key, stream_index = replacement
                        self.ws = active_stream.ws
                        continue
                    break

        finally:
            self.healthy.clear()
            if warmup_future is not None:
                if warmup_future.done() and not warmup_future.cancelled():
                    try:
                        leaked = warmup_future.result(timeout=0)
                        self._close_soniox_stream_state(leaked)
                    except Exception:
                        pass
                else:
                    warmup_future.cancel()
                warmup_future = None
            key_fetch_executor.shutdown(wait=False)
            if self.stop_event:
                self.stop_event.set()
            self.stop_event = None
            self.ws = None
            audio_router.close()
            self._stop_audio_streamer()
            if warmup_stream is not None:
                self._close_soniox_stream_state(warmup_stream)
            if active_stream is not None:
                self._close_soniox_stream_state(active_stream)
            # NB: do not null self.thread here — start() reaps it by join, and
            # nulling could clobber a freshly-started thread reference.
            self.last_disconnect_reason = disconnect_reason
            logger.info("STT engine session ended: %s", disconnect_reason)
