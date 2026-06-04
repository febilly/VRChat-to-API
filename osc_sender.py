"""
VRChat OSC chatbox sender + long-message paging rotator.

Sends text to VRChat's chatbox via OSC (/chatbox/input on UDP 9000 by default).
Long prompts that exceed the 144-char chatbox limit are split by punctuation
into pages and cycled ("翻面") until stopped, with per-page dwell estimated by
reading speed.
"""
import re
import threading
import time
import logging

from pythonosc.udp_client import SimpleUDPClient

from config import (
    VRCHAT_OSC_HOST,
    VRCHAT_OSC_PORT,
    CHATBOX_MAX_LENGTH,
    CHATBOX_CJK_CPS,
    CHATBOX_LATIN_CPS,
    CHATBOX_MIN_PAGE_SECONDS,
    CHATBOX_MAX_PAGE_SECONDS,
    CHATBOX_KEEPALIVE_SECONDS,
)

logger = logging.getLogger(__name__)

# Sentence/clause boundaries used to split long prompts into pages.
_SPLIT_AFTER = "，。,.；;！？!?、\n"
_SPLIT_RE = re.compile(rf"(?<=[{re.escape(_SPLIT_AFTER)}])")


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x3000 <= o <= 0x303F      # CJK punctuation
        or 0x3040 <= o <= 0x30FF   # hiragana / katakana
        or 0x3400 <= o <= 0x4DBF   # CJK ext A
        or 0x4E00 <= o <= 0x9FFF   # CJK unified
        or 0xF900 <= o <= 0xFAFF   # CJK compatibility
        or 0xFF00 <= o <= 0xFFEF   # fullwidth forms
        or 0xAC00 <= o <= 0xD7A3   # Hangul syllables
    )


def estimate_page_seconds(text: str) -> float:
    """Per-page dwell time estimated by reading speed (CJK vs other)."""
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    seconds = cjk / CHATBOX_CJK_CPS + other / CHATBOX_LATIN_CPS
    seconds = max(CHATBOX_MIN_PAGE_SECONDS, seconds)
    if CHATBOX_MAX_PAGE_SECONDS is not None:
        seconds = min(CHATBOX_MAX_PAGE_SECONDS, seconds)
    return seconds


def split_into_pages(text: str, max_len: int = CHATBOX_MAX_LENGTH) -> list[str]:
    """Split text into <=max_len pages, preferring punctuation boundaries."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    fragments = [f for f in _SPLIT_RE.split(text) if f]

    pages: list[str] = []
    current = ""
    for frag in fragments:
        # A single fragment longer than the limit must be hard-split.
        while len(frag) > max_len:
            if current:
                pages.append(current)
                current = ""
            pages.append(frag[:max_len])
            frag = frag[max_len:]
        if len(current) + len(frag) <= max_len:
            current += frag
        else:
            if current:
                pages.append(current)
            current = frag
    if current:
        pages.append(current)
    return pages


class OscSender:
    """Thin OSC client for the VRChat chatbox."""

    def __init__(self, host: str = VRCHAT_OSC_HOST, port: int = VRCHAT_OSC_PORT):
        self._client = SimpleUDPClient(host, port)

    def send_chatbox(self, text: str, *, notify: bool = False) -> None:
        """Send text to the VRChat chatbox immediately (bypassing the keyboard)."""
        clipped = (text or "")[:CHATBOX_MAX_LENGTH]
        try:
            self._client.send_message("/chatbox/input", [clipped, True, bool(notify)])
        except Exception as error:
            logger.warning("Failed to send OSC chatbox message: %s", error)

    def clear_chatbox(self) -> None:
        self.send_chatbox("", notify=False)


def format_tools_header(tools: list, max_named: int = 2) -> str:
    """Build a compact 'tools: [bash] [edit] (+30)' header from a tools list."""
    names: list[str] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") or {}
        name = fn.get("name") or tool.get("name")
        if name:
            names.append(str(name))
    if not names:
        return ""
    shown = names[:max_named]
    header = "tools: " + " ".join(f"[{n}]" for n in shown)
    extra = len(names) - len(shown)
    if extra > 0:
        header += f" (+{extra})"
    return header


class ChatboxRotator:
    """Cycle a long message's pages in the chatbox until stopped.

    Optional fixed `header` (e.g. a tools line) and mutable `footer` (e.g. a
    transient ``[listening]`` indicator) are rendered around each page, all kept
    within the 144-char chatbox limit. `set_footer` updates the footer and
    re-sends the current page immediately (no need to wait out the page dwell).
    """

    def __init__(self, sender: OscSender, text: str, *, header: str = "", footer: str = ""):
        self._sender = sender
        self._header = header or ""
        self._footer = footer or ""
        # Reserve room for header + (initial) footer lines so each frame fits.
        # The footer only ever shrinks during a request, so this stays safe.
        reserve = 0
        if self._header:
            reserve += len(self._header) + 1
        if self._footer:
            reserve += len(self._footer) + 1
        budget = max(1, CHATBOX_MAX_LENGTH - reserve)
        self._pages = split_into_pages(text, budget)
        if not self._pages and (self._header or self._footer):
            # Header/footer with empty body still deserves a frame.
            self._pages = [""]
        self._current_page = self._pages[0] if self._pages else ""
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _frame(self, page: str, footer: str) -> str:
        lines = []
        if self._header:
            lines.append(self._header)
        if page:
            lines.append(page)
        if footer:
            lines.append(footer)
        return "\n".join(lines)

    def start(self) -> None:
        if not self._pages:
            return
        self._thread = threading.Thread(target=self._run, name="ChatboxRotator", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # Send the first page immediately, then cycle pages forever.
        while not self._stop_event.is_set():
            for page in self._pages:
                if self._stop_event.is_set():
                    return
                with self._lock:
                    self._current_page = page
                    footer = self._footer
                frame = self._frame(page, footer)
                self._sender.send_chatbox(frame)

                deadline = time.monotonic() + estimate_page_seconds(page)
                while not self._stop_event.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    wait_seconds = min(remaining, CHATBOX_KEEPALIVE_SECONDS)
                    if self._stop_event.wait(wait_seconds):
                        return
                    if deadline - time.monotonic() > 0:
                        with self._lock:
                            footer = self._footer
                        self._sender.send_chatbox(self._frame(page, footer))

    def set_footer(self, footer: str) -> None:
        """Update the footer line and re-send the current page right away."""
        with self._lock:
            self._footer = footer or ""
            page = self._current_page
            current_footer = self._footer
        self._sender.send_chatbox(self._frame(page, current_footer))

    def stop(self, *, clear: bool = True) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        if clear:
            self._sender.clear_chatbox()


# Module-level default sender + convenience function (used by smoke tests).
_default_sender: OscSender | None = None


def get_sender() -> OscSender:
    global _default_sender
    if _default_sender is None:
        _default_sender = OscSender()
    return _default_sender


def send_chatbox(text: str, *, notify: bool = False) -> None:
    get_sender().send_chatbox(text, notify=notify)
