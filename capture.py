"""
Capture one spoken reply per API request.

A CaptureSession subscribes to the running STTEngine when a request arrives,
collects the final transcript tokens that land after the prompt was sent, and
decides when the reply is "finished":

    complete = endpoint_detected seen AND (now - last_final) >= MIN_SILENCE

capped by MAX_WAIT. On timeout it returns whatever was captured, or a
configurable no-reply fallback if nothing was said.
"""
import asyncio
import threading
import time
import logging

logger = logging.getLogger(__name__)


class CaptureSession:
    def __init__(
        self,
        engine,
        loop: asyncio.AbstractEventLoop,
        *,
        min_silence: float,
        max_wait: float,
        no_reply_message: str,
        silence_fallback: float = 0.0,
    ):
        self._engine = engine
        self._loop = loop
        self._min_silence = float(min_silence)
        self._max_wait = float(max_wait)
        self._no_reply = no_reply_message
        self._silence_fallback = float(silence_fallback)

        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self._final_parts: list[str] = []
        self._last_final_at: float | None = None
        self._endpoint_seen = False

        self._delta_queue: asyncio.Queue[str] = asyncio.Queue()
        self._unsub = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._unsub = self._engine.subscribe(self._on_event)

    def close(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    def __enter__(self) -> "CaptureSession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- engine callback (runs in the engine thread) ------------------------
    def _on_event(self, event: dict) -> None:
        ts = event.get("ts", time.monotonic())
        if ts < self._t0:
            return  # ignore speech that predates this request
        etype = event.get("type")
        if etype == "final":
            text = event.get("text", "")
            if not text:
                return
            with self._lock:
                self._final_parts.append(text)
                self._last_final_at = time.monotonic()
            # Hand the confirmed delta to the event loop thread.
            self._loop.call_soon_threadsafe(self._delta_queue.put_nowait, text)
        elif etype == "endpoint":
            with self._lock:
                self._endpoint_seen = True

    # -- completion ---------------------------------------------------------
    def _is_complete(self) -> bool:
        with self._lock:
            # Need at least some speech before we can complete.
            if self._last_final_at is None:
                return False
            silence = time.monotonic() - self._last_final_at
            # Primary: endpoint detected AND enough trailing silence.
            if self._endpoint_seen and silence >= self._min_silence:
                return True
            # Safety net: endpoint may never arrive on a noisy loopback feed;
            # complete once silence has clearly lasted past the fallback window.
            if self._silence_fallback > 0 and silence >= self._silence_fallback:
                return True
            return False

    def _assemble(self) -> str:
        with self._lock:
            text = "".join(self._final_parts).strip()
        return text or self._no_reply

    # -- non-streaming ------------------------------------------------------
    async def wait_complete(self) -> str:
        deadline = self._t0 + self._max_wait
        while True:
            if self._is_complete():
                return self._assemble()
            if time.monotonic() >= deadline:
                logger.info("Capture hit max_wait (%.1fs); returning partial/fallback.", self._max_wait)
                return self._assemble()
            await asyncio.sleep(0.1)

    # -- streaming (confirmed deltas only) ----------------------------------
    async def stream(self):
        """Yield confirmed (final) text deltas until complete or timeout."""
        deadline = self._t0 + self._max_wait
        produced = False
        while True:
            if self._is_complete():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.info("Capture hit max_wait (%.1fs) during stream.", self._max_wait)
                break
            try:
                delta = await asyncio.wait_for(self._delta_queue.get(), timeout=min(0.2, remaining))
                produced = True
                yield delta
            except asyncio.TimeoutError:
                continue

        # Drain anything confirmed right before completion.
        while not self._delta_queue.empty():
            produced = True
            yield self._delta_queue.get_nowait()

        if not produced:
            yield self._no_reply
