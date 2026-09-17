"""Small shared UI pieces and the formatting used across panels."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


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


class LogView(ttk.Frame):
    """Read-only, auto-scrolling text log with severity colouring."""

    COLOURS = {
        "info": "#1f2933",
        "warn": "#8a5a00",
        "error": "#a4161a",
    }

    def __init__(self, master, max_lines: int = 2000, **kwargs) -> None:
        super().__init__(master, **kwargs)
        self.max_lines = max_lines
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.text = tk.Text(
            self,
            wrap="none",
            height=8,
            state="disabled",
            font=("Consolas", 9),
            background="#fbfbfd",
            relief="flat",
            padx=8,
            pady=6,
        )
        self.text.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)

        for level, colour in self.COLOURS.items():
            self.text.tag_configure(level, foreground=colour)

    def append(self, message: str, level: str = "info") -> None:
        self.text.configure(state="normal")
        self.text.insert("end", message + "\n", level)
        # Keep memory bounded on a long batch.
        line_count = int(self.text.index("end-1c").split(".")[0])
        if line_count > self.max_lines:
            self.text.delete("1.0", f"{line_count - self.max_lines}.0")
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


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

        self.entry = ttk.Entry(tree)
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
