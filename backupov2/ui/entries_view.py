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
from . import theme
from .widgets import CellEditor, autohide, format_bytes, format_stamp

COLUMNS = ("index", "group", "folder", "status", "drive", "kind", "label", "files",
           "size", "finished")
HEADINGS = {
    "index": "#",
    "group": "Subpasta (EG)",
    "folder": "Pasta",
    "status": "Situacao",
    "drive": "Un.",
    "kind": "Tipo",
    "label": "Rotulo",
    "files": "Arquivos",
    "size": "Tamanho",
    "finished": "Concluido",
}
# Sized so the nine of them add up to less than the pane gets on a default
# 1280-wide window: the horizontal scrollbar is there for a narrowed window or
# a column dragged wider, not as a permanent fixture under a half-empty table.
WIDTHS = {
    "index": 38,
    "group": 104,
    "folder": 162,
    # Wide enough for the longest thing _situation() builds, which is
    # "falhou (defeito)" behind its dot - the one row where a clipped word
    # loses the reason.
    "status": 124,
    # Empty on every row until a second drive exists, which is the
    # only time whose-is-whose is a question worth a column.
    "drive": 48,
    "kind": 54,
    "label": 84,
    "files": 78,     # its own heading is the widest thing in it
    "size": 76,
    "finished": 84,
}

# What the text columns shrink to when the drive column is showing and the
# pane has given width to a second disc panel.
NARROW_WIDTHS = dict(
    WIDTHS,
    group=98,
    folder=122,
    label=76,
    size=70,
)

COLLECT_ON_LABEL = "Acumular discos nesta pasta"
COLLECT_OFF_LABEL = "Parar de acumular nesta pasta"

# Names the buttons, without trying to redraw their icons in text: the
# toolbar's marks are images now, and the Unicode stand-ins that used to sit
# here rendered as a different shape from the button they were pointing at.
EMPTY_MESSAGE = (
    "Nenhuma pasta na fila ainda.\n\n"
    'Use "Adicionar pastas..." para colar a lista,\n'
    'ou "Importar de fotos..." para ler o protocolo de entrega.'
)

# Floors, so dragging one column wider squeezes its neighbours only so far and
# a heading can always still be read. Below these the horizontal scrollbar
# takes over instead of the text collapsing to nothing.
MIN_WIDTHS = {
    "index": 38,
    "group": 90,
    "folder": 140,
    "status": 100,
    "drive": 44,
    "kind": 46,
    "label": 70,
    "files": 54,
    "size": 62,
    # "21/09 15:57", not the full ISO stamp it used to hold.
    "finished": 80,
}

# A dot in front of the word, so a row's situation survives a screenshot, a
# projector, and colour-blind eyes - colour alone was carrying it before.
STATUS_DOTS = {
    EntryStatus.PENDING: theme.STATUS_DOT["pending"],
    EntryStatus.IN_PROGRESS: theme.STATUS_DOT["in_progress"],
    EntryStatus.DONE: theme.STATUS_DOT["done"],
    EntryStatus.SKIPPED: theme.STATUS_DOT["skipped"],
    EntryStatus.FAILED: theme.STATUS_DOT["failed"],
}


