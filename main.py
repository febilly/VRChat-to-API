"""
Entry point: start the STT engine (background thread) and the OpenAI-compatible
FastAPI server.
"""
import logging

import uvicorn

import api_server
from config import SERVER_HOST, SERVER_PORT, MODEL_NAME, describe
from osc_sender import OscSender
from stt_engine import STTEngine


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"🧩 VRChat-to-API | {describe()}")

    engine = STTEngine()
    sender = OscSender()
    api_server.configure(engine, sender)

    # On-demand: the STT stream + audio capture start per request and stop when
    # it finishes (nothing is captured between requests).
    print(f"🚀 OpenAI-compatible API at http://{SERVER_HOST}:{SERVER_PORT}/v1  (model: {MODEL_NAME})")
    print("👂 Listening is on-demand — starts when a request arrives.")
    uvicorn.run(api_server.app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")


if __name__ == "__main__":
    main()
