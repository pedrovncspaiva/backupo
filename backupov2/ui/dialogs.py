"""Modal dialogs: adding folders, redirecting a disc, and resolving findings."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Sequence

from ..core import DEFECT_REPORT_NAME, parse_subfolders
from ..jobmodel import EntryDraft, Job
from ..reconcile import Finding, ReconcileReport, Resolution
from ..strings import status_label
from . import theme
from .widgets import autohide, format_stamp

RESOLUTION_LABELS = {
    Resolution.RESUME_INTO: "Retomar (manter o que ja existe)",
    Resolution.OVERWRITE: "Sobrescrever",
    Resolution.MARK_DONE: "Marcar como concluida",
    Resolution.MARK_PENDING: "Marcar como pendente",
    Resolution.RENAME: "Renomear...",
    Resolution.IGNORE: "Ignorar",
}


class _Dialog(tk.Toplevel):
    """Shared modal plumbing: centred, transient, Esc closes, waits for close."""

    def __init__(self, parent, title: str, width: int = 520, height: int = 420) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(True, True)
        self.result = None
        self.configure(background=theme.CANVAS)
        theme.apply_window_icon(self)

        self.geometry(f"{width}x{height}")
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

        self.bind("<Escape>", lambda e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def show(self):
        self.grab_set()
        self.wait_window()
        return self.result


class AddEntriesDialog(_Dialog):
    """Paste folder names, one per line - the manual draft producer."""

    def __init__(self, parent, title: str = "Adicionar pastas") -> None:
        super().__init__(parent, title, 520, 440)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(
            frame,
            text="Um nome de pasta por linha, na ordem em que os discos serao inseridos:",
            wraplength=470,
        ).grid(row=0, column=0, sticky="w")

        self.text = tk.Text(frame, wrap="none", height=14, font=("Consolas", 10))
        self.text.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self.text.focus_set()

        self.error_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.error_var, foreground=theme.DANGER, wraplength=470).grid(
            row=2, column=0, sticky="w", pady=(8, 0)
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Adicionar", command=self._accept, style="Accent.TButton").pack(
            side="right", padx=(0, 8)
        )

    def _accept(self) -> None:
        try:
            names = parse_subfolders(self.text.get("1.0", "end"))
        except ValueError as exc:
            self.error_var.set(str(exc))
            return
        self.result = [EntryDraft(name) for name in names]
        self.destroy()


class MarkDefectiveDialog(_Dialog):
    """Confirm writing a disc off, and collect why in the user's own words.

    The note matters more than it looks: a delivery protocol has to be
    answered with *which* disc was unreadable and how, and nobody remembers
    that two weeks later. It goes into the folder's report and into the job.
    """

    def __init__(self, parent, folder_label: str) -> None:
        super().__init__(parent, "Disco defeituoso", 520, 340)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        ttk.Label(
            frame,
            text=f"Marcar '{folder_label}' como pulada por disco defeituoso?",
            font=("Segoe UI", 10, "bold"),
            wraplength=470,
        ).grid(row=0, column=0, sticky="w")

        ttk.Label(
            frame,
            text=(
                "A pasta fica marcada como pulada e recebe um arquivo "
                f"'{DEFECT_REPORT_NAME}' explicando que o conteudo nao pode "
                "ser copiado. Nada que ja esteja na pasta e apagado."
            ),
            foreground=theme.INK_SOFT,
            wraplength=470,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))

        ttk.Label(frame, text="Observacao (opcional):").grid(
            row=2, column=0, sticky="w", pady=(12, 0)
        )
        self.text = tk.Text(frame, wrap="word", height=5, font=("Segoe UI", 10))
        self.text.grid(row=3, column=0, sticky="nsew", pady=(4, 0))
        self.text.focus_set()

        ttk.Label(
            frame,
            text="Ex.: disco trincado; nao e reconhecido pelo leitor; superficie riscada.",
            foreground=theme.INK_MUTED,
        ).grid(row=4, column=0, sticky="w", pady=(4, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Marcar como defeituoso", command=self._accept, style="Accent.TButton").pack(
            side="right", padx=(0, 8)
        )

    def _accept(self) -> None:
        # A dict, not the bare note: an empty note is still a confirmation, and
        # "" would be indistinguishable from having cancelled.
        self.result = {"note": self.text.get("1.0", "end").strip()}
        self.destroy()


RECENT_LIMIT = 5


class SendToDialog(_Dialog):
    """Pick any folder for the loaded disc, not just the next pending one.

    Folders finished most recently are called out in their own section at the
    top - the case this exists for is combining a follow-up disc into a folder
    you were just working with a moment ago, which otherwise means hunting for
    it in a list that can run to dozens of entries.
    """

    def __init__(self, parent, job: Job) -> None:
        super().__init__(parent, "Enviar disco para...", 560, 560)
        self.job = job
        self.keep_collecting = False

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(frame, text="Copiar o disco carregado para qual pasta?").grid(
            row=0, column=0, sticky="w"
        )

        columns = ("index", "folder", "status", "when")
        self.tree = ttk.Treeview(
            frame, columns=columns, show="tree headings", selectmode="browse"
        )
        self.tree.heading("index", text="#")
        self.tree.heading("folder", text="Pasta")
        self.tree.heading("status", text="Situacao")
        self.tree.heading("when", text="Concluido")
        self.tree.column("#0", width=0, stretch=False)  # unused tree-icon column
        self.tree.column("index", width=40, minwidth=34, anchor="center", stretch=False)
        # The four of them add up to less than the dialog's inner width, so
        # the horizontal bar stays out of the way until the window is dragged
        # narrower than the folder names need.
        self.tree.column("folder", width=266, minwidth=140, stretch=False)
        self.tree.column("status", width=90, minwidth=70, stretch=False)
        self.tree.column("when", width=100, minwidth=80, stretch=False)
        self.tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(
            yscrollcommand=autohide(vertical, row=1, column=1, sticky="ns", pady=(10, 0)),
            xscrollcommand=autohide(horizontal, row=2, column=0, sticky="ew"),
        )

        self.tree.tag_configure(
            "heading", font=theme.FONT_SMALL_BOLD, foreground=theme.INK_SOFT
        )
        self.tree.tag_configure(
            "recent", font=theme.FONT_BODY_BOLD, background=theme.BRAND_TINT
        )

        self._populate()
        self.tree.bind("<Double-1>", lambda e: self._accept())
        self.tree.bind("<<TreeviewSelect>>", self._check_selection)

        # Choosing a folder by hand is usually the moment you know a set runs
        # to several discs, so the "keep using this one" switch belongs here
        # and not only buried in the list's context menu.
        self.collect_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text="Continuar enviando os proximos discos para esta pasta",
            variable=self.collect_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.warn_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.warn_var, foreground=theme.WARNING, wraplength=520).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Enviar", command=self._accept, style="Accent.TButton").pack(
            side="right", padx=(0, 8)
        )

    def _populate(self) -> None:
        entries_by_position = {entry.entry_id: i for i, entry in enumerate(self.job.entries, start=1)}

        recent = sorted(
            (entry for entry in self.job.entries if entry.finished_utc),
            key=lambda entry: entry.finished_utc,
            reverse=True,
        )[:RECENT_LIMIT]

        first_selectable = None
        if recent:
            self.tree.insert(
                "", "end", iid="heading:recentes", values=("", "Usadas recentemente", "", ""),
                tags=("heading",), open=True,
            )
            for entry in recent:
                iid = f"recent:{entry.entry_id}"
                self._insert_row(iid, entries_by_position[entry.entry_id], entry, tag="recent")
                first_selectable = first_selectable or iid

            self.tree.insert(
                "", "end", iid="heading:todas", values=("", "Todas as pastas", "", ""),
                tags=("heading",), open=True,
            )

        for index, entry in enumerate(self.job.entries, start=1):
            iid = f"all:{entry.entry_id}"
            self._insert_row(iid, index, entry)
            first_selectable = first_selectable or iid

        if first_selectable:
            self.tree.selection_set(first_selectable)
            self.tree.see(first_selectable)

    def _insert_row(self, iid: str, index: int, entry, tag: str | None = None) -> None:
        when = format_stamp(entry.finished_utc, "%H:%M")
        self.tree.insert(
            "",
            "end",
            iid=iid,
            values=(index, entry.folder_name, status_label(entry.status), when),
            tags=(tag,) if tag else (),
        )

    def _selected_entry(self):
        selection = self.tree.selection()
        if not selection or selection[0].startswith("heading:"):
            return None
        _, entry_id = selection[0].split(":", 1)
        return self.job.entry_by_id(entry_id)

    def _check_selection(self, _event=None) -> None:
        entry = self._selected_entry()
        if entry is not None and entry.status.value == "done":
            self.warn_var.set(
                "Esta pasta ja foi concluida. Nada sera apagado: os arquivos do "
                "disco serao somados a ela, e qualquer nome que colidir com "
                "conteudo diferente sera salvo com outro nome (ex.: \"arquivo (2)\")."
            )
        else:
            self.warn_var.set("")

    def _accept(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        self.result = entry.entry_id
        # Kept beside the result rather than folded into it, so the caller's
        # "did they pick a folder?" check stays a plain truth test.
        self.keep_collecting = bool(self.collect_var.get())
        self.destroy()


class ReconcileDialog(_Dialog):
    """Show what reconciliation found and collect a decision for each question."""

    def __init__(self, parent, report: ReconcileReport, job: Job) -> None:
        super().__init__(parent, "Retomar trabalho", 640, 480)
        self.report = report
        self.job = job
        self.choices: dict[int, tk.StringVar] = {}

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        applied = [f for f in report.findings if f.applied]
        questions = report.questions

        summary = (
            f"{len(questions)} item(ns) precisam de uma decisao."
            if questions
            else "Nada precisa de decisao."
        )
        if applied:
            summary += f"  {len(applied)} ajuste(s) automatico(s) ja aplicado(s)."
        ttk.Label(outer, text=summary, font=theme.FONT_BODY_BOLD).grid(
            row=0, column=0, sticky="w"
        )

        # An unstyled tk.Canvas comes up in Tk's own grey, which against the
        # dialog's background read as a grey slab with the findings sitting on
        # a lighter rectangle that stopped halfway across it.
        canvas = tk.Canvas(outer, highlightthickness=0, background=theme.CANVAS)
        canvas.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        scroll.grid(row=1, column=1, sticky="ns", pady=(10, 0))
        canvas.configure(
            yscrollcommand=autohide(scroll, row=1, column=1, sticky="ns", pady=(10, 0))
        )

        body = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        # Without this the body keeps its requested width, so a long finding
        # wraps at 560px inside a frame that is only as wide as the shortest
        # one - which is where that half-width edge came from.
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))

        row = 0
        for finding in report.findings:
            block = ttk.Frame(body, padding=(0, 6))
            block.grid(row=row, column=0, sticky="ew")
            ttk.Label(block, text=finding.message, wraplength=560).grid(
                row=0, column=0, sticky="w"
            )
            if finding.needs_decision:
                index = report.findings.index(finding)
                choice = tk.StringVar(value=finding.options[0].value)
                self.choices[index] = choice
                options = ttk.Frame(block)
                options.grid(row=1, column=0, sticky="w", pady=(4, 0))
                for option in finding.options:
                    ttk.Radiobutton(
                        options,
                        text=RESOLUTION_LABELS.get(option, option.value),
                        value=option.value,
                        variable=choice,
                    ).pack(side="left", padx=(0, 12))
            row += 1

        buttons = ttk.Frame(outer)
        buttons.grid(row=2, column=0, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Aplicar", command=self._accept, style="Accent.TButton").pack(side="right")

    def _accept(self) -> None:
        decisions = []
        for index, choice in self.choices.items():
            decisions.append((self.report.findings[index], Resolution(choice.get())))
        self.result = decisions
        self.destroy()
