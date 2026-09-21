"""The main window.

Threading discipline, enforced: worker threads only ever put things on the
runner's queue, and the single ``after()`` pump below is the only place any Tk
widget is touched. Nothing else in the package imports tkinter.

Layout, top to bottom: a brand bar, then the *stage* - either the "new job"
card or the open batch's card, never both - then the toolbar, the folder list
beside the disc panel, the log tabs, and a status bar. The stage swap is the
whole idea: setup fields that no longer do anything are removed rather than
greyed out, so what is on screen is always what you can act on.
"""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import winapi
from ..core import DEFECT_REPORT_NAME, ensure_entry_folders, validate_folder_name
from ..jobmodel import EntryStatus, JobSettings
from ..jobstore import JobStore, control_files, load_recent, remove_control_files
from ..reconcile import apply_resolution, reconcile
from ..runner import (
    CountdownTick,
    DiscDetected,
    DiscFailed,
    DiscFinished,
    DiscSized,
    DiscTrouble,
    DuplicateWarning,
    Ejected,
    EntryUpdated,
    JobComplete,
    JobRunner,
    LogLine,
    NoTarget,
    Progress,
    RunnerState,
    StateChanged,
    ThreadExecutor,
    Win32Ejector,
)
from ..media import Win32Scanner
from ..strings import APP_TITLE, state_label
from . import theme
from .dialogs import (
    AddEntriesDialog,
    MarkDefectiveDialog,
    ReconcileDialog,
    SendToDialog,
)
from .guide import AboutDialog, HelpWindow
from .menubar import MenuBar
from .photo_import import PhotoImportDialog
from .disc_panel import DiscPanel
from .entries_view import EntriesView
from .widgets import Card, Chip, LogView, format_bytes, tip

POLL_MS = 1000
TICK_MS = 100

# How much of the window the disc panel keeps when the window first opens.
DISC_PANEL_WIDTH = 380

# How the drive's state reads in the status bar.
STATE_TONES = {
    RunnerState.IDLE: "neutral",
    RunnerState.READY_NO_DISC: "neutral",
    RunnerState.DISC_SETTLING: "brand",
    RunnerState.IDENTIFYING: "brand",
    RunnerState.DUPLICATE_WARNING: "warning",
    RunnerState.NO_TARGET: "warning",
    RunnerState.GRACE_COUNTDOWN: "brand",
    RunnerState.WORKING: "brand",
    RunnerState.EJECTING: "brand",
    RunnerState.WAIT_DISC_REMOVED: "brand",
    RunnerState.PAUSED: "warning",
    RunnerState.ERROR_HOLD: "danger",
    RunnerState.JOB_COMPLETE: "success",
}

# Widget classes that own the keyboard while focused. Single-key shortcuts
# (Space, Delete) must not fire while a name is being typed.
TYPING_CLASSES = {"Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"}


class BackupApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1280x900")
        self.minsize(1000, 780)

        winapi.silence_missing_media_dialogs()
        theme.apply_theme(self)
        theme.apply_window_icon(self)

        self.store: JobStore | None = None
        self.runner: JobRunner | None = None
        self.current_media = None
        self._help_window: HelpWindow | None = None

        self.destination_var = tk.StringVar()
        self.parent_var = tk.StringVar()
        self.saved_var = tk.StringVar(value="")
        self.job_title_var = tk.StringVar(value="Nenhum trabalho aberto")
        self.job_path_var = tk.StringVar(
            value="Escolha o Destino e crie um trabalho, ou importe de fotos."
        )
        self.status_var = tk.StringVar(value="Pronto.")
        self.auto_var = tk.BooleanVar(value=True)
        self.show_log_var = tk.BooleanVar(value=True)
        self.percent_var = tk.StringVar(value="")

        self._build()
        self.menubar = MenuBar(self)
        self._refresh_recent()
        self._set_controls_enabled(False)
        self._paint_header()
        self._centre()

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(TICK_MS, self._pump)
        self.after(POLL_MS, self._poll)

    def _centre(self) -> None:
        """Open centred, and never taller than the screen it opens on.

        The status bar is the last row, so a window that overflows loses it
        first - and losing it is silent.
        """
        self.update_idletasks()
        width = min(1280, self.winfo_screenwidth() - 80)
        height = min(900, self.winfo_screenheight() - 110)
        x = (self.winfo_screenwidth() - width) // 2
        y = max(0, (self.winfo_screenheight() - height) // 2 - 30)
        self.geometry(f"{width}x{height}+{x}+{y}")

    # -- layout -----------------------------------------------------------

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=3)  # the folder list gets the space
        self.rowconfigure(5, weight=2)

        self._build_brand_bar()
        self._build_job_card()
        self._build_setup_card()
        self._build_toolbar()
        self._build_middle()
        self._build_logs()
        self._build_status_bar()
        self._bind_keys()
        self.disc_panel.show_state(RunnerState.IDLE, "Nenhum trabalho aberto")

    # -- brand bar ---------------------------------------------------------

    def _build_brand_bar(self) -> None:
        """Name, mark and one-line purpose. Fixed, so the app is identifiable
        in a screenshot and in a taskbar full of windows."""
        bar = tk.Frame(self, background=theme.SURFACE, padx=16, pady=10,
                       highlightthickness=1, highlightbackground=theme.BORDER)
        bar.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 0))
        bar.columnconfigure(2, weight=1)

        logo = theme.load_logo(self, 32)
        if logo is not None:
            tk.Label(bar, image=logo, background=theme.SURFACE).grid(
                row=0, column=0, rowspan=2, padx=(0, 12)
            )

        tk.Label(bar, text=APP_TITLE, font=theme.FONT_SUBTITLE,
                 background=theme.SURFACE, foreground=theme.INK).grid(
            row=0, column=1, sticky="w")
        tk.Label(bar, text="Um disco, uma pasta - do comeco ao fim do lote.",
                 font=theme.FONT_SMALL, background=theme.SURFACE,
                 foreground=theme.INK_MUTED).grid(row=1, column=1, sticky="w")

        right = tk.Frame(bar, background=theme.SURFACE)
        right.grid(row=0, column=3, rowspan=2, sticky="e")
        tk.Label(right, text="SONDOTECNICA", font=(theme.UI_FAMILY, 11, "bold"),
                 background=theme.SURFACE, foreground=theme.BRAND).pack(side="left",
                                                                       padx=(0, 14))
        tip(
            ttk.Button(right, text=f"{theme.GLYPH['help']}  Ajuda", style="Quiet.TButton",
                       command=self._show_help),
            "Guias curtos para cada situacao  (F1)",
        ).pack(side="left")

    # -- the stage: job card, or setup card --------------------------------

    def _build_job_card(self) -> None:
        card = Card(self, padding=(16, 12))
        card.grid(row=1, column=0, sticky="ew", padx=12, pady=(8, 0))
        card.columnconfigure(0, weight=1)
        self.header = card

        self.title_label = tk.Label(
            card, textvariable=self.job_title_var, font=theme.FONT_DISPLAY,
            background=theme.SURFACE, foreground=theme.BRAND_DEEP, anchor="w",
        )
        self.title_label.grid(row=0, column=0, sticky="w")

        header_buttons = tk.Frame(card, background=theme.SURFACE)
        header_buttons.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(12, 0))
        # End-of-batch actions, in the order a batch actually ends: look at
        # it, tidy it, put it away.
        self.clean_button = ttk.Button(
            header_buttons, text=f"{theme.GLYPH['clean']}  Limpar controle",
            style="Quiet.TButton", command=self._clean_control_files,
        )
        self.clean_button.pack(side="left", padx=(0, 8))
        tip(self.clean_button,
            "Remove os arquivos de bookkeeping da pasta do lote, antes de entregar")

        self.open_folder_button = ttk.Button(
            header_buttons, text=f"{theme.GLYPH['folder']}  Abrir pasta",
            command=self._open_parent_folder,
        )
        self.open_folder_button.pack(side="left")
        tip(self.open_folder_button, "Abrir a pasta do lote no Explorer  (Ctrl+E)")
        # The only way to get "Criar trabalho" enabled again without closing
        # the whole app - finishing a batch leaves the setup fields disabled
        # with no button of their own to undo that.
        self.close_job_button = ttk.Button(
            header_buttons, text=f"{theme.GLYPH['close']}  Fechar trabalho",
            command=self._close_job,
        )
        self.close_job_button.pack(side="left", padx=(8, 0))
        tip(self.close_job_button,
            "Liberar a tela para o proximo lote. Nada e apagado  (Ctrl+W)")

        # Clickable, because the path being right there and not openable is a
        # small daily annoyance on a network share.
        self.path_label = tk.Label(
            card, textvariable=self.job_path_var, font=theme.FONT_MONO,
            background=theme.SURFACE, foreground=theme.INK_SOFT, anchor="w",
            cursor="hand2",
        )
        self.path_label.grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.path_label.bind("<Button-1>", lambda e: self._open_parent_folder())
        self.path_label.bind(
            "<Enter>", lambda e: self.path_label.configure(foreground=theme.BRAND))
        self.path_label.bind(
            "<Leave>", lambda e: self.path_label.configure(foreground=theme.INK_SOFT))

        progress_row = tk.Frame(card, background=theme.SURFACE)
        progress_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        progress_row.columnconfigure(0, weight=1)
        self.job_progress = ttk.Progressbar(
            progress_row, mode="determinate", maximum=100,
            style="Header.Horizontal.TProgressbar",
        )
        self.job_progress.grid(row=0, column=0, sticky="ew")
        tk.Label(progress_row, textvariable=self.percent_var, font=theme.FONT_SMALL_BOLD,
                 background=theme.SURFACE, foreground=theme.BRAND_DEEP, width=5,
                 anchor="e").grid(row=0, column=1, padx=(10, 0))

        # Counts as chips: three numbers that each mean something different
        # read better side by side than in one run-on sentence.
        self.chips = tk.Frame(card, background=theme.SURFACE)
        self.chips.grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # Only shown while a folder is collecting. It overrides the list order
        # for every disc, so it gets its own strip with its own off switch
        # rather than being a flag you have to go looking for in the table.
        self.collect_bar = tk.Frame(card, background=theme.AMBER_TINT, padx=12, pady=7,
                                    highlightthickness=1,
                                    highlightbackground=theme.AMBER)
        self.collect_bar.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.collect_bar.columnconfigure(1, weight=1)
        self.collect_var = tk.StringVar(value="")
        tk.Label(self.collect_bar, text=theme.GLYPH["warning"],
                 background=theme.AMBER_TINT, foreground=theme.AMBER_DEEP,
                 font=(theme.UI_FAMILY, 13)).grid(row=0, column=0, padx=(0, 10))
        tk.Label(
            self.collect_bar, textvariable=self.collect_var,
            background=theme.AMBER_TINT, foreground=theme.AMBER_DEEP,
            font=theme.FONT_SMALL_BOLD, anchor="w", justify="left",
        ).grid(row=0, column=1, sticky="w")
        ttk.Button(self.collect_bar, text="Parar de acumular",
                   command=self._stop_collecting).grid(row=0, column=2, sticky="e",
                                                       padx=(12, 0))
        self.collect_bar.grid_remove()

    def _build_setup_card(self) -> None:
        """Shown only while nothing is open - the first screen of the app.

        Numbered, because the order matters and getting it wrong is the most
        common way a first batch goes sideways.
        """
        card = Card(self, padding=(16, 14))
        card.grid(row=2, column=0, sticky="ew", padx=12, pady=(8, 0))
        card.columnconfigure(2, weight=1)
        self.setup = card

        tk.Label(card, text="Comece um trabalho", font=theme.FONT_TITLE,
                 background=theme.SURFACE, foreground=theme.BRAND_DEEP).grid(
            row=0, column=0, columnspan=4, sticky="w")
        tk.Label(card,
                 text="Escolha onde gravar, de um nome ao lote e cole a lista de pastas.",
                 font=theme.FONT_SMALL, background=theme.SURFACE,
                 foreground=theme.INK_MUTED).grid(row=1, column=0, columnspan=4,
                                                  sticky="w", pady=(2, 12))

        tk.Label(card, text="1", font=theme.FONT_SMALL_BOLD, background=theme.BRAND_TINT,
                 foreground=theme.BRAND_DEEP, width=3).grid(row=2, column=0, sticky="w",
                                                            pady=4)
        tk.Label(card, text="Destino", font=theme.FONT_BODY_BOLD,
                 background=theme.SURFACE, foreground=theme.INK).grid(
            row=2, column=1, sticky="w", padx=(10, 12))
        self.destination_entry = ttk.Entry(card, textvariable=self.destination_var,
                                           font=theme.FONT_BODY)
        self.destination_entry.grid(row=2, column=2, sticky="ew", pady=4)
        tip(self.destination_entry, "Onde o backup sera gravado - em geral uma pasta de rede")
        self.browse_button = ttk.Button(card, text="Procurar...", command=self._browse)
        self.browse_button.grid(row=2, column=3, padx=(10, 0))

        tk.Label(card, text="2", font=theme.FONT_SMALL_BOLD, background=theme.BRAND_TINT,
                 foreground=theme.BRAND_DEEP, width=3).grid(row=3, column=0, sticky="w",
                                                            pady=4)
        tk.Label(card, text="Pasta do lote", font=theme.FONT_BODY_BOLD,
                 background=theme.SURFACE, foreground=theme.INK).grid(
            row=3, column=1, sticky="w", padx=(10, 12))
        self.parent_entry = ttk.Entry(card, textvariable=self.parent_var,
                                      font=theme.FONT_BODY)
        self.parent_entry.grid(row=3, column=2, sticky="ew", pady=4)
        tip(self.parent_entry, "Ex.: CAIXA 02 (EG 1841 ao EG 1852)")
        tk.Label(card, text="ou deixe em branco\ne use as fotos", font=theme.FONT_SMALL,
                 background=theme.SURFACE, foreground=theme.INK_MUTED,
                 justify="left").grid(row=3, column=3, sticky="w", padx=(10, 0))

        actions = tk.Frame(card, background=theme.SURFACE)
        actions.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        self.new_button = ttk.Button(actions, text="Criar trabalho",
                                     style="Accent.TButton", command=self._new_job)
        self.new_button.pack(side="left")
        tip(self.new_button, "Cria a pasta do lote e pede a lista de pastas  (Ctrl+N)")

        open_button = ttk.Button(actions, text=f"{theme.GLYPH['open']}  Abrir...",
                                 command=self._open_job)
        open_button.pack(side="left", padx=(8, 0))
        tip(open_button, "Retomar um trabalho salvo  (Ctrl+O)")

        self.recent_button = ttk.Menubutton(
            actions, text=f"{theme.GLYPH['recent']}  Recentes")
        self.recent_menu = tk.Menu(self.recent_button, tearoff=0)
        self.recent_button.configure(menu=self.recent_menu)
        self.recent_button.pack(side="left", padx=(8, 0))
        tip(self.recent_button, "Os ultimos trabalhos abertos neste computador")

        photo_setup = ttk.Button(
            actions, text=f"{theme.GLYPH['photo']}  Importar de fotos...",
            command=self._import_from_photos)
        photo_setup.pack(side="left", padx=(8, 0))
        tip(photo_setup, "Le a folha do protocolo e monta o lote inteiro  (Ctrl+I)")


    # -- toolbar -----------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = tk.Frame(self, background=theme.SURFACE, padx=10, pady=8,
                       highlightthickness=1, highlightbackground=theme.BORDER)
        bar.grid(row=3, column=0, sticky="ew", padx=12, pady=(8, 0))
        self.toolbar = bar

        self.add_button = ttk.Button(
            bar, text=f"{theme.GLYPH['add']}  Adicionar pastas...",
            style="Toolbar.TButton", command=self._add_entries)
        self.add_button.pack(side="left")
        tip(self.add_button, "Colar mais nomes de pasta na fila  (Ctrl+Shift+A)")

        self.photo_button = ttk.Button(
            bar, text=f"{theme.GLYPH['photo']}  Importar de fotos...",
            style="Toolbar.TButton", command=self._import_from_photos)
        self.photo_button.pack(side="left", padx=(8, 0))
        tip(self.photo_button, "Ler a folha do protocolo de entrega  (Ctrl+I)")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12, pady=2)

        self.up_button = ttk.Button(bar, text=theme.GLYPH["up"], width=3,
                                    style="Icon.TButton", command=lambda: self._move(-1))
        self.up_button.pack(side="left")
        tip(self.up_button, "Subir a pasta selecionada  (Alt+Cima)")
        self.down_button = ttk.Button(bar, text=theme.GLYPH["down"], width=3,
                                      style="Icon.TButton", command=lambda: self._move(1))
        self.down_button.pack(side="left", padx=(4, 0))
        tip(self.down_button, "Descer a pasta selecionada  (Alt+Baixo)")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12, pady=2)

        self.auto_check = ttk.Checkbutton(
            bar, text="Copia automatica", variable=self.auto_var,
            command=self._toggle_auto)
        self.auto_check.pack(side="left")
        tip(self.auto_check,
            "Ligado: a copia comeca sozinha depois da contagem regressiva.\n"
            "Desligado: cada disco espera voce clicar em Iniciar agora.")

    # -- middle ------------------------------------------------------------

    def _build_middle(self) -> None:
        middle = ttk.PanedWindow(self, orient="horizontal")
        middle.grid(row=4, column=0, sticky="nsew", padx=12, pady=(8, 0))
        self.middle = middle

        self.entries_view = EntriesView(
            middle, on_rename=self._rename_entry, on_command=self._entry_command
        )
        middle.add(self.entries_view, weight=4)

        self.disc_panel = DiscPanel(middle, on_command=self._disc_command)
        middle.add(self.disc_panel, weight=2)

        # The table asks for the full width of all nine columns, so the sash
        # starts pinned to the right edge and the disc panel opens clipped.
        # Weights only govern *resizing*, not the initial split, so it is
        # placed by hand once the window has a real width.
        self.after(120, self._place_sash)

    def _place_sash(self) -> None:
        try:
            total = self.middle.winfo_width()
            if total > 600:
                self.middle.sashpos(0, total - DISC_PANEL_WIDTH)
        except tk.TclError:  # pragma: no cover - window gone before it ran
            pass

    def _build_logs(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=5, column=0, sticky="nsew", padx=12, pady=(8, 0))
        self.log_view = LogView(self.notebook)
        self.notebook.add(self.log_view, text="Registro")
        self.problems = LogView(self.notebook)
        self.notebook.add(self.problems, text="Problemas")

    def _build_status_bar(self) -> None:
        bar = tk.Frame(self, background=theme.SURFACE, padx=12, pady=6,
                       highlightthickness=1, highlightbackground=theme.BORDER)
        bar.grid(row=6, column=0, sticky="ew", padx=12, pady=(8, 10))
        bar.columnconfigure(1, weight=1)

        self.drive_chip = Chip(bar, text="Sem trabalho", tone="neutral")
        self.drive_chip.grid(row=0, column=0, sticky="w")

        tk.Label(bar, textvariable=self.status_var, font=theme.FONT_SMALL,
                 background=theme.SURFACE, foreground=theme.INK_SOFT,
                 anchor="w").grid(row=0, column=1, sticky="w", padx=12)

        self.saved_label = tk.Label(bar, textvariable=self.saved_var,
                                    font=theme.FONT_SMALL, background=theme.SURFACE,
                                    foreground=theme.INK_MUTED, anchor="e")
        self.saved_label.grid(row=0, column=2, sticky="e")
        tip(self.saved_label, "Quando o trabalho foi gravado em disco pela ultima vez")

    # -- keys --------------------------------------------------------------

    def _bind_keys(self) -> None:
        self.bind("<space>", self._space_pressed)
        self.bind("<Escape>", lambda e: self._disc_command("cancel"))
        self.bind("<Control-n>", lambda e: self._new_job())
        self.bind("<Control-o>", lambda e: self._open_job())
        self.bind("<Control-w>", lambda e: self._close_job())
        self.bind("<Control-q>", lambda e: self._close())
        self.bind("<Control-e>", lambda e: self._open_parent_folder())
        self.bind("<Control-i>", lambda e: self._import_from_photos())
        self.bind("<Control-A>", lambda e: self._add_entries())
        self.bind("<Control-p>", lambda e: self._disc_command("pause"))
        self.bind("<Control-j>", lambda e: self._disc_command("eject"))
        self.bind("<Control-d>", lambda e: self._disc_command("send_to"))
        self.bind("<F1>", lambda e: self._show_help())
        self.bind("<F2>", lambda e: self._rename_selected())

    def _is_typing(self) -> bool:
        """True while a text field has focus.

        Without this, Space - bound on the window so it works wherever you
        are - starts a copy while you are typing the batch name.
        """
        widget = self.focus_get()
        return widget is not None and widget.winfo_class() in TYPING_CLASSES

    def _space_pressed(self, _event=None) -> str | None:
        if self._is_typing():
            return None
        self._disc_command("start_now")
        return "break"

    # -- state painting ----------------------------------------------------

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.auto_check.configure(state=state)
        self.clean_button.configure(state=state)
        self.open_folder_button.configure(state=state)
        self.close_job_button.configure(state=state)
        self.add_button.configure(state=state)
        self.up_button.configure(state=state)
        self.down_button.configure(state=state)
        # Deliberately not gated on a job: importing photos is one of the ways
        # to *create* one, since the sheet names the batch folder too.

        # Once a job exists these fields no longer do anything, so the whole
        # card goes away instead of sitting there greyed out. The widgets keep
        # their disabled state too - hidden is not the same as inert, and a
        # shortcut can still reach them.
        for widget in (self.destination_entry, self.parent_entry, self.browse_button,
                       self.new_button):
            widget.configure(state="disabled" if enabled else "normal")

        if enabled:
            self.setup.grid_remove()
            self.header.grid()
            self.toolbar.grid()
        else:
            self.header.grid_remove()
            self.setup.grid()
            self.toolbar.grid()

        if hasattr(self, "menubar"):
            self.menubar.set_job_open(enabled)

    def _paint_header(self) -> None:
        """Keep the banner showing the batch that is actually open."""
        active = self.store is not None
        self.title_label.configure(
            foreground=theme.BRAND_DEEP if active else theme.INK_MUTED)

        if not active:
            self.job_title_var.set("Nenhum trabalho aberto")
            self.job_path_var.set(
                "Escolha o Destino e crie um trabalho, ou importe de fotos."
            )
            self.percent_var.set("")
            self.job_progress["value"] = 0
            self._set_chips([])
            self.collect_bar.grid_remove()
            return

        job = self.store.job
        self.job_title_var.set(job.parent_name)
        self.job_path_var.set(str(job.parent_path))

        counts = job.count_by_status()
        done = counts[EntryStatus.DONE]
        total = len(job)
        chips: list[tuple[str, str]] = [
            (f"{done} de {total} concluida(s)", "success" if done else "neutral")
        ]
        if counts[EntryStatus.FAILED]:
            chips.append((f"{counts[EntryStatus.FAILED]} com falha", "danger"))
        if counts[EntryStatus.SKIPPED]:
            chips.append((f"{counts[EntryStatus.SKIPPED]} pulada(s)", "warning"))
        groups = job.group_names()
        if groups:
            chips.append((f"{len(groups)} EG", "brand"))
        self._set_chips(chips)

        percent = (done / total * 100) if total else 0
        self.job_progress["value"] = percent
        self.percent_var.set(f"{percent:.0f}%")

        collecting = job.collecting_entry()
        if collecting is None:
            self.collect_bar.grid_remove()
        else:
            count = collecting.disc_count
            received = f"{count} disco(s) recebidos" if count else "ainda sem discos"
            self.collect_var.set(
                f"Acumulando em  {self._entry_label(collecting)}  -  {received}.\n"
                "Todos os proximos discos vao para esta pasta ate voce desligar."
            )
            self.collect_bar.grid()

    def _set_chips(self, items: list[tuple[str, str]]) -> None:
        for child in self.chips.winfo_children():
            child.destroy()
        for text, tone in items:
            Chip(self.chips, text=text, tone=tone).pack(side="left", padx=(0, 8))

    @staticmethod
    def _entry_label(entry) -> str:
        """EG and folder together, so the destination is unambiguous."""
        if entry is None:
            return ""
        return f"{entry.group}  >  {entry.folder_name}" if entry.group else entry.folder_name

    @classmethod
    def _target_label(cls, entry) -> str:
        """The same, plus why this folder is the target when it is not its turn."""
        label = cls._entry_label(entry)
        if entry is not None and entry.collecting:
            return f"{label}   [acumulando]"
        return label

    def _stop_collecting(self) -> None:
        collecting = self.store.job.collecting_entry() if self.store else None
        if collecting is not None:
            self._set_collecting(collecting.entry_id, False)

    def _current_entry(self):
        if self.store is None or self.runner is None:
            return None
        entry_id = self.runner.current_entry_id
        return self.store.job.entry_by_id(entry_id) if entry_id else None

    def _open_parent_folder(self) -> None:
        if self.store is not None:
            self._open_in_explorer(self.store.job.parent_path)

    # -- job lifecycle ----------------------------------------------------

    def _browse(self) -> None:
        selected = filedialog.askdirectory(title="Escolha a pasta de destino")
        if selected:
            self.destination_var.set(selected)

    def _new_job(self) -> None:
        destination = self.destination_var.get().strip()
        parent = self.parent_var.get().strip()
        if not destination or not Path(destination).is_dir():
            messagebox.showerror("Destino invalido", "Escolha uma pasta de destino existente.")
            return
        try:
            validate_folder_name(parent)
        except ValueError as exc:
            messagebox.showerror("Nome invalido", str(exc))
            return

        store = JobStore.create(destination, parent, settings=JobSettings())
        store.job.parent_path.mkdir(parents=True, exist_ok=True)
        store.save()
        self._attach(store)
        self.log(f"Novo trabalho criado em {store.job.parent_path}")

        drafts = AddEntriesDialog(self).show()
        if drafts:
            self._add_drafts(drafts)

    def _open_job(self) -> None:
        selected = filedialog.askopenfilename(
            title="Abrir trabalho",
            filetypes=[("Trabalho backupov2", "*.json"), ("Todos", "*.*")],
        )
        if selected:
            self._load(Path(selected))

    def _load(self, path: Path) -> None:
        try:
            outcome = JobStore.load(path)
        except Exception as exc:
            messagebox.showerror("Nao foi possivel abrir", str(exc))
            return

        store = JobStore(outcome.job, source_path=outcome.source_path)
        self._attach(store)
        for note in outcome.notes:
            self.log(note, "warn")

        report = reconcile(store.job)
        if report.findings:
            for finding in report.findings:
                self.log(finding.message, "warn" if finding.needs_decision else "info")
            decisions = ReconcileDialog(self, report, store.job).show()
            for finding, resolution in decisions or []:
                apply_resolution(store.job, finding, resolution)
            store.save()
        self._refresh()

    def _attach(self, store: JobStore) -> None:
        self.store = store
        self.destination_var.set(store.job.destination_root)
        self.parent_var.set(store.job.parent_name)
        self.auto_var.set(store.job.settings.auto_copy)
        self.log_view.clear()
        self.problems.clear()

        self.runner = JobRunner(
            store=store,
            scanner=Win32Scanner(),
            ejector=Win32Ejector(),
            executor=ThreadExecutor(),
        )
        self.runner.start()
        self._set_drive_chip(RunnerState.IDLE, "Iniciando...")
        self._set_controls_enabled(True)
        self._refresh()
        self._refresh_recent()

    def _close_job(self) -> None:
        """Detach the open job so 'Criar trabalho' becomes available again.

        Finishing or abandoning a batch leaves the setup fields disabled with
        no button of their own to undo that - the only other way back was
        restarting the app. Nothing is deleted: the job is exactly as saved,
        on disk and in Recentes, ready to be reopened.
        """
        if self.store is None:
            return
        if self.runner is not None and self.runner.is_busy:
            if not messagebox.askyesno(
                "Copia em andamento",
                "Cancelar a copia atual e fechar este trabalho?\n\n"
                "O progresso ja gravado sera mantido e o trabalho podera ser "
                "reaberto em Recentes.",
            ):
                return
            self.runner.cancel()
        self.store.save()

        self.store = None
        self.runner = None
        self.current_media = None
        self.parent_var.set("")
        self.log_view.clear()
        self.problems.clear()
        self.entries_view.clear()
        self.disc_panel.show_state(RunnerState.IDLE, "Nenhum trabalho aberto")
        self.saved_var.set("")
        self.status_var.set("Pronto para o proximo lote.")
        self._set_drive_chip(RunnerState.IDLE, "Sem trabalho")
        self._set_controls_enabled(False)
        self._paint_header()
        self._refresh_recent()

    def _clean_control_files(self) -> None:
        """Delete this app's own bookkeeping files from the batch folder.

        For handing the folder over once a batch is finished. Only the fixed set
        of names written by this app is touched - never the backed-up data.
        """
        if self.store is None:
            return
        if self.runner is not None and self.runner.is_busy:
            messagebox.showinfo(
                "Copia em andamento",
                "Aguarde o fim da copia atual antes de limpar os arquivos de controle.",
            )
            return

        parent = self.store.job.parent_path
        found = control_files(parent)
        if not found:
            messagebox.showinfo(
                "Nada a limpar",
                f"Nenhum arquivo de controle encontrado em:\n{parent}",
            )
            return

        listing = "\n".join(
            f"   {path.name}   ({format_bytes(path.stat().st_size)})" for path in found
        )
        warning = ""
        if not self.store.job.is_complete:
            warning = (
                "\n\nATENCAO: este trabalho ainda tem pastas pendentes. O arquivo "
                "sera recriado assim que o proximo disco for copiado."
            )

        if not messagebox.askyesno(
            "Limpar arquivos de controle",
            f"Excluir {len(found)} arquivo(s) de:\n{parent}\n\n{listing}\n\n"
            "As pastas e os arquivos copiados nao serao tocados.\n"
            "A copia local do trabalho e mantida, entao ele continua em Recentes."
            f"{warning}\n\nContinuar?",
            icon="warning",
        ):
            return

        removed, failures = remove_control_files(parent)
        for path in removed:
            self.log(f"Arquivo de controle removido: {path.name}")
        for path, error in failures:
            self.log(f"Nao foi possivel remover {path.name}: {error}", "error")

        if failures:
            messagebox.showerror(
                "Limpeza incompleta",
                f"{len(removed)} removido(s), {len(failures)} com erro. "
                "Veja a aba Problemas.",
            )
        else:
            messagebox.showinfo(
                "Limpeza concluida", f"{len(removed)} arquivo(s) removido(s)."
            )

    # -- entries ----------------------------------------------------------

    def _add_entries(self) -> None:
        if self.store is None:
            return
        drafts = AddEntriesDialog(self).show()
        if drafts:
            self._add_drafts(drafts)

    def _import_from_photos(self) -> None:
        """Read the delivery protocol with the vision model, then review it.

        Works with no job open: the sheet names the batch folder as well as the
        discs, so there is nothing for the user to type first beyond choosing
        where on disk the backup goes. The drafts come back through the same
        door as a manual paste, so the model, store and runner need no special
        case for them.
        """
        destination = self.destination_var.get().strip()
        if not destination or not Path(destination).is_dir():
            messagebox.showerror(
                "Escolha o destino",
                "Primeiro escolha a pasta de Destino onde o backup sera gravado.\n\n"
                "O nome da pasta do lote e a lista de discos vem das fotos.",
            )
            return

        creating = self.store is None
        outcome = PhotoImportDialog(self, ask_parent=creating).show()
        if not outcome:
            return

        if creating:
            try:
                store = JobStore.create(
                    destination, outcome.parent_name, settings=JobSettings()
                )
                store.job.parent_path.mkdir(parents=True, exist_ok=True)
                store.save()
            except (OSError, ValueError) as exc:
                messagebox.showerror("Nao foi possivel criar o trabalho", str(exc))
                return
            self._attach(store)
            self.parent_var.set(outcome.parent_name)
            self.log(f"Trabalho criado a partir das fotos em {store.job.parent_path}")

        flagged = sum(1 for draft in outcome.drafts if draft.needs_review)
        self._add_drafts(outcome.drafts)
        self.log(
            f"{len(outcome.drafts)} nome(s) importado(s) de fotos"
            + (f", {flagged} marcado(s) para conferencia." if flagged else ".")
        )

    def _add_drafts(self, drafts, at_index: int | None = None) -> None:
        assert self.store is not None
        clashes = [
            d.folder_name
            for d in drafts
            if self.store.job.has_folder_named(d.folder_name, group=d.group)
        ]
        if clashes:
            messagebox.showerror(
                "Nomes repetidos",
                "Ja existem pastas com estes nomes:\n" + "\n".join(clashes[:10]),
            )
            return
        self.store.add_entries(drafts, at_index=at_index)
        self._create_folders()
        self.log(f"{len(drafts)} pasta(s) adicionada(s).")
        self._refresh()

    def _create_folders(self) -> None:
        assert self.store is not None
        job = self.store.job
        try:
            ensure_entry_folders(
                Path(job.destination_root),
                job.parent_name,
                [(entry.group, entry.folder_name) for entry in job.entries],
            )
        except (OSError, ValueError) as exc:
            self.log(f"Nao foi possivel criar as pastas: {exc}", "error")

    def _rename_entry(self, entry_id: str, new_name: str) -> None:
        assert self.store is not None
        job = self.store.job
        entry = job.entry_by_id(entry_id)
        if entry is None:
            return
        if job.has_folder_named(new_name, group=entry.group, excluding=entry_id):
            messagebox.showerror("Nome repetido", f"Ja existe uma pasta '{new_name}'.")
            return

        old_folder = job.folder_for(entry)
        new_folder = old_folder.parent / new_name
        try:
            if old_folder.is_dir():
                if any(old_folder.iterdir()):
                    messagebox.showerror(
                        "Pasta nao vazia",
                        f"'{entry.folder_name}' ja contem arquivos e nao pode ser "
                        "renomeada automaticamente.",
                    )
                    return
                old_folder.rename(new_folder)
            else:
                new_folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Nao foi possivel renomear", str(exc))
            return

        entry.folder_name = new_name
        self.store.save()
        self.log(f"Pasta renomeada para '{new_name}'.")
        self._refresh()

    def _rename_selected(self) -> str | None:
        """F2 and the Lote menu: start the same inline editor as a double click."""
        if self.store is None or self._is_typing():
            return None
        entry_id = self.entries_view.selected
        if entry_id:
            self.entries_view.begin_rename(entry_id)
        return "break"

    def _selected_command(self, command: str) -> None:
        """Run a list command on whatever row is selected, for the menu bar."""
        entry_id = self.entries_view.selected
        if entry_id:
            self._entry_command(command, entry_id)

    def _move(self, offset: int) -> None:
        entry_id = self.entries_view.selected
        if self.store is None or not entry_id:
            return
        self.store.move_entry(entry_id, offset)
        self._refresh()
        self.entries_view.select(entry_id)

    def _entry_command(self, command: str, entry_id: str) -> None:
        if self.store is None:
            return
        job = self.store.job
        entry = job.entry_by_id(entry_id)
        if entry is None:
            return

        if command == "send_here":
            if self.runner and self.current_media is not None:
                self.runner.send_to(entry_id)
            else:
                messagebox.showinfo("Nenhum disco", "Nenhum disco carregado no momento.")
        elif command in ("insert_above", "insert_below"):
            drafts = AddEntriesDialog(self, "Inserir pastas").show()
            if drafts:
                index = job.index_of(entry_id) + (0 if command == "insert_above" else 1)
                self._add_drafts(drafts, at_index=index)
        elif command == "rename":
            self.entries_view.select(entry_id)
            self.entries_view.begin_rename(entry_id)
        elif command in ("collect_on", "collect_off"):
            self._set_collecting(entry_id, command == "collect_on")
        elif command == "mark_pending":
            entry.status = EntryStatus.PENDING
            entry.error = None
            self.store.save()
            self._refresh()
        elif command == "mark_skipped":
            entry.status = EntryStatus.SKIPPED
            self.store.save()
            self._refresh()
        elif command == "mark_defective":
            self._mark_entry_defective(entry_id)
        elif command == "retry":
            if self.runner:
                self.runner.retry_entry(entry_id)
            self._refresh()
        elif command == "open_folder":
            self._open_in_explorer(job.folder_for(entry))
        elif command == "delete":
            if entry.status is EntryStatus.IN_PROGRESS:
                messagebox.showerror("Em uso", "Esta pasta esta sendo copiada agora.")
                return
            if messagebox.askyesno(
                "Excluir da lista",
                f"Remover '{entry.folder_name}' da lista?\n\n"
                "A pasta no disco nao sera apagada.",
            ):
                self.store.remove_entry(entry_id)
                self._refresh()
        elif command in ("move_up", "move_down"):
            self._move(-1 if command == "move_up" else 1)

    def _mark_entry_defective(self, entry_id: str) -> None:
        """Write a disc off from the list, without ever reading it.

        For the disc you can see is broken, or that the drive will not mount.
        The during-a-copy version lives on the disc panel instead, because
        there it has a transfer to stop first.
        """
        if self.store is None or self.runner is None:
            return
        entry = self.store.job.entry_by_id(entry_id)
        if entry is None:
            return
        if entry.status is EntryStatus.IN_PROGRESS:
            messagebox.showinfo(
                "Copia em andamento",
                "Esta pasta esta sendo copiada agora.\n\n"
                "Use 'Disco defeituoso' no painel do disco para parar a copia "
                "e marcar o disco.",
            )
            return

        answer = MarkDefectiveDialog(self, self._entry_label(entry)).show()
        if answer is None:
            return
        if self.runner.mark_entry_defective(entry_id, answer.get("note", "")):
            self._refresh()

    def _set_collecting(self, entry_id: str, on: bool) -> None:
        """Turn the "keep sending discs here" flag on or off.

        Routed through the runner rather than the store so a disc already in
        the drive is re-aimed at the new target instead of waiting for the
        next one.
        """
        if self.store is None:
            return
        entry = self.store.job.entry_by_id(entry_id)
        if entry is None:
            return

        if on:
            previous = self.store.job.collecting_entry()
            if previous is not None and previous.entry_id != entry_id:
                if not messagebox.askyesno(
                    "Trocar a pasta acumuladora",
                    f"'{previous.folder_name}' esta acumulando discos agora.\n\n"
                    f"Passar a acumular em '{entry.folder_name}'?",
                ):
                    return

        if self.runner is not None:
            self.runner.set_collecting(entry_id, on)
        else:
            self.store.job.set_collecting(entry_id, on)
            self.store.save()
        self._refresh()

    @staticmethod
    def _open_in_explorer(folder: Path) -> None:
        try:
            if folder.is_dir():
                os.startfile(folder)  # noqa: S606 - intended shell integration
        except OSError:
            pass

    # -- disc panel commands ----------------------------------------------

    def _disc_command(self, command: str) -> None:
        if self.runner is None:
            return
        if command == "start_now":
            self.runner.start_now()
        elif command == "skip_disc":
            self.runner.skip_disc()
        elif command == "cancel":
            self.runner.cancel()
        elif command == "eject":
            self.runner.eject_now()
        elif command == "mark_corrupted":
            self._mark_corrupted()
        elif command == "pause":
            # is_paused, not the state: a held copy stays in WORKING, so the
            # state alone would send "Retomar" back into pause().
            if self.runner.is_paused:
                self.runner.resume()
            else:
                self.runner.pause()
        elif command == "send_to":
            if self.store is None or self.runner is None:
                return
            # The picker is a modal dialog, but Tk's nested event loop still
            # runs our after() pump underneath it - without pausing here, a
            # countdown in progress can reach zero and start copying into the
            # wrong folder while this dialog is still open.
            self.runner.pause()
            dialog = SendToDialog(self, self.store.job)
            entry_id = dialog.show()
            if entry_id:
                # Flag it before the copy starts, or this first disc would be
                # recorded as a one-off and only the ones after it pooled.
                if dialog.keep_collecting:
                    self._set_collecting(entry_id, True)
                self.runner.send_to(entry_id)
            else:
                self.runner.resume()

    def _mark_corrupted(self) -> None:
        """Confirm before writing a disc off - this one leaves a record.

        Skip and cancel are interruptions you can undo by putting the disc
        back. This marks the folder failed and writes a report into it, so it
        is worth one question first.
        """
        if self.runner is None or self.store is None:
            return
        entry = self._current_entry()
        name = self._entry_label(entry) or "esta pasta"
        if not messagebox.askyesno(
            "Marcar disco como defeituoso",
            f"Parar a copia deste disco e marcar '{name}' como falha?\n\n"
            "Os arquivos ja copiados serao mantidos e um arquivo "
            f"'{DEFECT_REPORT_NAME}' sera criado na pasta, listando o que nao "
            "pode ser lido.\n\n"
            "O disco sera ejetado e o proximo disco ira para a proxima pasta "
            "pendente.",
            icon="warning",
        ):
            return
        self.runner.mark_corrupted()

    def _toggle_auto(self) -> None:
        if self.store is None:
            return
        self.store.job.settings.auto_copy = self.auto_var.get()
        self.store.save()
        self.log(
            "Copia automatica ligada." if self.auto_var.get()
            else "Copia automatica desligada - cada disco espera 'Iniciar agora'."
        )

    # -- view menu ---------------------------------------------------------

    def _toggle_log_panel(self) -> None:
        """Hide the log tabs to give the folder list the whole window."""
        if self.show_log_var.get():
            self.notebook.grid()
            self.rowconfigure(5, weight=2)
        else:
            self.notebook.grid_remove()
            self.rowconfigure(5, weight=0)

    def _clear_logs(self) -> None:
        self.log_view.clear()
        self.problems.clear()

    def _copy_log(self) -> None:
        """Put the visible tab's contents on the clipboard, to paste into a
        message when asking someone what went wrong."""
        try:
            current = self.notebook.index(self.notebook.select())
        except tk.TclError:
            current = 0
        view = self.log_view if current == 0 else self.problems
        contents = view.contents()
        self.clipboard_clear()
        self.clipboard_append(contents)
        self.status_var.set(f"{len(contents.splitlines())} linha(s) copiada(s).")

    # -- help --------------------------------------------------------------

    def _show_help(self, topic: str | None = None) -> None:
        """One help window, raised rather than duplicated on a second click."""
        if self._help_window is not None and self._help_window.winfo_exists():
            self._help_window.deiconify()
            self._help_window.lift()
            if topic:
                self._help_window.render(topic)
                self._help_window.topics.selection_set(topic)
            return
        self._help_window = HelpWindow(self, topic)

    def _show_about(self) -> None:
        AboutDialog(self)

    def _open_readme(self) -> None:
        readme = Path(__file__).resolve().parent.parent.parent / "README.md"
        if readme.is_file():
            try:
                os.startfile(readme)  # noqa: S606 - intended shell integration
                return
            except OSError:
                pass
        messagebox.showinfo(
            "README nao encontrado",
            "Nao foi possivel abrir o README do projeto.\n\n"
            "Use Ajuda > Ajuda e guias para os guias dentro do programa.",
        )

    # -- pumps ------------------------------------------------------------

    def _pump(self) -> None:
        """The only place Tk widgets are touched in response to runner work."""
        if self.runner is not None:
            self.runner.tick()
            for event in self.runner.drain():
                self._handle(event)
        self.after(TICK_MS, self._pump)

    def _poll(self) -> None:
        if self.runner is not None:
            try:
                self.runner.poll()
            except Exception as exc:  # a polling glitch must not kill the app
                self.log(f"Falha ao verificar a unidade: {exc}", "error")
        self.after(POLL_MS, self._poll)

    def _handle(self, event) -> None:
        if isinstance(event, StateChanged):
            self.disc_panel.show_state(
                event.state,
                event.detail,
                paused=self.runner.is_paused if self.runner else False,
            )
            self._set_drive_chip(event.state)
            if event.state is RunnerState.JOB_COMPLETE:
                self.log("Backup concluido.")
        elif isinstance(event, DiscDetected):
            self.current_media = event.media
            self.disc_panel.show_disc(event.media, event.kind, event.reason)
        elif isinstance(event, DiscSized):
            self.disc_panel.show_size(event.file_count, event.total_bytes)
        elif isinstance(event, CountdownTick):
            entry = self._current_entry()
            self.disc_panel.show_countdown(
                event.remaining,
                event.total,
                self._target_label(entry) or event.target_name,
            )
        elif isinstance(event, Progress):
            self.disc_panel.show_progress(event)
        elif isinstance(event, EntryUpdated):
            self._refresh()
        elif isinstance(event, DiscFinished):
            self.disc_panel.show_message(
                f"{event.files_copied} arquivo(s), {format_bytes(event.bytes_copied)}"
            )
            self._refresh()
        elif isinstance(event, DiscTrouble):
            self.disc_panel.show_trouble(event.message)
        elif isinstance(event, DiscFailed):
            self.problems.append(event.message, "error")
            self._refresh()
        elif isinstance(event, DuplicateWarning):
            self._ask_duplicate(event)
        elif isinstance(event, NoTarget):
            self._ask_no_target(event)
        elif isinstance(event, Ejected):
            if not event.ok:
                self.log("A bandeja pode nao ter aberto; remova o disco manualmente.", "warn")
        elif isinstance(event, LogLine):
            self.log(event.message, event.level)
        elif isinstance(event, JobComplete):
            messagebox.showinfo("Backup concluido", "Todos os discos foram processados.")

    def _set_drive_chip(self, state: RunnerState, text: str | None = None) -> None:
        self.drive_chip.configure(text=text or state_label(state))
        self.drive_chip.set_tone(STATE_TONES.get(state, "neutral"))

    def _ask_duplicate(self, event) -> None:
        when = (event.finished_utc or "").replace("T", " ").rstrip("Z")
        again = messagebox.askyesno(
            "Disco ja copiado",
            f"Este disco ja foi copiado para '{event.entry_name}'"
            + (f" em {when}" if when else "")
            + ".\n\nCopiar novamente para a proxima pasta pendente?",
        )
        if again and self.runner:
            self.runner.copy_anyway()
        elif self.runner:
            self.runner.skip_disc()

    def _ask_no_target(self, event) -> None:
        if messagebox.askyesno(
            "Nenhuma pasta pendente",
            "Nao ha pasta pendente para este disco.\n\nAdicionar uma pasta agora?",
        ):
            drafts = AddEntriesDialog(self, "Nova pasta para este disco").show()
            if drafts:
                self._add_drafts(drafts)
                if self.runner:
                    self.runner.copy_anyway()
        elif self.runner:
            self.runner.skip_disc()

    # -- refresh ----------------------------------------------------------

    def _refresh(self) -> None:
        if self.store is None:
            return
        job = self.store.job
        working = self.runner.current_entry_id if self.runner and self.runner.is_busy else None
        self.entries_view.refresh(job, working_entry_id=working)

        pending_count = sum(1 for entry in job.entries if entry.is_open)
        self.status_var.set(
            f"{len(job)} pasta(s) no total   -   {pending_count} ainda por copiar"
        )
        self.saved_var.set(f"salvo as {job.updated_utc[11:19]}")
        self._paint_header()

        pending = job.next_pending()
        if pending is not None and self.runner and not self.runner.is_busy:
            self.disc_panel.show_target(self._target_label(pending))

    def _refresh_recent(self) -> None:
        self.recent_menu.delete(0, "end")
        items = load_recent()
        if not items:
            self.recent_menu.add_command(label="(nenhum)", state="disabled")
        else:
            for item in items[:10]:
                label = f"{item.get('parent_name')} - {item.get('updated_utc', '')[:10]}"
                path = Path(item.get("local_path", ""))
                self.recent_menu.add_command(
                    label=label, command=lambda p=path: self._load(p)
                )
        if hasattr(self, "menubar"):
            self.menubar.refresh_recent()

    def log(self, message: str, level: str = "info") -> None:
        self.log_view.append(message, level)
        if level in ("warn", "error"):
            self.problems.append(message, level)

    # -- shutdown ---------------------------------------------------------

    def _close(self) -> None:
        if self.runner is not None and self.runner.is_busy:
            if not messagebox.askyesno(
                "Copia em andamento",
                "Cancelar a copia atual e sair?\n\n"
                "O progresso ja gravado sera mantido e o trabalho podera ser retomado.",
            ):
                return
            self.runner.cancel()
        if self.store is not None:
            self.store.save()
        self.destroy()


def main() -> None:
    BackupApp().mainloop()


if __name__ == "__main__":
    main()
