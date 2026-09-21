"""Import folder names from photographed delivery protocols.

Two steps, deliberately: the extraction runs on a worker thread, and then the
result is shown as an editable list that the user confirms. Nothing the model
produced reaches the job until someone has looked at it - it is a transcription
of a photograph, and the whole batch's folder structure depends on it.

Rows that need attention are coloured and sorted to the user's eye by the
Situacao column: a changed name, a repeat, or a line the model was unsure of.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..core import validate_folder_name
from ..vision import (
    ExtractedItem,
    ExtractionResult,
    VisionConfig,
    VisionError,
    extract_names,
    load_api_key,
    load_model,
    save_api_key,
    to_drafts,
)
from . import theme
from .widgets import CellEditor


@dataclass
class PhotoImportOutcome:
    """Everything the sheet describes: the batch folder and its discs."""

    parent_name: str
    drafts: list

    def __bool__(self) -> bool:
        return bool(self.drafts)

IMAGE_TYPES = [
    ("Imagens e PDF", "*.jpg *.jpeg *.png *.webp *.heic *.heif *.pdf"),
    ("Todos", "*.*"),
]


class PhotoImportDialog(tk.Toplevel):
    """Pick pages, read them, review the result, then hand back drafts."""

    def __init__(
        self,
        parent,
        local_root: Path | None = None,
        ask_parent: bool = True,
    ) -> None:
        super().__init__(parent)
        self.title("Importar nomes de fotos")
        self.transient(parent)
        self.configure(background=theme.CANVAS)
        theme.apply_window_icon(self)
        self.result = None
        self.local_root = local_root
        self.ask_parent = ask_parent
        self.paths: list[Path] = []
        self.extraction: ExtractionResult | None = None
        self._editor: CellEditor | None = None
        self._busy = False
        # The worker hands its outcome back through here. Calling after() or
        # touching a widget from the worker thread is not thread-safe in Tk.
        self._outcome: "queue.Queue[tuple[str, object]]" = queue.Queue()

        self.geometry("900x640")
        self.minsize(720, 520)
        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self.status_var = tk.StringVar(value="Selecione as fotos do protocolo.")
        self.group_var = tk.BooleanVar(value=True)
        self.parent_var = tk.StringVar()
        self._build()

    # -- layout -----------------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        top = ttk.Frame(outer)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Button(top, text="Escolher fotos...", command=self._choose).pack(side="left")
        self.read_button = ttk.Button(
            top, text="Ler com IA", command=self._start, state="disabled"
        )
        self.read_button.pack(side="left", padx=(8, 0))
        ttk.Button(top, text="Chave de API...", command=self._set_key).pack(side="left", padx=(8, 0))
        self.group_check = ttk.Checkbutton(
            top,
            text="Criar subpasta por EG",
            variable=self.group_var,
            command=self._refresh,
            state="disabled",
        )
        self.group_check.pack(side="right")

        ttk.Label(outer, textvariable=self.status_var, foreground=theme.INK_SOFT).grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )

        self.parent_row = ttk.Frame(outer)
        self.parent_row.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        self.parent_row.columnconfigure(1, weight=1)
        ttk.Label(self.parent_row, text="Pasta principal").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.parent_entry = ttk.Entry(self.parent_row, textvariable=self.parent_var)
        self.parent_entry.grid(row=0, column=1, sticky="ew")
        ttk.Label(
            self.parent_row,
            text="lida da folha - edite se quiser",
            foreground=theme.INK_SOFT,
        ).grid(row=1, column=1, sticky="w", pady=(2, 0))
        if not self.ask_parent:
            self.parent_row.grid_remove()

        table = ttk.Frame(outer)
        table.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)

        columns = ("index", "group", "folder", "raw", "issues")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        for name, title, width, minwidth, stretch in (
            # stretch is False everywhere: a stretchable column springs back
            # to fit the pane when you drag it wider.
            ("index", "#", 40, 34, False),
            ("group", "Subpasta (EG)", 160, 90, False),
            ("folder", "Nome da pasta", 250, 140, False),
            ("raw", "Como esta no papel", 220, 120, False),
            ("issues", "Situacao", 180, 90, False),
        ):
            self.tree.heading(name, text=title)
            self.tree.column(
                name,
                width=width,
                minwidth=minwidth,
                stretch=stretch,
                anchor="center" if name == "index" else "w",
            )
        self.tree.grid(row=0, column=0, sticky="nsew")

        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(
            yscrollcommand=vertical.set, xscrollcommand=horizontal.set
        )

        self.tree.tag_configure("review", background=theme.WARNING_TINT)
        self.tree.tag_configure("uncertain", foreground=theme.DANGER)
        self.tree.bind("<Double-1>", self._begin_edit)

        ttk.Label(
            outer,
            text="Clique duas vezes em um nome para corrigi-lo. "
                 "As linhas destacadas foram alteradas ou estao repetidas.",
            foreground=theme.INK_SOFT,
        ).grid(row=3, column=0, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(outer)
        buttons.grid(row=4, column=0, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=self._cancel).pack(side="right")
        self.add_button = ttk.Button(
            buttons, text="Adicionar ao trabalho", command=self._accept, state="disabled",
            style="Accent.TButton",
        )
        self.add_button.pack(side="right", padx=(0, 8))

    # -- steps ------------------------------------------------------------

    def _choose(self) -> None:
        selected = filedialog.askopenfilenames(
            title="Fotos do protocolo (na ordem das paginas)",
            filetypes=IMAGE_TYPES,
            parent=self,
        )
        if not selected:
            return
        self.paths = [Path(path) for path in selected]
        self.read_button.configure(state="normal")
        self.status_var.set(
            f"{len(self.paths)} pagina(s) selecionada(s): "
            + ", ".join(path.name for path in self.paths[:4])
            + (" ..." if len(self.paths) > 4 else "")
        )

    def _set_key(self) -> None:
        from tkinter import simpledialog

        current = load_api_key(self.local_root)
        masked = f" (atual: ...{current[-4:]})" if current else ""
        key = simpledialog.askstring(
            "Chave de API do Gemini",
            f"Cole a chave de API{masked}.\n\n"
            "Ela fica salva apenas neste computador e nunca vai para a pasta de backup.",
            show="*",
            parent=self,
        )
        if key:
            save_api_key(key, self.local_root)
            self.status_var.set("Chave de API salva.")

    def _start(self) -> None:
        if self._busy or not self.paths:
            return
        config = VisionConfig(
            api_key=load_api_key(self.local_root), model=load_model(self.local_root)
        )
        if not config.api_key:
            messagebox.showerror(
                "Sem chave de API",
                "Configure a chave do Gemini primeiro (botao 'Chave de API...') "
                "ou defina a variavel de ambiente GEMINI_API_KEY.",
                parent=self,
            )
            return

        self._busy = True
        self.read_button.configure(state="disabled")
        self.status_var.set(f"Lendo {len(self.paths)} pagina(s) com {config.model}...")

        def work() -> None:
            """Runs off the main thread: queue only, never a widget."""
            try:
                outcome = extract_names(self.paths, config)
            except VisionError as exc:
                self._outcome.put(("error", str(exc)))
            except Exception as exc:  # never leave the dialog stuck
                self._outcome.put(("error", f"Falha inesperada: {exc}"))
            else:
                self._outcome.put(("ok", outcome))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll_outcome)

    def _poll_outcome(self) -> None:
        """Main-thread pump, mirroring how the runner reports back to the app."""
        try:
            kind, payload = self._outcome.get_nowait()
        except queue.Empty:
            if self._busy:
                self.after(100, self._poll_outcome)
            return
        if kind == "ok":
            self._loaded(payload)
        else:
            self._failed(str(payload))

    def _failed(self, message: str) -> None:
        self._busy = False
        self.read_button.configure(state="normal")
        self.status_var.set("A leitura falhou.")
        messagebox.showerror("Nao foi possivel ler", message, parent=self)

    def _loaded(self, outcome: ExtractionResult) -> None:
        self._busy = False
        self.read_button.configure(state="normal")
        self.extraction = outcome
        if self.ask_parent and not self.parent_var.get().strip():
            self.parent_var.set(outcome.suggested_parent_name())
        self.group_check.configure(state="normal")
        self.add_button.configure(state="normal")

        summary = f"{len(outcome.items)} CD(s) lidos"
        if outcome.box:
            summary += f" - {outcome.box}"
        groups = outcome.group_labels()
        if groups:
            summary += f" - grupos: {', '.join(groups)}"
        if outcome.review_count:
            summary += f" - {outcome.review_count} para conferir"
        for warning in outcome.warnings:
            summary += f" - {warning}"
        self.status_var.set(summary)
        self._refresh()

    # -- table ------------------------------------------------------------

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        if self.extraction is None:
            return
        for index, item in enumerate(self.extraction.items, start=1):
            tags = []
            if item.needs_review:
                tags.append("review")
            if item.uncertain:
                tags.append("uncertain")
            self.tree.insert(
                "",
                "end",
                iid=str(index - 1),
                values=(
                    index,
                    self._group_of(item),
                    item.folder_name,
                    item.raw_text if item.was_changed else "",
                    "; ".join(item.issues) or ("leitura incerta" if item.uncertain else ""),
                ),
                tags=tuple(tags),
            )

    def _group_of(self, item) -> str:
        """The subfolder this disc will land in, or blank when flattened."""
        return item.group_folder_name() if self.group_var.get() else ""

    def _begin_edit(self, event) -> str | None:
        item = self.tree.identify_row(event.y)
        if not item or self.tree.identify_column(event.x) != "#3":
            return None
        self._editor = CellEditor(
            self.tree, item, "folder", self.tree.set(item, "folder"), self._commit
        )
        return "break"

    def _commit(self, item: str, value: str) -> None:
        self._editor = None
        value = value.strip()
        if not value or self.extraction is None:
            return
        try:
            validate_folder_name(value)
        except ValueError as exc:
            messagebox.showerror("Nome invalido", str(exc), parent=self)
            return
        index = int(item)
        entry = self.extraction.items[index]
        entry.folder_name = value
        entry.issues = [issue for issue in entry.issues if "repetido" not in issue]
        entry.uncertain = False
        self._refresh()

    # -- finish -----------------------------------------------------------

    def _accept(self) -> None:
        if self.extraction is None:
            return
        names = [item.folder_name for item in self.extraction.items]

        invalid = []
        for name in names:
            try:
                validate_folder_name(name)
            except ValueError as exc:
                invalid.append(f"{name}: {exc}")
        if invalid:
            messagebox.showerror(
                "Nomes invalidos",
                "Corrija antes de continuar:\n\n" + "\n".join(invalid[:8]),
                parent=self,
            )
            return

        folded = [
            (self._group_of(item).casefold(), item.folder_name.casefold())
            for item in self.extraction.items
        ]
        if len(folded) != len(set(folded)):
            repeated = sorted({f"{g}/{n}" if g else n for g, n in folded if folded.count((g, n)) > 1})
            messagebox.showerror(
                "Nomes repetidos",
                "Estes nomes aparecem mais de uma vez:\n\n" + "\n".join(repeated[:8]),
                parent=self,
            )
            return

        parent_name = self.parent_var.get().strip()
        if self.ask_parent:
            if not parent_name:
                messagebox.showerror(
                    "Pasta principal",
                    "Informe o nome da pasta principal do lote.",
                    parent=self,
                )
                return
            try:
                validate_folder_name(parent_name)
            except ValueError as exc:
                messagebox.showerror("Pasta principal invalida", str(exc), parent=self)
                return

        self.result = PhotoImportOutcome(
            parent_name=parent_name,
            drafts=to_drafts(
                self.extraction.items,
                model=self.extraction.model,
                use_groups=self.group_var.get(),
            ),
        )
        self.destroy()

    def _cancel(self) -> None:
        if self._busy and not messagebox.askyesno(
            "Leitura em andamento", "Cancelar a leitura?", parent=self
        ):
            return
        self.result = None
        self.destroy()

    def show(self):
        self.grab_set()
        self.wait_window()
        return self.result
