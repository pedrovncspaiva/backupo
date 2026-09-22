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

# The subset with nothing to show a bar about. JOB_COMPLETE is idle too, but
# its bar is full and green, and that is the whole point of it.
QUIET_STATES = IDLE_STATES - {RunnerState.JOB_COMPLETE}

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
            target_card, text="Trocar...", style="Tint.TButton",
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
        # Bar and its two captions travel together, because with no disc in
        # the drive they are an empty trough over two blank lines - which
        # reads as "0% copied" rather than "nothing loaded", and pushes the
        # buttons a third of the way down an otherwise empty pane.
        self.progress_block = tk.Frame(body, background=theme.SURFACE)
        self.progress_block.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        self.progress_block.columnconfigure(0, weight=1)

        self.bar = ttk.Progressbar(self.progress_block, mode="determinate", maximum=100,
                                   style="Disc.Horizontal.TProgressbar")
        self.bar.grid(row=0, column=0, sticky="ew")

        tk.Label(self.progress_block, textvariable=self.file_var, font=theme.FONT_SMALL,
                 background=theme.SURFACE, foreground=theme.INK_SOFT, anchor="w",
                 wraplength=320, justify="left").grid(row=1, column=0, sticky="w",
                                                      pady=(6, 0))
        tk.Label(self.progress_block, textvariable=self.progress_var, font=theme.FONT_SMALL,
                 background=theme.SURFACE, foreground=theme.INK_SOFT,
                 anchor="w").grid(row=2, column=0, sticky="w", pady=(2, 0))
        self.progress_block.grid_remove()

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
            wraplength=290,
            justify="left",
            anchor="w",
            padx=10,
            pady=6,
            compound="left",
        )
        # The same mark the "Disco defeituoso" button below it carries, rather
        # than a Unicode stand-in that renders as a different shape.
        self._trouble_icon = theme.load_icon(self, "warning", tone="danger")
        if self._trouble_icon is not None:
            self.trouble_label.configure(image=self._trouble_icon, padx=10)
        self._trouble_shown = False

        # -- controls -----------------------------------------------------
        self.buttons = tk.Frame(body, background=theme.SURFACE)
        self.buttons.grid(row=9, column=0, sticky="ew", pady=(10, 0))

        primary = tk.Frame(self.buttons, background=theme.SURFACE)
        primary.pack(fill="x")
        self.start_button = ttk.Button(
            primary, style="Accent.TButton",
            command=lambda: self.on_command("start_now"))
        theme.set_button_icon(self.start_button, "play", "Iniciar agora")
        self.start_button.pack(side="left")
        tip(self.start_button, "Nao esperar a contagem regressiva  (Espaco)")

        self.pause_button = ttk.Button(
            primary,
            command=lambda: self.on_command("pause"))
        theme.set_button_icon(self.pause_button, "pause", "Pausar")
        self.pause_button.pack(side="left", padx=(8, 0))
        tip(self.pause_button, "Segurar tudo sem perder o progresso  (Ctrl+P)")

        secondary = tk.Frame(self.buttons, background=theme.SURFACE)
        secondary.pack(fill="x", pady=(6, 0))
        self.skip_button = ttk.Button(
            secondary, style="Quiet.TButton",
            command=lambda: self.on_command("skip_disc"))
        theme.set_button_icon(self.skip_button, "skip", "Pular disco")
        self.skip_button.pack(side="left")
        tip(self.skip_button, "Deixar a pasta pendente e ejetar este disco")

        self.cancel_button = ttk.Button(
            secondary, style="Quiet.TButton",
            command=lambda: self.on_command("cancel"))
        theme.set_button_icon(self.cancel_button, "stop", "Cancelar")
        self.cancel_button.pack(side="left", padx=(6, 0))
        tip(self.cancel_button, "Parar a copia e manter o disco na bandeja  (Esc)")

        self.eject_button = ttk.Button(
            secondary, style="Quiet.TButton",
            command=lambda: self.on_command("eject"))
        theme.set_button_icon(self.eject_button, "eject", "Ejetar")
        self.eject_button.pack(side="left", padx=(6, 0))
        tip(self.eject_button, "Abrir a bandeja agora  (Ctrl+J)")

        # Its own row, not a fourth button squeezed onto the end of
        # ``secondary``: at this pane's usual width four buttons in one row
        # don't fit and the last one clips off the edge - and this is the one
        # that matters most in that exact moment, not the one to lose.
        self.danger_row = tk.Frame(self.buttons, background=theme.SURFACE)
        self.corrupt_button = ttk.Button(
            self.danger_row,
            style="Danger.TButton",
            command=lambda: self.on_command("mark_corrupted"),
        )
        theme.set_button_icon(self.corrupt_button, "warning", "Disco defeituoso")
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

    def _reveal_controls(self) -> None:
        """Scroll the pane to its buttons, if it has become tall enough to
        need scrolling at all."""
        self._update_scrollbar()
        if self._scrollbar.winfo_manager():
            self._canvas.yview_moveto(1.0)

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
        if paused:
            theme.set_button_icon(self.pause_button, "play", "Retomar")
        else:
            theme.set_button_icon(self.pause_button, "pause", "Pausar")
        self._enable(self.pause_button, state is not RunnerState.IDLE)

        # The offer belongs to the disc being copied, so it goes away with it.
        if not working:
            self.clear_trouble()

        self._show_progress_block(state not in QUIET_STATES)
        # IDLE only happens with no job open, and then not one of these six
        # buttons can ever do anything - there is no runner behind them. Six
        # greyed-out controls is the first thing the eye lands on in this pane
        # on a fresh window, so they go away with the job instead.
        self._show_buttons(state is not RunnerState.IDLE)

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
        self._show_progress_block(True)
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

    def _show_progress_block(self, shown: bool) -> None:
        if shown:
            self.progress_block.grid()
        else:
            self.progress_block.grid_remove()

    def _show_buttons(self, shown: bool) -> None:
        if shown:
            self.buttons.grid()
        else:
            self.buttons.grid_remove()

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
        self._show_progress_block(True)
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
        self._show_progress_block(True)
        self.file_var.set(message)

    def show_trouble(self, message: str) -> None:
        """Surface the way out of a disc that is not going to finish.

        Just the problem, not also what to do about it: the "Disco
        defeituoso" button appears in the same breath and says that itself.
        Spelling it out again in the message was the difference between
        fitting this panel on a 900px-tall window and clipping its own
        buttons off the bottom - exactly the moment this box exists for.
        """
        # tk.Label has no padding between a compound image and its text, so
        # the gap is in the string.
        self.trouble_var.set(f"  {message}" if self._trouble_icon is not None else message)
        if not self._trouble_shown:
            self.trouble_label.grid(row=8, column=0, sticky="ew", pady=(8, 0))
            self.danger_row.pack(fill="x", pady=(6, 0))
            self._trouble_shown = True
            # The warning box and the button that answers it are the two
            # tallest things this pane ever holds, and they arrive together -
            # so on a short window the button lands below the fold. A
            # scrollbar the user has not noticed is the same as no button at
            # all, so the pane scrolls itself to the controls. after_idle,
            # because the two widgets above have not been measured yet.
            self.after_idle(self._reveal_controls)

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
