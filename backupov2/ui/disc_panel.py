"""The current-disc panel: what is loaded, and what is about to happen to it.

One frame that swaps between four looks driven by runner state - no disc,
countdown, working, waiting for removal - so the user always has exactly the
controls that make sense right now.

The controls are split into a primary row and a secondary row. Everything that
*advances* the batch is on top, at full size; everything that interrupts it
sits below, quieter. A row of six identical buttons made "Cancelar" exactly as
prominent as "Iniciar agora", which is not the shape of the decision.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from ..jobmodel import DiscKind
from ..runner import RunnerState
from ..strings import kind_label, state_label
from . import theme
from .widgets import format_bytes, format_duration, format_speed, tip

IDLE_STATES = {
    RunnerState.IDLE,
    RunnerState.READY_NO_DISC,
    RunnerState.DISC_SETTLING,
    RunnerState.IDENTIFYING,
    RunnerState.JOB_COMPLETE,
}

# The state title's colour, so the panel reads before it is read.
STATE_COLOURS = {
    RunnerState.WORKING: theme.BRAND_DEEP,
    RunnerState.GRACE_COUNTDOWN: theme.BRAND,
    RunnerState.PAUSED: theme.WARNING,
    RunnerState.DUPLICATE_WARNING: theme.WARNING,
    RunnerState.NO_TARGET: theme.WARNING,
    RunnerState.ERROR_HOLD: theme.DANGER,
    RunnerState.JOB_COMPLETE: theme.SUCCESS,
    RunnerState.WAIT_DISC_REMOVED: theme.BRAND,
    RunnerState.EJECTING: theme.BRAND,
}


class DiscPanel(ttk.Frame):
    def __init__(self, master, on_command: Callable[[str], None], **kwargs) -> None:
        kwargs.setdefault("style", "Card.TFrame")
        super().__init__(master, **kwargs)
        self.on_command = on_command
        self.columnconfigure(0, weight=1)

        self.state_var = tk.StringVar(value="Aguardando disco")
        self.disc_var = tk.StringVar(value="Nenhum disco carregado")
        self.detail_var = tk.StringVar(value="")
        self.target_var = tk.StringVar(value="")
        self.progress_var = tk.StringVar(value="")
        self.file_var = tk.StringVar(value="")

        # This column's content is the most variable-height thing in the
        # window: the destination box and the trouble warning each come and
        # go, and together with a long file name or error message they can
        # ask for more room than a short window has to give. A plain Frame
        # would silently clip its own buttons off the bottom in exactly the
        # moment - a disc going wrong - where reaching them matters most. A
        # Canvas scrolls instead, and the scrollbar only appears when it is
        # actually needed.
        canvas = tk.Canvas(self, background=theme.SURFACE, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        scroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        self._scrollbar = scroll
        self._canvas = canvas

        body = tk.Frame(canvas, background=theme.SURFACE, padx=12, pady=10)
        body.columnconfigure(0, weight=1)
        window = canvas.create_window((0, 0), window=body, anchor="nw")

        def sync_scrollregion(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            self._update_scrollbar()

        def sync_body_width(event) -> None:
            canvas.itemconfigure(window, width=event.width)
            self._update_scrollbar()

        body.bind("<Configure>", sync_scrollregion)
        canvas.bind("<Configure>", sync_body_width)
        canvas.bind("<MouseWheel>", self._on_mousewheel)

        tk.Label(body, text="DISCO ATUAL", font=theme.FONT_SMALL_BOLD,
                 background=theme.SURFACE, foreground=theme.INK_MUTED,
                 anchor="w").grid(row=0, column=0, sticky="w")

        self.state_label = tk.Label(
            body, textvariable=self.state_var, font=(theme.UI_FAMILY, 13, "bold"),
            background=theme.SURFACE, foreground=theme.INK, anchor="w",
            wraplength=320, justify="left",
        )
        self.state_label.grid(row=1, column=0, sticky="w", pady=(2, 0))

        tk.Label(body, textvariable=self.disc_var, font=theme.FONT_BODY_BOLD,
                 background=theme.SURFACE, foreground=theme.INK, anchor="w",
                 wraplength=320, justify="left").grid(row=2, column=0, sticky="w",
                                                      pady=(4, 0))
        tk.Label(body, textvariable=self.detail_var, font=theme.FONT_SMALL,
                 background=theme.SURFACE, foreground=theme.INK_SOFT, anchor="w",
                 wraplength=320, justify="left").grid(row=3, column=0, sticky="w")

        # -- where this disc is going -------------------------------------
        # Stacked rather than side by side: this pane is the narrow one, and a
        # folder name plus a button on one line is what clips first.
        target_card = tk.Frame(body, background=theme.BRAND_TINT, padx=10, pady=7)
        target_card.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        target_card.columnconfigure(0, weight=1)
        tk.Label(target_card, text="DESTINO DESTE DISCO", font=theme.FONT_SMALL_BOLD,
                 background=theme.BRAND_TINT, foreground=theme.BRAND).grid(
            row=0, column=0, sticky="w")
        self.send_button = ttk.Button(
            target_card, text="Trocar...", style="Quiet.TButton",
            command=lambda: self.on_command("send_to")
        )
        self.send_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self._target_card = target_card
        tk.Label(target_card, textvariable=self.target_var, font=theme.FONT_BODY_BOLD,
                 background=theme.BRAND_TINT, foreground=theme.BRAND_DEEP, anchor="w",
                 wraplength=290, justify="left").grid(row=1, column=0, columnspan=2,
                                                      sticky="ew", pady=(2, 0))
        tip(self.send_button, "Mandar este disco para outra pasta da lista  (Ctrl+D)")

        # -- progress -----------------------------------------------------
        self.bar = ttk.Progressbar(body, mode="determinate", maximum=100,
                                   style="Disc.Horizontal.TProgressbar")
        self.bar.grid(row=5, column=0, sticky="ew", pady=(8, 0))

        tk.Label(body, textvariable=self.file_var, font=theme.FONT_SMALL,
                 background=theme.SURFACE, foreground=theme.INK_SOFT, anchor="w",
                 wraplength=320, justify="left").grid(row=6, column=0, sticky="w",
                                                      pady=(6, 0))
        tk.Label(body, textvariable=self.progress_var, font=theme.FONT_SMALL,
                 background=theme.SURFACE, foreground=theme.INK_SOFT,
                 anchor="w").grid(row=7, column=0, sticky="w", pady=(2, 0))

        # Only shown once a disc is actually going badly. A permanent "this
        # disc is broken" button would invite writing off discs that were
        # merely slow.
        self.trouble_var = tk.StringVar(value="")
        self.trouble_label = tk.Label(
            body,
            textvariable=self.trouble_var,
            background=theme.DANGER_TINT,
            foreground=theme.DANGER,
            font=theme.FONT_SMALL,
            wraplength=300,
            justify="left",
            anchor="w",
            padx=10,
            pady=6,
        )
        self._trouble_shown = False

        # -- controls -----------------------------------------------------
        self.buttons = tk.Frame(body, background=theme.SURFACE)
        self.buttons.grid(row=9, column=0, sticky="ew", pady=(10, 0))

        primary = tk.Frame(self.buttons, background=theme.SURFACE)
        primary.pack(fill="x")
        self.start_button = ttk.Button(
            primary, text=f"{theme.GLYPH['play']}  Iniciar agora", style="Accent.TButton",
            command=lambda: self.on_command("start_now"))
        self.start_button.pack(side="left")
        tip(self.start_button, "Nao esperar a contagem regressiva  (Espaco)")

        self.pause_button = ttk.Button(
            primary, text=f"{theme.GLYPH['pause']}  Pausar",
            command=lambda: self.on_command("pause"))
        self.pause_button.pack(side="left", padx=(8, 0))
        tip(self.pause_button, "Segurar tudo sem perder o progresso  (Ctrl+P)")

        secondary = tk.Frame(self.buttons, background=theme.SURFACE)
        secondary.pack(fill="x", pady=(6, 0))
        self.skip_button = ttk.Button(
            secondary, text=f"{theme.GLYPH['skip']}  Pular disco", style="Quiet.TButton",
            command=lambda: self.on_command("skip_disc"))
        self.skip_button.pack(side="left")
        tip(self.skip_button, "Deixar a pasta pendente e ejetar este disco")

        self.cancel_button = ttk.Button(
            secondary, text=f"{theme.GLYPH['stop']}  Cancelar", style="Quiet.TButton",
            command=lambda: self.on_command("cancel"))
        self.cancel_button.pack(side="left", padx=(6, 0))
        tip(self.cancel_button, "Parar a copia e manter o disco na bandeja  (Esc)")

        self.eject_button = ttk.Button(
            secondary, text=f"{theme.GLYPH['eject']}  Ejetar", style="Quiet.TButton",
            command=lambda: self.on_command("eject"))
        self.eject_button.pack(side="left", padx=(6, 0))
        tip(self.eject_button, "Abrir a bandeja agora  (Ctrl+J)")

        # Its own row, not a fourth button squeezed onto the end of
        # ``secondary``: at this pane's usual width four buttons in one row
        # don't fit and the last one clips off the edge - and this is the one
        # that matters most in that exact moment, not the one to lose.
        self.danger_row = tk.Frame(self.buttons, background=theme.SURFACE)
        self.corrupt_button = ttk.Button(
            self.danger_row,
            text=f"{theme.GLYPH['warning']}  Disco defeituoso",
            style="Danger.TButton",
            command=lambda: self.on_command("mark_corrupted"),
        )
        self.corrupt_button.pack(side="left")
        tip(self.corrupt_button,
            "Parar, marcar a pasta como falha e gravar um relatorio dentro dela")

        self._body = body
        self._update_scrollbar()

    # -- scrolling ----------------------------------------------------------

    def _update_scrollbar(self) -> None:
        """Show the scrollbar only once the content is actually taller than
        the pane - most of the time this panel fits and should look like it
        never scrolls at all."""
        self._canvas.update_idletasks()
        content_height = self._body.winfo_reqheight()
        visible_height = self._canvas.winfo_height()
        if content_height > visible_height > 1:
            self._scrollbar.grid(row=0, column=1, sticky="ns")
        else:
            self._scrollbar.grid_remove()
            self._canvas.yview_moveto(0)

    def _on_mousewheel(self, event) -> None:
        if self._scrollbar.winfo_manager():
            self._canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

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
        self.state_label.configure(
            foreground=STATE_COLOURS.get(state, theme.INK))
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
        # that opens the tray safely. Not in IDLE either: that state only
        # happens with no job open, and there is no runner to ask.
        self._enable(self.eject_button, not working and state is not RunnerState.IDLE)
        self.pause_button.configure(
            text=f"{theme.GLYPH['play']}  Retomar" if paused
            else f"{theme.GLYPH['pause']}  Pausar")
        self._enable(self.pause_button, state is not RunnerState.IDLE)

        # The offer belongs to the disc being copied, so it goes away with it.
        if not working:
            self.clear_trouble()

        if state in IDLE_STATES:
            self.bar.configure(mode="determinate", maximum=100,
                               style="Disc.Horizontal.TProgressbar")
            self.bar["value"] = 100 if state is RunnerState.JOB_COMPLETE else 0
            if state is RunnerState.JOB_COMPLETE:
                self.bar.configure(style="Success.Horizontal.TProgressbar")
            self.target_var.set("")
            self._show_target_card(False)
            self.file_var.set("")
            self.progress_var.set("")
            if state is not RunnerState.IDENTIFYING:
                self.disc_var.set("Nenhum disco carregado")
                self.detail_var.set(
                    "Insira o proximo disco - ele e reconhecido sozinho."
                    if state is RunnerState.READY_NO_DISC else "")

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
        self.target_var.set(target_name)
        self._show_target_card(bool(target_name))
        if total <= 0:
            self.state_var.set("Pronto - aguardando confirmacao")
            self.bar.configure(maximum=100)
            self.bar["value"] = 0
            return
        self.state_var.set(f"Iniciando em {int(remaining) + 1} s")
        self.bar.configure(maximum=total)
        self.bar["value"] = remaining

    def show_target(self, target_name: str) -> None:
        self.target_var.set(target_name)
        self._show_target_card(bool(target_name))

    def _show_target_card(self, shown: bool) -> None:
        """Hide the destination box outright when there is no destination.

        An empty box with a live-looking "Trocar..." button in it reads as
        something being wrong rather than as nothing being loaded.
        """
        if shown:
            self._target_card.grid()
        else:
            self._target_card.grid_remove()

    def show_progress(self, event) -> None:
        self.bar.configure(maximum=100, style="Disc.Horizontal.TProgressbar")
        self.bar["value"] = event.percent
        self.file_var.set(event.current_file)
        self.progress_var.set(
            f"{event.percent:.0f}%  -  "
            f"{format_bytes(event.copied_bytes)} / {format_bytes(event.total_bytes)}"
            f"  -  {format_speed(event.bytes_per_second)}"
            f"  -  faltam {format_duration(event.seconds_remaining)}"
        )

    def show_message(self, message: str) -> None:
        self.file_var.set(message)

    def show_trouble(self, message: str) -> None:
        """Surface the way out of a disc that is not going to finish.

        Just the problem, not also what to do about it: the "Disco
        defeituoso" button appears in the same breath and says that itself.
        Spelling it out again in the message was the difference between
        fitting this panel on a 900px-tall window and clipping its own
        buttons off the bottom - exactly the moment this box exists for.
        """
        self.trouble_var.set(f"{theme.GLYPH['warning']}  {message}")
        if not self._trouble_shown:
            self.trouble_label.grid(row=8, column=0, sticky="ew", pady=(8, 0))
            self.danger_row.pack(fill="x", pady=(6, 0))
            self._trouble_shown = True

    def clear_trouble(self) -> None:
        if not self._trouble_shown:
            return
        self.trouble_var.set("")
        self.trouble_label.grid_remove()
        self.danger_row.pack_forget()
        self._trouble_shown = False

    @staticmethod
    def _enable(button: ttk.Button, enabled: bool) -> None:
        button.configure(state="normal" if enabled else "disabled")
