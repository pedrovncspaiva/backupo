"""The main window.

Threading discipline, enforced: worker threads only ever put things on the
runner's queue, and the single ``after()`` pump below is the only place any Tk
widget is touched. Nothing else in the package imports tkinter.
"""

from __future__ import annotations

import os
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import winapi
from ..core import DEFECT_REPORT_NAME, ensure_entry_folders, validate_folder_name
from ..jobmodel import EntryDraft, EntryStatus, JobSettings
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
from ..strings import APP_TITLE
from .dialogs import (
    AddEntriesDialog,
    MarkDefectiveDialog,
    ReconcileDialog,
    SendToDialog,
)
from .photo_import import PhotoImportDialog
from .disc_panel import DiscPanel
from .entries_view import EntriesView
from .widgets import LogView, format_bytes

POLL_MS = 1000
TICK_MS = 100

HEADER_BG = "#eef4ff"     # the current-batch banner
HEADER_IDLE_BG = "#f3f4f6"  # same banner with nothing open
TITLE_ACTIVE = "#1e3a8a"
TITLE_IDLE = "#6b7280"
MUTED = "#4b5563"
COLLECT_BG = "#fde68a"    # the "this folder is swallowing every disc" strip
COLLECT_FG = "#7c2d12"


class BackupApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x800")
        self.minsize(900, 640)

        winapi.silence_missing_media_dialogs()

        self.store: JobStore | None = None
        self.runner: JobRunner | None = None
        self.current_media = None

        self.destination_var = tk.StringVar()
        self.parent_var = tk.StringVar()
        self.saved_var = tk.StringVar(value="")
        self.job_title_var = tk.StringVar(value="Nenhum trabalho aberto")
        self.job_path_var = tk.StringVar(
            value="Escolha o Destino e crie um trabalho, ou importe de fotos."
        )
        self.job_counts_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="")
        self.auto_var = tk.BooleanVar(value=True)

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(TICK_MS, self._pump)
        self.after(POLL_MS, self._poll)

    # -- layout -----------------------------------------------------------

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=3)  # the folder list gets the space
        self.rowconfigure(4, weight=1)

        # -- current batch, stated plainly -------------------------------
        # The single most important thing on screen: which folder am I filling
        # right now, and where is it. Given its own banner rather than a text
        # field, because once a job exists those fields are not editable and
        # looking editable is its own kind of lie.
        header = tk.Frame(self, background=HEADER_BG, padx=14, pady=10)
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 0))
        header.columnconfigure(0, weight=1)
        self.header = header

        self.title_label = tk.Label(
            header,
            textvariable=self.job_title_var,
            font=("Segoe UI", 14, "bold"),
            background=HEADER_BG,
            foreground=TITLE_IDLE,
            anchor="w",
        )
        self.title_label.grid(row=0, column=0, sticky="w")

        self.open_folder_button = ttk.Button(
            header, text="Abrir pasta", command=self._open_parent_folder
        )
        self.open_folder_button.grid(row=0, column=1, sticky="e", padx=(12, 0))

        self.path_label = tk.Label(
            header,
            textvariable=self.job_path_var,
            font=("Consolas", 9),
            background=HEADER_BG,
            foreground=MUTED,
            anchor="w",
        )
        self.path_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

        self.counts_label = tk.Label(
            header,
            textvariable=self.job_counts_var,
            background=HEADER_BG,
            foreground=MUTED,
            anchor="w",
        )
        self.counts_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.job_progress = ttk.Progressbar(header, mode="determinate", maximum=100)
        self.job_progress.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        # Only shown while a folder is collecting. It overrides the list order
        # for every disc, so it gets its own strip with its own off switch
        # rather than being a flag you have to go looking for in the table.
        self.collect_bar = tk.Frame(header, background=COLLECT_BG, padx=10, pady=6)
        self.collect_bar.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.collect_bar.columnconfigure(0, weight=1)
        self.collect_var = tk.StringVar(value="")
        tk.Label(
            self.collect_bar,
            textvariable=self.collect_var,
            background=COLLECT_BG,
            foreground=COLLECT_FG,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            self.collect_bar, text="Parar de acumular", command=self._stop_collecting
        ).grid(row=0, column=1, sticky="e", padx=(12, 0))
        self.collect_bar.grid_remove()

        # -- setup, only needed until a job exists -----------------------
        self.setup = ttk.LabelFrame(self, text="Novo trabalho", padding=10)
        self.setup.grid(row=1, column=0, sticky="ew", padx=12, pady=(10, 0))
        self.setup.columnconfigure(1, weight=1)

        ttk.Label(self.setup, text="Destino").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.destination_entry = ttk.Entry(self.setup, textvariable=self.destination_var)
        self.destination_entry.grid(row=0, column=1, sticky="ew", pady=3)
        self.browse_button = ttk.Button(
            self.setup, text="Procurar...", command=self._browse
        )
        self.browse_button.grid(row=0, column=2, padx=(8, 0))

        ttk.Label(self.setup, text="Pasta do lote").grid(
            row=1, column=0, sticky="w", padx=(0, 8)
        )
        self.parent_entry = ttk.Entry(self.setup, textvariable=self.parent_var)
        self.parent_entry.grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(
            self.setup,
            text="ou deixe em branco e use as fotos",
            foreground=MUTED,
        ).grid(row=1, column=2, sticky="w", padx=(8, 0))

        actions = ttk.Frame(self.setup)
        actions.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.new_button = ttk.Button(
            actions, text="Criar trabalho", command=self._new_job
        )
        self.new_button.pack(side="left")
        ttk.Button(actions, text="Abrir...", command=self._open_job).pack(
            side="left", padx=(8, 0)
        )
        self.recent_button = ttk.Menubutton(actions, text="Recentes")
        self.recent_menu = tk.Menu(self.recent_button, tearoff=0)
        self.recent_button.configure(menu=self.recent_menu)
        self.recent_button.pack(side="left", padx=(8, 0))
        self.clean_button = ttk.Button(
            actions, text="Limpar arquivos de controle", command=self._clean_control_files
        )
        self.clean_button.pack(side="left", padx=(8, 0))

        toolbar = ttk.Frame(self, padding=(12, 8))
        toolbar.grid(row=2, column=0, sticky="ew")
        self.auto_check = ttk.Checkbutton(
            toolbar,
            text="Copia automatica",
            variable=self.auto_var,
            command=self._toggle_auto,
        )
        self.auto_check.pack(side="left")
        ttk.Button(toolbar, text="+ Adicionar pastas...", command=self._add_entries).pack(
            side="left", padx=(12, 0)
        )
        self.photo_button = ttk.Button(
            toolbar, text="Importar de fotos...", command=self._import_from_photos
        )
        self.photo_button.pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="v", width=3, command=lambda: self._move(1)).pack(
            side="right"
        )
        ttk.Button(toolbar, text="^", width=3, command=lambda: self._move(-1)).pack(
            side="right", padx=(0, 4)
        )

        middle = ttk.PanedWindow(self, orient="horizontal")
        middle.grid(row=3, column=0, sticky="nsew", padx=12)

        self.entries_view = EntriesView(
            middle, on_rename=self._rename_entry, on_command=self._entry_command
        )
        middle.add(self.entries_view, weight=4)

        self.disc_panel = DiscPanel(middle, on_command=self._disc_command)
        middle.add(self.disc_panel, weight=2)

        notebook = ttk.Notebook(self)
        notebook.grid(row=4, column=0, sticky="nsew", padx=12, pady=(10, 0))
        self.log_view = LogView(notebook)
        notebook.add(self.log_view, text="Registro")
        self.problems = LogView(notebook)
        notebook.add(self.problems, text="Problemas")

        ttk.Label(self, textvariable=self.status_var, foreground="#4b5563").grid(
            row=5, column=0, sticky="w", padx=12, pady=(6, 10)
        )

        self.bind("<space>", lambda e: self._disc_command("start_now"))
        self.bind("<Escape>", lambda e: self._disc_command("cancel"))
        self.bind("<Control-n>", lambda e: self._new_job())
        self.bind("<Control-o>", lambda e: self._open_job())

        self._refresh_recent()
        self._set_controls_enabled(False)
        self._paint_header()

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.auto_check.configure(state=state)
        self.clean_button.configure(state=state)
        self.open_folder_button.configure(state=state)
        # Deliberately not gated on a job: importing photos is one of the ways
        # to *create* one, since the sheet names the batch folder too.

        # Once a job exists these fields no longer do anything, so they stop
        # inviting edits rather than silently ignoring them.
        for widget in (self.destination_entry, self.parent_entry, self.browse_button,
                       self.new_button):
            widget.configure(state="disabled" if enabled else "normal")
        self.setup.configure(
            text="Trabalho aberto - use Abrir ou Recentes para trocar"
            if enabled
            else "Novo trabalho"
        )

    def _paint_header(self) -> None:
        """Keep the banner showing the batch that is actually open."""
        active = self.store is not None
        background = HEADER_BG if active else HEADER_IDLE_BG
        for widget in (self.header, self.title_label, self.path_label, self.counts_label):
            widget.configure(background=background)
        self.title_label.configure(foreground=TITLE_ACTIVE if active else TITLE_IDLE)

        if not active:
            self.job_title_var.set("Nenhum trabalho aberto")
            self.job_path_var.set(
                "Escolha o Destino e crie um trabalho, ou importe de fotos."
            )
            self.job_counts_var.set("")
            self.job_progress["value"] = 0
            self.collect_bar.grid_remove()
            return

        job = self.store.job
        self.job_title_var.set(job.parent_name)
        self.job_path_var.set(str(job.parent_path))

        counts = job.count_by_status()
        done = counts[EntryStatus.DONE]
        total = len(job)
        parts = [f"{done} de {total} concluida(s)"]
        if counts[EntryStatus.FAILED]:
            parts.append(f"{counts[EntryStatus.FAILED]} com falha")
        if counts[EntryStatus.SKIPPED]:
            parts.append(f"{counts[EntryStatus.SKIPPED]} pulada(s)")
        groups = job.group_names()
        if groups:
            parts.append(f"{len(groups)} EG")
        if self.saved_var.get():
            parts.append(self.saved_var.get())
        self.job_counts_var.set("   -   ".join(parts))
        self.job_progress["value"] = (done / total * 100) if total else 0

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
        self._set_controls_enabled(True)
        self._refresh()
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
            messagebox.showinfo(
                "Renomear", "Clique duas vezes no nome da pasta para edita-lo."
            )
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
        self.saved_var.set(f"salvo {job.updated_utc[11:19]}")
        self._paint_header()

        pending = job.next_pending()
        if pending is not None and self.runner and not self.runner.is_busy:
            self.disc_panel.show_target(self._target_label(pending))

    def _refresh_recent(self) -> None:
        self.recent_menu.delete(0, "end")
        items = load_recent()
        if not items:
            self.recent_menu.add_command(label="(nenhum)", state="disabled")
            return
        for item in items[:10]:
            label = f"{item.get('parent_name')} - {item.get('updated_utc', '')[:10]}"
            path = Path(item.get("local_path", ""))
            self.recent_menu.add_command(
                label=label, command=lambda p=path: self._load(p)
            )

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
