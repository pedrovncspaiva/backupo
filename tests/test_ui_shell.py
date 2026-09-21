"""The window shell: menu bar, stage swap, shortcuts and shared assets.

Drives the real window (hidden, no mainloop), like ``test_app``. These are all
things that look fine in a screenshot and are broken in use - a menu item
wired to a label that no longer exists, a button that can only ever be reached
while it is disabled, Space starting a copy because the cursor happened to be
in a text field.
"""

import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from backupov2.jobmodel import EntryDraft, JobSettings
from backupov2.jobstore import JobStore
from backupov2.ui import theme
from backupov2.ui.app import BackupApp
from backupov2.ui.menubar import JOB_ITEMS


class ShellCase(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.app = BackupApp()
        except tk.TclError as exc:
            self.skipTest(f"no display available for Tk: {exc}")
        self.app.withdraw()
        self.addCleanup(self.app.destroy)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest = Path(self._tmp.name) / "dest"
        self.dest.mkdir()

    def open_job(self, entries=("Disco 01", "Disco 02")) -> JobStore:
        store = JobStore.create(
            self.dest, "Lote", settings=JobSettings(),
            local_root=Path(self._tmp.name) / "local",
        )
        store.job.add_entries([EntryDraft(name) for name in entries])
        store.job.parent_path.mkdir(parents=True, exist_ok=True)
        store.save()
        self.app._attach(store)
        return store


class MenuBarTests(ShellCase):
    def menus(self) -> dict:
        bar = self.app.menubar
        return {"file": bar.file, "batch": bar.batch, "disc": bar.disc}

    def test_every_gated_label_exists_in_its_menu(self) -> None:
        """JOB_ITEMS addresses items by label, so a renamed item would
        otherwise stop being enabled with no error anywhere."""
        menus = self.menus()
        for key, labels in JOB_ITEMS.items():
            for label in labels:
                with self.subTest(menu=key, item=label):
                    menus[key].index(label)  # raises TclError if absent

    def test_job_items_are_disabled_with_nothing_open(self) -> None:
        menus = self.menus()
        for key, labels in JOB_ITEMS.items():
            for label in labels:
                with self.subTest(menu=key, item=label):
                    self.assertEqual(menus[key].entrycget(label, "state"), "disabled")

    def test_opening_a_job_enables_them(self) -> None:
        self.open_job()
        menus = self.menus()
        for key, labels in JOB_ITEMS.items():
            for label in labels:
                with self.subTest(menu=key, item=label):
                    self.assertEqual(menus[key].entrycget(label, "state"), "normal")

    def test_new_job_is_the_one_item_that_inverts(self) -> None:
        self.assertEqual(self.app.menubar.file.entrycget("Novo trabalho", "state"), "normal")
        self.open_job()
        self.assertEqual(self.app.menubar.file.entrycget("Novo trabalho", "state"), "disabled")

    def test_closing_a_job_disables_them_again(self) -> None:
        self.open_job()
        self.app._close_job()
        self.assertEqual(
            self.app.menubar.disc.entrycget("Iniciar copia agora", "state"), "disabled"
        )

    def test_the_recent_submenu_is_never_empty_of_meaning(self) -> None:
        """An empty cascade looks broken; it says so instead."""
        self.app.menubar.refresh_recent()
        self.assertIsNotNone(self.app.menubar.recent_menu.index("end"))


class StageTests(ShellCase):
    """Setup card and batch card are two states of one slot, never both."""

    def test_only_the_setup_card_shows_with_nothing_open(self) -> None:
        self.app.update_idletasks()
        self.assertTrue(self.app.setup.winfo_manager())
        self.assertFalse(self.app.header.winfo_manager())

    def test_opening_a_job_swaps_the_cards(self) -> None:
        self.open_job()
        self.app.update_idletasks()
        self.assertFalse(self.app.setup.winfo_manager())
        self.assertTrue(self.app.header.winfo_manager())

    def test_closing_swaps_them_back(self) -> None:
        self.open_job()
        self.app._close_job()
        self.app.update_idletasks()
        self.assertTrue(self.app.setup.winfo_manager())
        self.assertFalse(self.app.header.winfo_manager())

    def test_cleanup_is_reachable_while_a_job_is_open(self) -> None:
        """The bug this fixes: the button lived on the card that is only
        visible when no job is open, so it was always disabled."""
        self.open_job()
        self.assertEqual(str(self.app.clean_button["state"]), "normal")
        self.assertTrue(self.app.clean_button.winfo_manager())

    def test_the_toolbar_is_inert_with_nothing_open(self) -> None:
        for button in (self.app.add_button, self.app.up_button, self.app.down_button):
            self.assertEqual(str(button["state"]), "disabled")

    def test_importing_photos_stays_available_with_nothing_open(self) -> None:
        """It is one of the ways to *create* a job - the sheet names the
        batch folder too."""
        self.assertEqual(str(self.app.photo_button["state"]), "normal")


class DiscControlTests(ShellCase):
    def test_disc_controls_start_disabled(self) -> None:
        panel = self.app.disc_panel
        for button in (panel.start_button, panel.skip_button, panel.cancel_button,
                       panel.send_button, panel.pause_button, panel.eject_button):
            self.assertEqual(str(button["state"]), "disabled")

    def test_the_destination_box_is_hidden_until_there_is_one(self) -> None:
        self.app.update_idletasks()
        self.assertFalse(self.app.disc_panel._target_card.winfo_manager())

    def test_it_appears_with_a_target(self) -> None:
        self.app.disc_panel.show_target("EG 1841  >  Disco 01")
        self.app.update_idletasks()
        self.assertTrue(self.app.disc_panel._target_card.winfo_manager())


class ShortcutTests(ShellCase):
    def test_space_does_not_start_a_copy_while_typing(self) -> None:
        """Space is bound on the window so it works wherever the focus is,
        which means it also fires while the batch name is being typed."""
        self.app.focus_get = lambda: self.app.destination_entry
        self.assertTrue(self.app._is_typing())
        self.assertIsNone(self.app._space_pressed())

    def test_space_acts_when_the_focus_is_not_a_text_field(self) -> None:
        self.app.focus_get = lambda: self.app.entries_view.tree
        self.assertFalse(self.app._is_typing())
        self.assertEqual(self.app._space_pressed(), "break")

    def test_f2_opens_the_inline_editor_on_the_selected_row(self) -> None:
        store = self.open_job()
        self.app.entries_view.select(store.job.entries[0].entry_id)
        self.app._rename_selected()
        self.assertIsNotNone(self.app.entries_view._editor)
        self.app.entries_view._editor._cancel()

    def test_f2_with_no_selection_does_nothing(self) -> None:
        self.open_job()
        self.app._rename_selected()  # must not raise
        self.assertIsNone(self.app.entries_view._editor)

    def test_a_menu_command_with_no_selection_does_nothing(self) -> None:
        self.open_job()
        self.app._selected_command("delete")  # must not raise or prompt


class LogPanelTests(ShellCase):
    def test_hiding_the_log_panel_unmaps_it(self) -> None:
        self.app.show_log_var.set(False)
        self.app._toggle_log_panel()
        self.app.update_idletasks()
        self.assertFalse(self.app.notebook.winfo_manager())

    def test_showing_it_again_brings_it_back(self) -> None:
        self.app.show_log_var.set(False)
        self.app._toggle_log_panel()
        self.app.show_log_var.set(True)
        self.app._toggle_log_panel()
        self.app.update_idletasks()
        self.assertTrue(self.app.notebook.winfo_manager())

    def test_warnings_reach_both_tabs(self) -> None:
        self.app.log("algo estranho", "warn")
        self.assertIn("algo estranho", self.app.log_view.contents())
        self.assertIn("algo estranho", self.app.problems.contents())

    def test_each_line_carries_the_time_it_arrived(self) -> None:
        self.app.log("copiando")
        first = self.app.log_view.contents().splitlines()[0]
        self.assertRegex(first, r"^\d\d:\d\d:\d\d\s+copiando$")

    def test_copying_the_log_puts_it_on_the_clipboard(self) -> None:
        self.app.log("uma linha")
        self.app._copy_log()
        self.assertIn("uma linha", self.app.clipboard_get())

    def test_clearing_empties_both(self) -> None:
        self.app.log("erro", "error")
        self.app._clear_logs()
        self.assertEqual(self.app.log_view.contents(), "")
        self.assertEqual(self.app.problems.contents(), "")


class HelpTests(ShellCase):
    def test_help_opens_on_the_topic_asked_for(self) -> None:
        self.app._show_help("atalhos")
        self.addCleanup(self.app._help_window.destroy)
        self.assertEqual(self.app._help_window.topics.selection(), ("atalhos",))

    def test_a_second_request_reuses_the_window(self) -> None:
        self.app._show_help("primeiros-passos")
        first = self.app._help_window
        self.addCleanup(first.destroy)
        self.app._show_help("problemas")
        self.assertIs(self.app._help_window, first)
        self.assertEqual(first.topics.selection(), ("problemas",))

    def test_every_topic_renders(self) -> None:
        from backupov2.ui.guide import TOPICS

        self.app._show_help()
        window = self.app._help_window
        self.addCleanup(window.destroy)
        for key in TOPICS:
            with self.subTest(topic=key):
                window.render(key)
                self.assertIn(TOPICS[key][0], window.body.get("1.0", "end"))

    def test_about_lists_the_running_versions(self) -> None:
        from backupov2 import __version__
        from backupov2.ui.guide import AboutDialog

        dialog = AboutDialog(self.app)
        self.addCleanup(dialog.destroy)
        names = [name for name, _ in dialog._stack()]
        self.assertIn("Python", names)
        self.assertIn("Interface", names)
        dialog._copy()
        self.assertIn(__version__, dialog.clipboard_get())


class AssetTests(unittest.TestCase):
    """The logo is cached, and a cached image belongs to one interpreter."""

    def test_the_assets_folder_is_where_the_code_looks(self) -> None:
        """Frozen or not, these two files have to be findable - the window
        icon and the mark both read from disk at runtime."""
        self.assertTrue(theme.ASSETS.is_dir(), theme.ASSETS)
        self.assertTrue((theme.ASSETS / "app-icon.ico").is_file())
        for size in (24, 32, 48, 96):
            with self.subTest(size=size):
                self.assertTrue((theme.ASSETS / f"logo-{size}.png").is_file())

    def test_the_logo_loads(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display available for Tk: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        self.assertIsNotNone(theme.load_logo(root, 32))

    def test_a_second_interpreter_gets_its_own_image(self) -> None:
        """Handing window two an image made by window one fails with
        'image pyimageN doesn't exist' - which is how a whole test module
        came to skip itself."""
        try:
            first = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display available for Tk: {exc}")
        first.withdraw()
        theme.load_logo(first, 32)
        first.destroy()

        second = tk.Tk()
        second.withdraw()
        self.addCleanup(second.destroy)
        image = theme.load_logo(second, 32)
        self.assertIsNotNone(image)
        self.assertEqual(image.width(), 32)  # usable, not a dangling handle


if __name__ == "__main__":
    unittest.main()
