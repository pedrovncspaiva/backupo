"""The list of folders, and every action you can take on it.

Stays fully editable while a copy is running - only the row actually being
written is protected. The Treeview ``iid`` is the entry's ``entry_id``, never a
position, so reordering and inserting never renumber or mis-target anything.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from ..core import validate_folder_name
from ..jobmodel import EntryStatus, Job
from ..strings import kind_label, status_label
from .widgets import CellEditor, format_bytes

COLUMNS = ("index", "group", "folder", "status", "kind", "label", "files", "size", "finished")
HEADINGS = {
    "index": "#",
    "group": "Subpasta (EG)",
    "folder": "Pasta",
    "status": "Situacao",
    "kind": "Tipo",
    "label": "Rotulo do disco",
    "files": "Arquivos",
    "size": "Tamanho",
    "finished": "Concluido",
}
WIDTHS = {
    "index": 40,
    "group": 150,
    "folder": 260,
    "status": 86,
    "kind": 58,
    "label": 120,
    "files": 70,
    "size": 80,
    "finished": 120,
}

COLLECT_ON_LABEL = "Acumular discos nesta pasta"
COLLECT_OFF_LABEL = "Parar de acumular nesta pasta"

# Floors, so dragging one column wider squeezes its neighbours only so far and
# a heading can always still be read. Below these the horizontal scrollbar
# takes over instead of the text collapsing to nothing.
MIN_WIDTHS = {
    "index": 34,
    "group": 90,
    "folder": 140,
    "status": 70,
    "kind": 46,
    "label": 70,
    "files": 52,
    "size": 60,
    "finished": 90,
}


class EntriesView(ttk.Frame):
    def __init__(
        self,
        master,
        on_rename: Callable[[str, str], None],
        on_command: Callable[[str, str], None],
        **kwargs,
    ) -> None:
        super().__init__(master, **kwargs)
        self.on_rename = on_rename
        self.on_command = on_command
        self._editor: CellEditor | None = None
        self._protected: str | None = None
        # Which rows are collecting, so the context menu can offer the right
        # half of the toggle without needing the job handed to it again.
        self._collecting: set[str] = set()

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(self, columns=COLUMNS, show="headings", selectmode="browse")
        for name in COLUMNS:
            self.tree.heading(name, text=HEADINGS[name])
            anchor = "center" if name in ("index", "status", "kind") else "w"
            if name in ("files", "size"):
                anchor = "e"
            self.tree.column(
                name,
                width=WIDTHS[name],
                minwidth=MIN_WIDTHS[name],
                anchor=anchor,
                # No column stretches. A stretchable column is recomputed to fit
                # the pane on every geometry pass, so dragging it wider snaps
                # straight back - which is exactly the behaviour to avoid here.
                # Overflow is the horizontal scrollbar's job instead.
                stretch=False,
            )
        self.tree.grid(row=0, column=0, sticky="nsew")

        vertical = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        # The columns are wider than the pane, so without this the table simply
        # squeezes and a widened column has nowhere to go.
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(
            yscrollcommand=vertical.set, xscrollcommand=horizontal.set
        )

        self.tree.tag_configure("done", foreground="#0b6b3a")
        self.tree.tag_configure("review", foreground="#8a5a00")
        self.tree.tag_configure("failed", foreground="#a4161a")
        self.tree.tag_configure("skipped", foreground="#6b7280")
        self.tree.tag_configure("working", font=("Segoe UI", 9, "bold"))
        self.tree.tag_configure("next", background="#dbeafe")
        # Loud on purpose: while this is on, every disc goes here no matter
        # what the list order says, and that has to be impossible to miss.
        self.tree.tag_configure(
            "collecting", background="#fde68a", font=("Segoe UI", 9, "bold")
        )
        # Alternating bands per EG, so where one group ends and the next
        # begins is visible at a glance rather than inferred from the text.
        self.tree.tag_configure("bandA", background="#ffffff")
        self.tree.tag_configure("bandB", background="#f6f7f9")

        self.tree.bind("<Double-1>", self._begin_edit)
        self.tree.bind("<Button-3>", self._popup)
        self.tree.bind("<Alt-Up>", lambda e: self._emit("move_up"))
        self.tree.bind("<Alt-Down>", lambda e: self._emit("move_down"))
        self.tree.bind("<Delete>", lambda e: self._emit("delete"))

        self.menu = tk.Menu(self, tearoff=0)
        self._collect_index = 1  # patched per-popup to match the clicked row
        for label, command in (
            ("Enviar disco atual para aqui", "send_here"),
            (COLLECT_ON_LABEL, "collect_on"),
            (None, None),
            ("Inserir pasta acima", "insert_above"),
            ("Inserir pasta abaixo", "insert_below"),
            ("Renomear", "rename"),
            (None, None),
            ("Marcar como pendente", "mark_pending"),
            ("Marcar como pulada", "mark_skipped"),
            ("Marcar como pulada - disco defeituoso...", "mark_defective"),
            ("Tentar novamente", "retry"),
            (None, None),
            ("Abrir no Explorer", "open_folder"),
            ("Excluir pasta da lista", "delete"),
        ):
            if label is None:
                self.menu.add_separator()
            else:
                self.menu.add_command(
                    label=label, command=lambda c=command: self._emit(c)
                )

    # -- selection --------------------------------------------------------

    @property
    def selected(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def select(self, entry_id: str) -> None:
        if self.tree.exists(entry_id):
            self.tree.selection_set(entry_id)
            self.tree.see(entry_id)

    def _emit(self, command: str) -> str:
        entry_id = self.selected
        if entry_id:
            self.on_command(command, entry_id)
        return "break"

    def _popup(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)
        # One entry that reads as the action available on *this* row, rather
        # than two that are each wrong half the time.
        on = item in self._collecting
        self.menu.entryconfigure(
            self._collect_index,
            label=COLLECT_OFF_LABEL if on else COLLECT_ON_LABEL,
            command=lambda: self._emit("collect_off" if on else "collect_on"),
        )
        self.menu.tk_popup(event.x_root, event.y_root)

    # -- rendering --------------------------------------------------------

    def refresh(self, job: Job, working_entry_id: str | None = None) -> None:
        """Redraw from the job. Cheap enough for the list sizes involved."""
        self._protected = working_entry_id
        previous = self.selected
        scroll = self.tree.yview()

        self.tree.delete(*self.tree.get_children())
        next_pending = job.next_pending()
        next_id = next_pending.entry_id if next_pending else None
        self._collecting = {
            entry.entry_id for entry in job.entries if entry.collecting
        }

        band_for = {
            name: ("bandA" if position % 2 == 0 else "bandB")
            for position, name in enumerate(job.group_names())
        }
        previous_group: str | None = None

        for index, entry in enumerate(job.entries, start=1):
            result = entry.result
            files = ""
            if result and (result.files_copied or result.tracks_ripped):
                files = str(result.files_copied or result.tracks_ripped)
                if result.files_failed:
                    files += f" (!{result.files_failed})"
            size = format_bytes(result.bytes_copied) if result and result.bytes_copied else ""
            finished = (entry.finished_utc or "").replace("T", " ").rstrip("Z")
            # The plain status would read "concluida" on a folder that is in
            # fact still open for the next disc - say what is actually true.
            if entry.collecting:
                situation = (
                    f"acumulando ({entry.disc_count})"
                    if entry.disc_count
                    else "acumulando"
                )
            elif entry.error == "disc_corrupted":
                # "pulada" alone loses the one thing that matters about this
                # row: the disc is bad, not merely postponed.
                situation = f"{status_label(entry.status)} (defeito)"
            else:
                situation = status_label(entry.status)

            self.tree.insert(
                "",
                "end",
                iid=entry.entry_id,
                values=(
                    index,
                    # Repeating the EG on every row is noise; showing it only
                    # where it changes reads like the sheet it came from.
                    entry.group if entry.group != previous_group else "",
                    entry.folder_name,
                    situation,
                    kind_label(entry.disc_kind),
                    entry.media.label if entry.media else "",
                    files,
                    size,
                    finished,
                ),
                tags=self._tags_for(entry, next_id, band_for.get(entry.group)),
            )
            previous_group = entry.group

        if previous and self.tree.exists(previous):
            self.tree.selection_set(previous)
        self.tree.yview_moveto(scroll[0])

    @staticmethod
    def _tags_for(entry, next_id: str | None, band: str | None = None) -> tuple[str, ...]:
        tags: list[str] = []
        # "next" first so its highlight beats the group band; the status tags
        # below only set a foreground, so they never conflict with either.
        # Collecting outranks both - it is the one that changes where a disc
        # goes, so it must not be the one that loses the styling contest.
        if entry.collecting:
            tags.append("collecting")
        elif entry.entry_id == next_id:
            tags.append("next")
        elif band:
            tags.append(band)
        if entry.status is EntryStatus.DONE:
            tags.append("review" if entry.needs_review else "done")
        elif entry.status is EntryStatus.FAILED:
            tags.append("failed")
        elif entry.status is EntryStatus.SKIPPED:
            tags.append("skipped")
        elif entry.status is EntryStatus.IN_PROGRESS:
            tags.append("working")
        return tuple(tags)

    # -- inline rename ----------------------------------------------------

    def _begin_edit(self, event) -> str | None:
        item = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not item or column != f"#{COLUMNS.index('folder') + 1}":
            return None
        if item == self._protected:
            return "break"  # never rename the folder being written right now

        current = self.tree.set(item, "folder")
        self._editor = CellEditor(self.tree, item, "folder", current, self._commit_edit)
        return "break"

    def _commit_edit(self, item: str, value: str) -> None:
        self._editor = None
        value = value.strip()
        if not value or value == self.tree.set(item, "folder"):
            return
        try:
            validate_folder_name(value)
        except ValueError as exc:
            from tkinter import messagebox

            messagebox.showerror("Nome invalido", str(exc), parent=self)
            return
        self.on_rename(item, value)
