"""The window's menu bar.

Every command in the app is reachable from here, including the ones that also
have a button. That is the point: a toolbar can only carry the handful of
actions that fit, and a context menu only helps if you already know to
right-click. The menu is the place you can *browse* what the program does.

Items that need an open job are disabled while there is none, rather than
failing quietly when clicked.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

from ..jobstore import load_recent

# Labels that only make sense with a job open, per menu. Kept as labels rather
# than indexes so inserting an item above one never silently re-aims the
# enable/disable at its neighbour.
JOB_ITEMS = {
    "file": ["Fechar trabalho", "Abrir pasta do lote", "Limpar arquivos de controle..."],
    "batch": [
        "Adicionar pastas...",
        "Mover para cima",
        "Mover para baixo",
        "Renomear pasta selecionada",
        "Excluir pasta da lista",
        "Abrir pasta selecionada",
        "Copia automatica",
    ],
    "disc": [
        "Iniciar copia agora",
        "Pular disco",
        "Pausar / Retomar",
        "Cancelar copia",
        "Ejetar disco",
        "Enviar disco para...",
        "Marcar disco como defeituoso",
    ],
}


class MenuBar(tk.Menu):
    """Builds the bar and keeps it in step with whether a job is open."""

    def __init__(self, app) -> None:
        super().__init__(app, tearoff=0)
        self.app = app

        self.file = self._submenu("Arquivo")
        self.batch = self._submenu("Lote")
        self.disc = self._submenu("Disco")
        self.view = self._submenu("Exibir")
        self.help = self._submenu("Ajuda")

        self._build_file()
        self._build_batch()
        self._build_disc()
        self._build_view()
        self._build_help()

        app.configure(menu=self)
        self.set_job_open(False)

    def _submenu(self, label: str) -> tk.Menu:
        menu = tk.Menu(self, tearoff=0)
        self.add_cascade(label=label, menu=menu)
        return menu

    # -- the menus --------------------------------------------------------

    def _build_file(self) -> None:
        app = self.app
        self.file.add_command(label="Novo trabalho", accelerator="Ctrl+N",
                              command=app._new_job)
        self.file.add_command(label="Abrir trabalho...", accelerator="Ctrl+O",
                              command=app._open_job)

        self.recent_menu = tk.Menu(self.file, tearoff=0)
        self.file.add_cascade(label="Trabalhos recentes", menu=self.recent_menu)

        self.file.add_separator()
        self.file.add_command(label="Fechar trabalho", accelerator="Ctrl+W",
                              command=app._close_job)
        self.file.add_separator()
        self.file.add_command(label="Abrir pasta do lote", accelerator="Ctrl+E",
                              command=app._open_parent_folder)
        self.file.add_command(label="Limpar arquivos de controle...",
                              command=app._clean_control_files)
        self.file.add_separator()
        self.file.add_command(label="Sair", accelerator="Ctrl+Q", command=app._close)

    def _build_batch(self) -> None:
        app = self.app
        self.batch.add_command(label="Adicionar pastas...", accelerator="Ctrl+Shift+A",
                               command=app._add_entries)
        self.batch.add_command(label="Importar de fotos...", accelerator="Ctrl+I",
                               command=app._import_from_photos)
        self.batch.add_separator()
        self.batch.add_command(label="Mover para cima", accelerator="Alt+Cima",
                               command=lambda: app._move(-1))
        self.batch.add_command(label="Mover para baixo", accelerator="Alt+Baixo",
                               command=lambda: app._move(1))
        self.batch.add_command(label="Renomear pasta selecionada", accelerator="F2",
                               command=app._rename_selected)
        self.batch.add_command(label="Excluir pasta da lista", accelerator="Del",
                               command=lambda: app._selected_command("delete"))
        self.batch.add_separator()
        self.batch.add_command(label="Abrir pasta selecionada",
                               command=lambda: app._selected_command("open_folder"))
        self.batch.add_separator()
        self.batch.add_checkbutton(label="Copia automatica", variable=app.auto_var,
                                   command=app._toggle_auto)

    def _build_disc(self) -> None:
        app = self.app
        self.disc.add_command(label="Iniciar copia agora", accelerator="Espaco",
                              command=lambda: app._disc_command("start_now"))
        self.disc.add_command(label="Pular disco",
                              command=lambda: app._disc_command("skip_disc"))
        self.disc.add_command(label="Pausar / Retomar", accelerator="Ctrl+P",
                              command=lambda: app._disc_command("pause"))
        self.disc.add_command(label="Cancelar copia", accelerator="Esc",
                              command=lambda: app._disc_command("cancel"))
        self.disc.add_separator()
        self.disc.add_command(label="Ejetar disco", accelerator="Ctrl+J",
                              command=lambda: app._disc_command("eject"))
        self.disc.add_command(label="Enviar disco para...", accelerator="Ctrl+D",
                              command=lambda: app._disc_command("send_to"))
        self.disc.add_separator()
        self.disc.add_command(label="Marcar disco como defeituoso",
                              command=lambda: app._disc_command("mark_corrupted"))

    def _build_view(self) -> None:
        app = self.app
        self.view.add_checkbutton(label="Mostrar painel de registro",
                                  variable=app.show_log_var, command=app._toggle_log_panel)
        self.view.add_separator()
        self.view.add_command(label="Limpar registro", command=app._clear_logs)
        self.view.add_command(label="Copiar registro", command=app._copy_log)

    def _build_help(self) -> None:
        app = self.app
        self.help.add_command(label="Ajuda e guias", accelerator="F1",
                              command=app._show_help)
        self.help.add_separator()
        self.help.add_command(label="Primeiros passos",
                              command=lambda: app._show_help("primeiros-passos"))
        self.help.add_command(label="Um conjunto com varios discos",
                              command=lambda: app._show_help("varios-discos"))
        self.help.add_command(label="Quando algo da errado",
                              command=lambda: app._show_help("problemas"))
        self.help.add_command(label="Atalhos de teclado",
                              command=lambda: app._show_help("atalhos"))
        self.help.add_separator()
        self.help.add_command(label="Abrir o README do projeto",
                              command=app._open_readme)
        self.help.add_separator()
        self.help.add_command(label="Sobre o programa", command=app._show_about)

    # -- state ------------------------------------------------------------

    def set_job_open(self, open_: bool) -> None:
        state = "normal" if open_ else "disabled"
        for menu, labels in (
            (self.file, JOB_ITEMS["file"]),
            (self.batch, JOB_ITEMS["batch"]),
            (self.disc, JOB_ITEMS["disc"]),
        ):
            for label in labels:
                try:
                    menu.entryconfigure(label, state=state)
                except tk.TclError:  # pragma: no cover - label typo guard
                    pass
        # Creating one is exactly what you do when none is open.
        self.file.entryconfigure("Novo trabalho", state="disabled" if open_ else "normal")

    def refresh_recent(self) -> None:
        """Rebuild the recent list. Same source as the toolbar's button."""
        self.recent_menu.delete(0, "end")
        items = load_recent()
        if not items:
            self.recent_menu.add_command(label="(nenhum trabalho recente)",
                                         state="disabled")
            return
        for item in items[:10]:
            label = f"{item.get('parent_name')}   -   {item.get('updated_utc', '')[:10]}"
            path = Path(item.get("local_path", ""))
            self.recent_menu.add_command(label=label,
                                         command=lambda p=path: self.app._load(p))
