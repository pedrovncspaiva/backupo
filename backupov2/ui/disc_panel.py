"""The current-disc panel: what is loaded, and what is about to happen to it.

One frame that swaps between four looks driven by runner state - no disc,
countdown, working, waiting for removal - so the user always has exactly the
controls that make sense right now.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from ..jobmodel import DiscKind
from ..runner import RunnerState
from ..strings import kind_label, state_label
from .widgets import format_bytes, format_duration, format_speed

TROUBLE_BG = "#fee2e2"
TROUBLE_FG = "#991b1b"

IDLE_STATES = {
    RunnerState.IDLE,
    RunnerState.READY_NO_DISC,
    RunnerState.DISC_SETTLING,
    RunnerState.IDENTIFYING,
    RunnerState.JOB_COMPLETE,
}


class DiscPanel(ttk.LabelFrame):
    def __init__(self, master, on_command: Callable[[str], None], **kwargs) -> None:
        super().__init__(master, text="Disco atual", padding=12, **kwargs)
        self.on_command = on_command
        self.columnconfigure(0, weight=1)

        self.state_var = tk.StringVar(value="Aguardando disco")
        self.disc_var = tk.StringVar(value="Nenhum disco carregado")
        self.detail_var = tk.StringVar(value="")
        self.target_var = tk.StringVar(value="")
        self.progress_var = tk.StringVar(value="")
        self.file_var = tk.StringVar(value="")

        ttk.Label(
            self, textvariable=self.state_var, font=("Segoe UI", 12, "bold")
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(self, textvariable=self.disc_var, font=("Segoe UI", 10)).grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Label(self, textvariable=self.detail_var, foreground="#4b5563").grid(
            row=2, column=0, sticky="w"
        )

        target_row = ttk.Frame(self)
        target_row.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        target_row.columnconfigure(0, weight=1)
        ttk.Label(
            target_row, textvariable=self.target_var, font=("Segoe UI", 10, "bold")
        ).grid(row=0, column=0, sticky="w")
        self.send_button = ttk.Button(
            target_row, text="Enviar para...", command=lambda: self.on_command("send_to")
        )
        self.send_button.grid(row=0, column=1, sticky="e")

        self.bar = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.bar.grid(row=4, column=0, sticky="ew", pady=(10, 0))

        ttk.Label(self, textvariable=self.file_var, foreground="#4b5563").grid(
            row=5, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Label(self, textvariable=self.progress_var, foreground="#4b5563").grid(
            row=6, column=0, sticky="w"
        )

        # Only shown once a disc is actually going badly. A permanent "this
        # disc is broken" button would invite writing off discs that were
        # merely slow.
        self.trouble_var = tk.StringVar(value="")
        self.trouble_label = tk.Label(
            self,
            textvariable=self.trouble_var,
            background=TROUBLE_BG,
            foreground=TROUBLE_FG,
            wraplength=360,
            justify="left",
            anchor="w",
            padx=8,
            pady=6,
        )
        self._trouble_shown = False

        self.buttons = ttk.Frame(self)
        self.buttons.grid(row=8, column=0, sticky="ew", pady=(12, 0))

        self.start_button = ttk.Button(
            self.buttons, text="Iniciar agora", command=lambda: self.on_command("start_now")
        )
        self.skip_button = ttk.Button(
            self.buttons, text="Pular disco", command=lambda: self.on_command("skip_disc")
        )
        self.pause_button = ttk.Button(
            self.buttons, text="Pausar", command=lambda: self.on_command("pause")
        )
        self.cancel_button = ttk.Button(
            self.buttons, text="Cancelar", command=lambda: self.on_command("cancel")
        )
        self.eject_button = ttk.Button(
            self.buttons, text="Ejetar", command=lambda: self.on_command("eject")
        )
        for button in (
            self.start_button,
            self.skip_button,
            self.pause_button,
            self.cancel_button,
            self.eject_button,
        ):
            button.pack(side="left", padx=(0, 8))

        self.corrupt_button = ttk.Button(
            self.buttons,
            text="Disco defeituoso",
            command=lambda: self.on_command("mark_corrupted"),
        )

    # -- updates ----------------------------------------------------------

    def show_state(
        self, state: RunnerState, detail: str = "", paused: bool = False
    ) -> None:
        """Light up exactly the controls that mean something right now.

        ``paused`` is passed in rather than derived from ``state`` because a
        held copy stays in WORKING - the entry is still mid-write - so the
        state alone cannot say whether the button should read Pausar or
        Retomar.
        """
        self.state_var.set(detail or state_label(state))
        working = state is RunnerState.WORKING
        countdown = state is RunnerState.GRACE_COUNTDOWN
        choosing = state in (RunnerState.DUPLICATE_WARNING, RunnerState.NO_TARGET)
        idle_paused = state is RunnerState.PAUSED

        self._enable(self.start_button, countdown or choosing)
        # During a copy all three ways out are available: skip stops it and
        # ejects, pause holds it, cancel stops it and keeps the disc.
        self._enable(self.skip_button, countdown or choosing or idle_paused or working)
        self._enable(self.send_button, countdown or choosing or idle_paused)
        self._enable(self.cancel_button, working)
        # Not while copying - the drive is in use, and skip is the way out
        # that opens the tray safely.
        self._enable(self.eject_button, not working)
        self.pause_button.configure(text="Retomar" if paused else "Pausar")
        self._enable(self.pause_button, True)

        # The offer belongs to the disc being copied, so it goes away with it.
        if not working:
            self.clear_trouble()

        if state in IDLE_STATES:
            self.bar.configure(mode="determinate", maximum=100)
            self.bar["value"] = 0
            self.target_var.set("")
            self.file_var.set("")
            self.progress_var.set("")
            if state is not RunnerState.IDENTIFYING:
                self.disc_var.set("Nenhum disco carregado")
                self.detail_var.set("")

    def show_disc(self, media, kind: DiscKind, reason: str = "") -> None:
        self.disc_var.set(f"{media.display_name}   [{kind_label(kind).upper()}]")
        serial = f"serie 0x{media.serial:08X}" if media.serial else "sem numero de serie"
        extra = f" - {reason}" if reason else ""
        self.detail_var.set(f"{serial}{extra}")

    def show_size(self, file_count: int, total_bytes: int) -> None:
        current = self.detail_var.get().split("  |  ")[0]
        self.detail_var.set(
            f"{current}  |  {file_count} arquivo(s), {format_bytes(total_bytes)}"
        )

    def show_countdown(self, remaining: float, total: float, target_name: str) -> None:
        self.target_var.set(f"-> {target_name}")
        if total <= 0:
            self.state_var.set("Pronto - aguardando confirmacao")
            self.bar.configure(maximum=100)
            self.bar["value"] = 0
            return
        self.state_var.set(f"Iniciando em {int(remaining) + 1} s")
        self.bar.configure(maximum=total)
        self.bar["value"] = remaining

    def show_target(self, target_name: str) -> None:
        self.target_var.set(f"-> {target_name}")

    def show_progress(self, event) -> None:
        self.bar.configure(maximum=100)
        self.bar["value"] = event.percent
        self.file_var.set(event.current_file)
        self.progress_var.set(
            f"{format_bytes(event.copied_bytes)} / {format_bytes(event.total_bytes)}"
            f"  -  {format_speed(event.bytes_per_second)}"
            f"  -  faltam {format_duration(event.seconds_remaining)}"
        )

    def show_message(self, message: str) -> None:
        self.file_var.set(message)

    def show_trouble(self, message: str) -> None:
        """Surface the way out of a disc that is not going to finish."""
        self.trouble_var.set(
            f"{message}\nVoce pode marcar este disco como defeituoso e seguir "
            "para o proximo."
        )
        if not self._trouble_shown:
            self.trouble_label.grid(row=7, column=0, sticky="ew", pady=(10, 0))
            self.corrupt_button.pack(side="left", padx=(0, 8))
            self._trouble_shown = True

    def clear_trouble(self) -> None:
        if not self._trouble_shown:
            return
        self.trouble_var.set("")
        self.trouble_label.grid_remove()
        self.corrupt_button.pack_forget()
        self._trouble_shown = False

    @staticmethod
    def _enable(button: ttk.Button, enabled: bool) -> None:
        button.configure(state="normal" if enabled else "disabled")
