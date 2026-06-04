"""
Floating always-on-top overlay window: live recognition + status.

Uses Tkinter (stdlib, no extra deps). Runs on the main thread; the API server
runs on a background thread. The engine (STT events) and the API server (request
status) push events here via the thread-safe `push()` method; a Tk `after` loop
drains them and updates the UI.

Event shapes consumed by push():
    {"type": "request_start", "prompt": <str>}   # a request began; prompt sent
    {"type": "request_end",   "reply":  <str>}   # request finished; final reply
    {"type": "partial",  "text": <str>}          # live (non-final) hypothesis
    {"type": "final",    "text": <str>}          # newly finalized text
    {"type": "endpoint"}                          # Soniox endpoint detected
"""
import queue
import tkinter as tk

# Dark theme palette.
_BG = "#1e1e1e"
_HEADER_BG = "#2d2d2d"
_FG = "#f0f0f0"
_PARTIAL = "#9e9e9e"
_PROMPT = "#4fc3f7"
_REPLY = "#ffd54f"
_LISTENING = "#4caf50"
_IDLE = "#777777"
_FONT = "Microsoft YaHei UI"


class Overlay:
    def __init__(self, engine, *, opacity: float = 0.92):
        self._engine = engine
        self._opacity = opacity
        self._q: "queue.Queue[dict]" = queue.Queue()

        # Display state.
        self._prompt = ""
        self._finals = ""
        self._partial = ""
        self._reply = ""
        self._endpoint = False
        self._dirty = True

        self._drag_x = 0
        self._drag_y = 0

        self._build_ui()

    # -- public, thread-safe ------------------------------------------------
    def push(self, event: dict) -> None:
        try:
            self._q.put_nowait(event)
        except Exception:
            pass

    def run(self) -> None:
        self._root.mainloop()

    # -- UI -----------------------------------------------------------------
    def _build_ui(self) -> None:
        root = tk.Tk()
        self._root = root
        root.title("VRChat-to-API")
        root.overrideredirect(True)
        root.wm_attributes("-topmost", True)
        try:
            root.wm_attributes("-alpha", self._opacity)
        except tk.TclError:
            pass
        root.configure(bg=_BG)

        # Position: top-right corner.
        w, h = 440, 260
        sw = root.winfo_screenwidth()
        root.geometry(f"{w}x{h}+{sw - w - 24}+24")

        # Header (drag handle + status + close).
        header = tk.Frame(root, bg=_HEADER_BG, height=30)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        self._status = tk.Label(
            header, text="● 空闲", bg=_HEADER_BG, fg=_IDLE,
            font=(_FONT, 11, "bold"), anchor="w", padx=10,
        )
        self._status.pack(side="left", fill="y")

        close = tk.Label(header, text="✕", bg=_HEADER_BG, fg="#bbb", font=(_FONT, 12), padx=12)
        close.pack(side="right", fill="y")
        close.bind("<Button-1>", lambda _e: self._on_close())
        close.bind("<Enter>", lambda _e: close.config(fg="#ff5252"))
        close.bind("<Leave>", lambda _e: close.config(fg="#bbb"))

        for widget in (header, self._status):
            widget.bind("<Button-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)

        # Prompt line (message sent to VRChat).
        self._prompt_lbl = tk.Label(
            root, text="发送: —", bg=_BG, fg=_PROMPT, font=(_FONT, 10),
            anchor="w", justify="left", wraplength=w - 24, padx=12, pady=(0),
        )
        self._prompt_lbl.pack(fill="x", pady=(8, 2))

        # Recognition area (finals + live partial).
        self._text = tk.Text(
            root, bg=_BG, fg=_FG, font=(_FONT, 13), wrap="word",
            bd=0, highlightthickness=0, padx=12, pady=4, height=6,
            insertwidth=0, cursor="arrow",
        )
        self._text.tag_configure("final", foreground=_FG)
        self._text.tag_configure("partial", foreground=_PARTIAL)
        self._text.configure(state="disabled")
        self._text.pack(fill="both", expand=True)

        # Reply line (final returned value).
        self._reply_lbl = tk.Label(
            root, text="", bg=_BG, fg=_REPLY, font=(_FONT, 10),
            anchor="w", justify="left", wraplength=w - 24, padx=12, pady=6,
        )
        self._reply_lbl.pack(fill="x", side="bottom")

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._tick)

    # -- drag ---------------------------------------------------------------
    def _start_drag(self, event) -> None:
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag(self, event) -> None:
        x = self._root.winfo_x() + event.x - self._drag_x
        y = self._root.winfo_y() + event.y - self._drag_y
        self._root.geometry(f"+{x}+{y}")

    def _on_close(self) -> None:
        try:
            self._root.destroy()
        except Exception:
            pass

    # -- update loop --------------------------------------------------------
    def _tick(self) -> None:
        # Drain queued events.
        while True:
            try:
                event = self._q.get_nowait()
            except queue.Empty:
                break
            self._apply(event)

        # Listening status from the engine.
        listening = bool(self._engine and self._engine.is_running())
        if listening:
            self._status.config(text="● 监听中", fg=_LISTENING)
        else:
            self._status.config(text="● 空闲", fg=_IDLE)

        if self._dirty:
            self._render()
            self._dirty = False

        self._root.after(100, self._tick)

    def _apply(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "request_start":
            self._prompt = event.get("prompt", "")
            self._finals = ""
            self._partial = ""
            self._reply = ""
            self._endpoint = False
        elif etype == "request_end":
            self._reply = event.get("reply", "")
            self._partial = ""
        elif etype == "final":
            self._finals += event.get("text", "")
            self._partial = ""
        elif etype == "partial":
            self._partial = event.get("text", "")
        elif etype == "endpoint":
            self._endpoint = True
        self._dirty = True

    def _render(self) -> None:
        self._prompt_lbl.config(text=f"发送: {self._prompt or '—'}")

        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        if self._finals:
            self._text.insert("end", self._finals, ("final",))
        if self._partial:
            self._text.insert("end", self._partial, ("partial",))
        if not self._finals and not self._partial:
            self._text.insert("end", "（等待识别…）", ("partial",))
        self._text.see("end")
        self._text.configure(state="disabled")

        if self._reply:
            mark = " ⏹" if self._endpoint else ""
            self._reply_lbl.config(text=f"回复{mark}: {self._reply}")
        else:
            self._reply_lbl.config(text="")
