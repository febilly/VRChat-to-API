"""
Interactive command-line chat client for the local OpenAI-compatible endpoint.

A tiny debug REPL — no extra dependencies (uses `requests`). Keeps multi-turn
history, streams the reply by default, and is independent of the server config
(so it won't trigger the Soniox validation in config.py).

Usage:
    python chat_cli.py
    python chat_cli.py --no-stream
    python chat_cli.py --host 127.0.0.1 --port 8080 --model vrchat-human

In-chat commands:
    /reset   clear conversation history
    /stream  toggle streaming on/off
    /system  set (or clear) a system prompt
    /exit    quit  (also Ctrl-C / Ctrl-D)
"""
import argparse
import json
import os
import sys

import requests


def _base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/v1"


def stream_chat(base_url: str, model: str, messages: list, timeout: float):
    """Yield content deltas from a streaming chat completion."""
    with requests.post(
        f"{base_url}/chat/completions",
        json={"model": model, "messages": messages, "stream": True},
        stream=True,
        timeout=timeout,
    ) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            if not raw.startswith("data: "):
                continue
            data = raw[6:]
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            delta = obj.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content")
            if content:
                yield content


def nonstream_chat(base_url: str, model: str, messages: list, timeout: float) -> str:
    resp = requests.post(
        f"{base_url}/chat/completions",
        json={"model": model, "messages": messages, "stream": False},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive CLI chat client for VRChat-to-API")
    parser.add_argument("--host", default=os.environ.get("SERVER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SERVER_PORT", "8080")))
    parser.add_argument("--model", default=os.environ.get("MODEL_NAME", "vrchat-human"))
    parser.add_argument("--system", default=None, help="optional system prompt")
    parser.add_argument("--no-stream", action="store_true", help="disable streaming")
    parser.add_argument("--timeout", type=float, default=300.0, help="request timeout seconds")
    args = parser.parse_args()

    base_url = _base_url(args.host, args.port)
    streaming = not args.no_stream
    messages: list[dict] = []
    if args.system:
        messages.append({"role": "system", "content": args.system})

    print(f"💬 VRChat-to-API chat | {base_url} | model={args.model} | stream={streaming}")
    print("   commands: /reset  /stream  /system  /exit\n")

    while True:
        try:
            user = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user:
            continue

        if user in ("/exit", "/quit"):
            break
        if user == "/reset":
            messages = [m for m in messages if m["role"] == "system"]
            print("(history cleared)")
            continue
        if user == "/stream":
            streaming = not streaming
            print(f"(streaming = {streaming})")
            continue
        if user.startswith("/system"):
            new_sys = user[len("/system"):].strip()
            messages = [m for m in messages if m["role"] != "system"]
            if new_sys:
                messages.insert(0, {"role": "system", "content": new_sys})
                print(f"(system prompt set)")
            else:
                print("(system prompt cleared)")
            continue

        messages.append({"role": "user", "content": user})

        try:
            print("VRC> ", end="", flush=True)
            if streaming:
                pieces: list[str] = []
                for delta in stream_chat(base_url, args.model, messages, args.timeout):
                    pieces.append(delta)
                    sys.stdout.write(delta)
                    sys.stdout.flush()
                print()
                reply = "".join(pieces)
            else:
                reply = nonstream_chat(base_url, args.model, messages, args.timeout)
                print(reply)
        except requests.RequestException as error:
            print(f"\n⚠️  request failed: {error}")
            messages.pop()  # drop the user message that didn't get a reply
            continue

        messages.append({"role": "assistant", "content": reply})

    print("bye 👋")


if __name__ == "__main__":
    main()
