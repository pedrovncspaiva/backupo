"""Small shared UI pieces and the formatting used across panels."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import theme


def format_bytes(size: float) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}".replace(".", ",")
        value /= 1024
    return f"{value:.1f} TB".replace(".", ",")


def format_speed(bytes_per_second: float) -> str:
    if bytes_per_second <= 0:
        return "-"
    return f"{format_bytes(bytes_per_second)}/s"


def format_duration(seconds: float | None) -> str:
    """Compact pt-BR duration: 45s, 3m20, 1h05."""
    if seconds is None or seconds < 0:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}"


class Card(tk.Frame):
    """A white panel with a hairline border - the app's one container shape.

    A plain ``tk.Frame`` with a 1px solid border rather than a ttk.Frame,
    because ttk's clam borders are drawn as a 3D relief and cannot be given a
    colour of their own.
    """

    def __init__(self, master, padding: int | tuple[int, int] = 14, **kwargs) -> None:
        pad_x, pad_y = (padding, padding) if isinstance(padding, int) else padding
        kwargs.setdefault("background", theme.SURFACE)
        kwargs.setdefault("highlightbackground", theme.BORDER)
        kwargs.setdefault("highlightcolor", theme.BORDER)
        kwargs.setdefault("highlightthickness", 1)
        kwargs.setdefault("bd", 0)
        super().__init__(master, padx=pad_x, pady=pad_y, **kwargs)


class Tooltip:
    """Hover help for a control whose label had to stay short.

    Deliberately plain: a borderless Toplevel that follows the pointer, shown
    after a short delay so sweeping the mouse across a toolbar does not strobe.
    """

    DELAY_MS = 450

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self._after: str | None = None
        self._tip: tk.Toplevel | None = None

        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def set_text(self, text: str) -> None:
        self.text = text

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self) -> None:
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return

        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tip,
            text=self.text,
            background=theme.INK,
            foreground="#ffffff",
            font=theme.FONT_SMALL,
            justify="left",
            padx=9,
            pady=6,
            bd=0,
        ).pack()
        try:
            tip.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        self._tip = tip

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            tip, self._tip = self._tip, None
            tip.destroy()


def tip(widget: tk.Widget, text: str) -> tk.Widget:
    """Attach a tooltip and give the widget straight back, for chaining."""
    Tooltip(widget, text)
    return widget


class Chip(tk.Label):
    """A small pill for a count or a state, beside the thing it describes."""

    def __init__(self, master, text: str = "", tone: str = "neutral", **kwargs) -> None:
        super().__init__(
            master,
            text=text,
            font=theme.FONT_SMALL_BOLD,
            padx=8,
            pady=2,
            bd=0,
            **kwargs,
        )
        self.set_tone(tone)

    TONES = {
        "neutral": (theme.CANVAS, theme.INK_SOFT),
        "brand": (theme.BRAND_TINT, theme.BRAND_DEEP),
        "success": (theme.SUCCESS_TINT, theme.SUCCESS),
        "warning": (theme.WARNING_TINT, theme.WARNING),
        "danger": (theme.DANGER_TINT, theme.DANGER),
    }

    def set_tone(self, tone: str) -> None:
        background, foreground = self.TONES.get(tone, self.TONES["neutral"])
        self.configure(background=background, foreground=foreground)


class LogView(ttk.Frame):
    """Read-only, auto-scrolling text log with severity colouring.

    Each line is stamped with the time it arrived: a batch runs for hours, and
    "when did that share drop out?" is the question the log is read to answer.
    """

    COLOURS = {
        "info": theme.INK,
        "warn": theme.WARNING,
        "error": theme.DANGER,
    }

    def __init__(self, master, max_lines: int = 2000, **kwargs) -> None:
        super().__init__(master, style="Card.TFrame", **kwargs)
        self.max_lines = max_lines
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._follow = True

        self.text = tk.Text(
            self,
            wrap="none",
            height=4,
            state="disabled",
            font=theme.FONT_MONO,
            background=theme.SURFACE,
            foreground=theme.INK,
            selectbackground=theme.BRAND_TINT_2,
            selectforeground=theme.INK,
            relief="flat",
            highlightthickness=0,
            padx=10,
            pady=8,
        )
        self.text.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)

        for level, colour in self.COLOURS.items():
            self.text.tag_configure(level, foreground=colour)
        self.text.tag_configure("warn", foreground=theme.WARNING,
                                background=theme.WARNING_TINT)
        self.text.tag_configure("error", foreground=theme.DANGER,
                                background=theme.DANGER_TINT)
        self.text.tag_configure("stamp", foreground=theme.INK_MUTED)

        # Scrolling up is a request to read; following again is a click away.
        self.text.bind("<MouseWheel>", self._maybe_unfollow, add="+")
        self.text.bind("<Button-4>", self._maybe_unfollow, add="+")
        self.text.bind("<Button-5>", self._maybe_unfollow, add="+")

    def append(self, message: str, level: str = "info") -> None:
        from time import strftime

        self.text.configure(state="normal")
        self.text.insert("end", strftime("%H:%M:%S  "), "stamp")
        self.text.insert("end", message + "\n", level)
        # Keep memory bounded on a long batch.
        line_count = int(self.text.index("end-1c").split(".")[0])
        if line_count > self.max_lines:
            self.text.delete("1.0", f"{line_count - self.max_lines}.0")
        if self._follow:
            self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._follow = True

    def contents(self) -> str:
        return self.text.get("1.0", "end-1c")

    def follow_end(self) -> None:
        self._follow = True
        self.text.see("end")

    def _maybe_unfollow(self, _event=None) -> None:
        # yview()[1] is where the bottom of the visible window sits, 1.0 being
        # the end of the text.
        self._follow = self.text.yview()[1] > 0.999


class CellEditor:
    """An Entry floated over a Treeview cell for inline renaming.

    Placed rather than gridded so the tree layout is untouched; it destroys
    itself on commit, Escape, or focus loss.
    """

    def __init__(self, tree: ttk.Treeview, item: str, column: str, value: str, on_commit) -> None:
        self.tree = tree
        self.item = item
        self.on_commit = on_commit

        bbox = tree.bbox(item, column)
        if not bbox:
            self.entry = None
            return
        x, y, width, height = bbox

        self.entry = ttk.Entry(tree, font=theme.FONT_BODY)
        self.entry.insert(0, value)
        self.entry.select_range(0, "end")
        self.entry.place(x=x, y=y, width=width, height=height)
        self.entry.focus_set()

        self.entry.bind("<Return>", self._commit)
        self.entry.bind("<Escape>", self._cancel)
        self.entry.bind("<FocusOut>", self._commit)

    def _commit(self, _event=None) -> None:
        if self.entry is None:
            return
        value = self.entry.get()
        self._destroy()
        self.on_commit(self.item, value)

    def _cancel(self, _event=None) -> None:
        self._destroy()

    def _destroy(self) -> None:
        if self.entry is not None:
            entry, self.entry = self.entry, None
            entry.destroy()