class EntriesView(ttk.Frame):
    def __init__(
        self,
        master,
        on_rename: Callable[[str, str], None],
        on_command: Callable[[str, str], None],
        **kwargs,
    ) -> None:
        kwargs.setdefault("style", "Card.TFrame")
        super().__init__(master, **kwargs)
        self.on_rename = on_rename
        self.on_command = on_command
        self._editor: CellEditor | None = None
        # Folders being written right now - one per busy drive.
        self._protected: set[str] = set()
        # entry_id -> drive, so a row can say which drive has it.
        self._claimed: dict[str, str] = {}
        # Which rows are collecting, so the context menu can offer the right
        # half of the toggle without needing the job handed to it again.
        self._collecting: set[str] = set()

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # A title strip, so the two panes read as two named things rather than
        # a table that happens to sit next to a panel.
        header = tk.Frame(self, background=theme.SURFACE, padx=12, pady=8)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(1, weight=1)
        tk.Label(header, text="Fila de pastas", font=theme.FONT_SUBTITLE,
                 background=theme.SURFACE, foreground=theme.INK).grid(row=0, column=0,
                                                                     sticky="w")
        self.hint_var = tk.StringVar(
            value="Duplo clique renomeia  -  botao direito abre todas as acoes")
        tk.Label(header, textvariable=self.hint_var, font=theme.FONT_SMALL,
                 background=theme.SURFACE, foreground=theme.INK_MUTED).grid(
            row=0, column=1, sticky="e")

        self.tree = ttk.Treeview(self, columns=COLUMNS, show="headings", selectmode="browse")
        for name in COLUMNS:
            self.tree.heading(name, text=HEADINGS[name])
            anchor = "center" if name in ("index", "kind", "drive") else "w"
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
        self.tree.grid(row=1, column=0, sticky="nsew")
        self.show_drive_column(False)

        # Both scrollbars come and go with the need for them, the same way the
        # disc panel's does. The columns now add up to less than the pane gets
        # on a default window, so a permanently gridded horizontal bar was an
        # empty trough under a half-empty table saying "there is more here" -
        # and the vertical one said the same under five rows.
        self._vertical = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self._horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(
            yscrollcommand=autohide(self._vertical, row=1, column=1, sticky="ns"),
            xscrollcommand=autohide(self._horizontal, row=2, column=0, sticky="ew"),
        )

        # An empty table that says nothing looks broken. This says what to do
        # next, and is lifted away the moment there is a row to show.
        self.empty_label = tk.Label(
            self.tree, text=EMPTY_MESSAGE, font=theme.FONT_BODY,
            background=theme.SURFACE, foreground=theme.INK_MUTED, justify="center",
        )
        self._show_empty(True)

        self.tree.tag_configure("done", foreground=theme.SUCCESS)
        self.tree.tag_configure("review", foreground=theme.WARNING)
        self.tree.tag_configure("failed", foreground=theme.DANGER)
        self.tree.tag_configure("skipped", foreground=theme.INK_MUTED)
        self.tree.tag_configure("working", font=theme.FONT_BODY_BOLD,
                                foreground=theme.BRAND_DEEP)
        self.tree.tag_configure("next", background=theme.BRAND_TINT)
        # Loud on purpose: while this is on, every disc goes here no matter
        # what the list order says, and that has to be impossible to miss.
        self.tree.tag_configure(
            "collecting", background=theme.AMBER_TINT, font=theme.FONT_BODY_BOLD
        )
        # Alternating bands per EG, so where one group ends and the next
        # begins is visible at a glance rather than inferred from the text.
        self.tree.tag_configure("bandA", background=theme.SURFACE)
        self.tree.tag_configure("bandB", background=theme.STRIPE)

        self.tree.bind("<Double-1>", self._begin_edit)
        self.tree.bind("<Button-3>", self._popup)
        self.tree.bind("<Alt-Up>", lambda e: self._emit("move_up"))
        self.tree.bind("<Alt-Down>", lambda e: self._emit("move_down"))
        self.tree.bind("<Delete>", lambda e: self._emit("delete"))
        self.tree.bind("<Return>", lambda e: self._emit("send_here"))

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

    def show_drive_column(self, shown: bool) -> None:
        """Reveal "Unidade" only when there is more than one of them.

        With a single drive every cell in it holds the same letter, which is
        a column of noise. Showing it also costs width twice over - the
        column itself, and the wider drives pane two panels need - so the
        text columns give some back rather than letting a heading clip.
        """
        self.tree.configure(
            displaycolumns=COLUMNS if shown
            else tuple(c for c in COLUMNS if c != "drive")
        )
        for name, width in (NARROW_WIDTHS if shown else WIDTHS).items():
            self.tree.column(name, width=width)

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

    def clear(self) -> None:
        """Empty the table with no job to show - closing one, with no other
        open yet to refresh() against."""
        self.tree.delete(*self.tree.get_children())
        self._protected = set()
        self._collecting = set()
        self._show_empty(True)

    def _show_empty(self, empty: bool) -> None:
        if empty:
            self.empty_label.place(relx=0.5, rely=0.45, anchor="center")
        else:
            self.empty_label.place_forget()

    def refresh(
        self,
        job: Job,
        working_entry_id: str | None = None,
        working_entry_ids: set[str] | None = None,
        claimed_by: dict[str, str] | None = None,
    ) -> None:
        """Redraw from the job. Cheap enough for the list sizes involved."""
        # Either form: one id from the single-drive caller, or the set a
        # pool of drives produces.
        self._protected = set(working_entry_ids or ())
        if working_entry_id:
            self._protected.add(working_entry_id)
        self._claimed = dict(claimed_by or {})
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
            finished = format_stamp(entry.finished_utc)

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
                    self._situation(entry),
                    self._claimed.get(entry.entry_id, ""),
                    kind_label(entry.disc_kind),
                    entry.media.label if entry.media else "",
                    files,
                    size,
                    finished,
                ),
                tags=self._tags_for(entry, next_id, band_for.get(entry.group)),
            )
            previous_group = entry.group

        self._show_empty(not job.entries)
        if previous and self.tree.exists(previous):
            self.tree.selection_set(previous)
        self.tree.yview_moveto(scroll[0])

    @staticmethod
    def _situation(entry) -> str:
        """What is actually true about this row, in one short phrase."""
        # The plain status would read "concluida" on a folder that is in
        # fact still open for the next disc - say what is actually true.
        if entry.collecting:
            dot = theme.STATUS_DOT["collecting"]
            return (
                f"{dot} acumulando ({entry.disc_count})"
                if entry.disc_count
                else f"{dot} acumulando"
            )
        dot = STATUS_DOTS.get(entry.status, theme.STATUS_DOT["pending"])
        if entry.error == "disc_corrupted":
            # "pulada" alone loses the one thing that matters about this
            # row: the disc is bad, not merely postponed.
            return f"{dot} {status_label(entry.status)} (defeito)"
        return f"{dot} {status_label(entry.status)}"

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

    def begin_rename(self, entry_id: str) -> None:
        """Open the inline editor from somewhere other than a double click.

        Scrolled into view first: the editor is placed over the cell's bbox,
        and a row that is off screen has no bbox to place it on.
        """
        if not self.tree.exists(entry_id) or entry_id in self._protected:
            return
        self.tree.selection_set(entry_id)
        self.tree.see(entry_id)
        self.tree.update_idletasks()
        current = self.tree.set(entry_id, "folder")
        self._editor = CellEditor(self.tree, entry_id, "folder", current, self._commit_edit)

    def _begin_edit(self, event) -> str | None:
        item = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not item or column != f"#{COLUMNS.index('folder') + 1}":
            return None
        if item in self._protected:
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
