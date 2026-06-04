"""
Entry point: start the OpenAI-compatible FastAPI server and (optionally) a
floating always-on-top overlay window.

Listening is on-demand — the STT stream + audio capture start per request and
stop when it finishes. When the overlay is enabled, uvicorn runs on a background
thread and the overlay owns the main thread (Tkinter requires the main thread).
"""
import logging
import threading

import uvicorn

import api_server
from config import (
    SERVER_HOST,
    SERVER_PORT,
    MODEL_NAME,
    SHOW_OVERLAY,
    OVERLAY_OPACITY,
    describe,
)
from osc_sender import OscSender
from stt_engine import STTEngine

logger = logging.getLogger(__name__)


def _make_server() -> uvicorn.Server:
    config = uvicorn.Config(api_server.app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
    server = uvicorn.Server(config)
    # uvicorn installs signal handlers only on the main thread; disable so it
    # can run on a background thread when the overlay owns the main thread.
    server.install_signal_handlers = lambda: None
    return server


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"🧩 VRChat-to-API | {describe()}")

    engine = STTEngine()
    sender = OscSender()

    overlay = None
    if SHOW_OVERLAY:
        try:
            from overlay import Overlay
            overlay = Overlay(engine, opacity=OVERLAY_OPACITY)
        except Exception as error:  # no display / Tk unavailable
            logger.warning("Overlay unavailable (%s); running headless.", error)
            overlay = None

    api_server.configure(engine, sender, status_cb=(overlay.push if overlay else None))
    if overlay is not None:
        engine.subscribe(overlay.push)

    print(f"🚀 OpenAI-compatible API at http://{SERVER_HOST}:{SERVER_PORT}/v1  (model: {MODEL_NAME})")
    print("👂 Listening is on-demand — starts when a request arrives.")

    if overlay is not None:
        # uvicorn on a background thread; overlay owns the main thread.
        server = _make_server()
        threading.Thread(target=server.run, name="uvicorn", daemon=True).start()
        print("🪟 Overlay window enabled (close it to quit).")
        overlay.run()
    else:
        uvicorn.run(api_server.app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")


if __name__ == "__main__":
    main()
