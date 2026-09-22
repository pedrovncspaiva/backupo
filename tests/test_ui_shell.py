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
from datetime import datetime, timezone
from pathlib import Path
from tkinter import ttk

from backupov2.jobmodel import EntryDraft, JobSettings
from backupov2.jobstore import JobStore
from backupov2.media import FakeScanner
from backupov2.runner import LogLine, RunnerState, StateChanged
from backupov2.ui import theme
from backupov2.ui.app import BackupApp
from backupov2.ui.menubar import JOB_ITEMS
from backupov2.ui.widgets import format_stamp


def descendants(widget: tk.Misc):
    """Every widget under ``widget``, itself excluded."""
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class ShellCase(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.app = BackupApp()
        except tk.TclError as exc:
            self.skipTest(f"no display available for Tk: {exc}")
        # Pin the hardware: otherwise these build a different window on a
        # laptop with no optical drive than on the machine with two.
        self.app.make_scanner = lambda: FakeScanner([], drives=("D:",))
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

    def test_the_toolbar_is_hidden_with_nothing_open(self) -> None:
        """It acts on a queue of folders, and there is none - so it was a row
        of greyed-out buttons plus a second copy of the setup card's
        "Importar de fotos...". Same rule as the setup fields: gone, not grey."""
        self.app.update_idletasks()
        self.assertFalse(self.app.toolbar.winfo_manager())

    def test_opening_a_job_brings_the_toolbar_back(self) -> None:
        self.open_job()
        self.app.update_idletasks()
        self.assertTrue(self.app.toolbar.winfo_manager())

    def test_closing_hides_the_toolbar_again(self) -> None:
        self.open_job()
        self.app._close_job()
        self.app.update_idletasks()
        self.assertFalse(self.app.toolbar.winfo_manager())

    def test_there_is_exactly_one_help_button(self) -> None:
        """Two were built for a while - the glyph one and the icon one meant
        to replace it - and side by side in the brand bar they read as two
        different features."""
        found = [
            widget
            for widget in descendants(self.app)
            if isinstance(widget, ttk.Button)
            and "Ajuda" in str(widget.cget("text"))
        ]
        self.assertEqual(len(found), 1, [str(w) for w in found])

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

    def test_the_controls_are_hidden_with_no_job(self) -> None:
        """IDLE only happens with nothing open, and then not one of the six
        buttons has a runner behind it."""
        self.app.update_idletasks()
        self.assertFalse(self.app.disc_panel.buttons.winfo_manager())

    def test_the_controls_come_back_with_a_job(self) -> None:
        self.app.disc_panel.show_state(RunnerState.READY_NO_DISC)
        self.app.update_idletasks()
        self.assertTrue(self.app.disc_panel.buttons.winfo_manager())

    def test_the_progress_bar_is_hidden_until_there_is_progress(self) -> None:
        """An empty trough over two blank captions reads as "0% copied",
        not as "nothing loaded"."""
        self.app.disc_panel.show_state(RunnerState.READY_NO_DISC)
        self.app.update_idletasks()
        self.assertFalse(self.app.disc_panel.progress_block.winfo_manager())

    def test_the_progress_bar_appears_while_copying(self) -> None:
        self.app.disc_panel.show_state(RunnerState.WORKING)
        self.app.update_idletasks()
        self.assertTrue(self.app.disc_panel.progress_block.winfo_manager())

    def test_a_finished_job_keeps_its_full_bar(self) -> None:
        """JOB_COMPLETE is idle too, but the full green bar is the point."""
        self.app.disc_panel.show_state(RunnerState.JOB_COMPLETE)
        self.app.update_idletasks()
        self.assertTrue(self.app.disc_panel.progress_block.winfo_manager())

    def test_trouble_brings_the_way_out_with_it(self) -> None:
        panel = self.app.disc_panel
        panel.show_state(RunnerState.WORKING)
        panel.show_trouble("3 arquivos ilegiveis ate agora.")
        self.app.update_idletasks()
        self.assertTrue(panel.danger_row.winfo_manager())
        self.assertTrue(panel.trouble_label.winfo_manager())

    def test_leaving_the_copy_takes_the_trouble_box_away(self) -> None:
        panel = self.app.disc_panel
        panel.show_state(RunnerState.WORKING)
        panel.show_trouble("3 arquivos ilegiveis ate agora.")
        panel.show_state(RunnerState.READY_NO_DISC)
        self.app.update_idletasks()
        self.assertFalse(panel.danger_row.winfo_manager())


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

    def test_icons_folder_and_files_exist(self) -> None:
        icons_dir = theme.ASSETS / "icons"
        self.assertTrue(icons_dir.is_dir())
        expected = [
            "play", "pause", "skip", "stop", "eject", "warning",
            "add", "photo", "folder", "open", "clean", "recent",
            "close", "up", "down", "help",
        ]
        for name in expected:
            with self.subTest(icon=name):
                self.assertTrue((icons_dir / f"{name}.png").is_file())
                self.assertTrue((icons_dir / f"{name}-16.png").is_file())

    def test_load_icon(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display available for Tk: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        icon = theme.load_icon(root, "play", 16)
        self.assertIsNotNone(icon)
        self.assertEqual(icon.width(), 16)
        self.assertEqual(icon.height(), 16)

    def test_load_icon_caching_per_interpreter(self) -> None:
        try:
            first = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display available for Tk: {exc}")
        first.withdraw()
        theme.load_icon(first, "play", 16)
        first.destroy()

        second = tk.Tk()
        second.withdraw()
        self.addCleanup(second.destroy)
        icon = theme.load_icon(second, "play", 16)
        self.assertIsNotNone(icon)
        self.assertEqual(icon.width(), 16)

    def test_set_button_icon(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display available for Tk: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        btn = ttk.Button(root)
        theme.set_button_icon(btn, "play", "Iniciar")
        self.assertIn("Iniciar", str(btn.cget("text")))
        self.assertEqual(str(btn.cget("compound")), "left")

    def test_a_button_icon_greys_out_with_its_label(self) -> None:
        """The bug this guards: a disabled button kept a fully saturated icon
        beside dead grey text, so it read as half-enabled. ttk takes a
        state-keyed image list, so both are handed over at build time."""
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display available for Tk: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        btn = ttk.Button(root)
        theme.set_button_icon(btn, "play", "Iniciar")
        spec = str(btn.cget("image"))
        self.assertIn("disabled", spec, spec)

    def test_every_tone_the_ui_asks_for_is_on_disk(self) -> None:
        """A missing tone falls back to the Unicode glyph silently, which is
        exactly the kind of drift that put four different icon styles in one
        toolbar."""
        icons = theme.ASSETS / "icons"
        for name in ("play", "pause", "skip", "stop", "eject", "warning", "add",
                     "photo", "folder", "open", "clean", "recent", "close",
                     "up", "down", "help", "check", "send"):
            for tone in ("", "muted", "invert", "danger", "amber"):
                suffix = f"-{tone}" if tone else ""
                with self.subTest(icon=name, tone=tone):
                    self.assertTrue((icons / f"{name}-16{suffix}.png").is_file())

    def test_the_accent_button_gets_a_legible_icon(self) -> None:
        """Brand blue on the brand-blue accent button is an invisible icon,
        so the tone follows the style the button already has."""
        self.assertEqual(theme.TONE_FOR_STYLE["Accent.TButton"], "invert")
        self.assertEqual(theme.TONE_FOR_STYLE["Danger.TButton"], "danger")

    def test_the_checkbox_indicator_is_the_drawn_one(self) -> None:
        """clam paints a *checked* box as a dark X on grey, and an X is the
        one mark a user reads as "off" - on the toggle that decides whether a
        disc starts copying by itself."""
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display available for Tk: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        style = theme.apply_theme(root)
        self.assertIn("Brand.Checkbutton.indicator", str(style.layout("TCheckbutton")))


class MultiDriveTests(unittest.TestCase):
    """The window with two optical drives in the machine."""

    def setUp(self) -> None:
        try:
            self.app = BackupApp()
        except tk.TclError as exc:
            self.skipTest(f"no display available for Tk: {exc}")
        self.app.make_scanner = lambda: FakeScanner([], drives=("D:", "E:"))
        self.app.withdraw()
        self.addCleanup(self.app.destroy)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest = Path(self._tmp.name) / "dest"
        self.dest.mkdir()

    def open_job(self, entries=("Disco 01", "Disco 02", "Disco 03")) -> JobStore:
        store = JobStore.create(
            self.dest, "Lote", settings=JobSettings(),
            local_root=Path(self._tmp.name) / "local",
        )
        store.job.add_entries([EntryDraft(name) for name in entries])
        store.job.parent_path.mkdir(parents=True, exist_ok=True)
        store.save()
        self.app._attach(store)
        return store

    def test_one_panel_per_drive(self) -> None:
        self.open_job()
        self.assertEqual(sorted(self.app.disc_panels), ["D:", "E:"])

    def test_each_panel_is_named_after_its_drive(self) -> None:
        self.open_job()
        self.assertEqual(self.app.disc_panels["E:"].drive, "E:")

    def test_panels_go_compact_so_both_fit(self) -> None:
        self.open_job()
        self.assertTrue(all(p.compact for p in self.app.disc_panels.values()))

    def test_the_drive_column_appears(self) -> None:
        """Which drive has which folder is only a question with two."""
        self.open_job()
        shown = self.app.entries_view.tree.cget("displaycolumns")
        self.assertIn("drive", shown)

    def test_closing_the_job_returns_to_one_panel(self) -> None:
        self.open_job()
        self.app._close_job()
        self.assertEqual(len(self.app.disc_panels), 1)
        shown = self.app.entries_view.tree.cget("displaycolumns")
        self.assertNotIn("drive", shown)

    def test_an_event_paints_only_its_own_drive(self) -> None:
        """Every event carries its drive, so a copy in D: must not repaint
        the panel that belongs to E:."""
        self.open_job()
        before = self.app.disc_panels["E:"].state_var.get()
        self.app._handle(StateChanged(RunnerState.WORKING, "Copiando", drive="D:"))
        self.assertEqual(self.app.disc_panels["D:"].state_var.get(), "Copiando")
        self.assertEqual(self.app.disc_panels["E:"].state_var.get(), before)

    def test_an_event_from_a_vanished_drive_is_dropped(self) -> None:
        """The job can be closed while a copy is unwinding; its last events
        must not land on another drive's panel."""
        self.open_job()
        self.app._handle(StateChanged(RunnerState.WORKING, "Copiando", drive="Z:"))
        for drive, panel in self.app.disc_panels.items():
            with self.subTest(drive=drive):
                self.assertNotEqual(panel.state_var.get(), "Copiando")

    def test_log_lines_say_which_drive_they_came_from(self) -> None:
        self.open_job()
        self.app._handle(LogLine("Copiando", drive="E:"))
        self.assertIn("[E:]", self.app.log_view.contents())

    def test_a_command_reaches_the_drive_it_names(self) -> None:
        self.open_job()
        calls = []
        self.app.pool.runners["E:"].skip_disc = lambda: calls.append("E:")
        self.app.pool.runners["D:"].skip_disc = lambda: calls.append("D:")
        self.app._disc_command("skip_disc", "E:")
        self.assertEqual(calls, ["E:"])

    def test_each_idle_drive_is_told_what_to_expect(self) -> None:
        """The answer to "which disc goes in which drive"."""
        self.open_job()
        self.app._paint_expectations()
        first = self.app.disc_panels["D:"].expect_var.get()
        second = self.app.disc_panels["E:"].expect_var.get()
        self.assertIn("Disco 01", first)
        self.assertIn("Disco 02", second)

    def test_the_batch_is_announced_once_not_once_per_drive(self) -> None:
        self.open_job()
        self.assertFalse(self.app._completion_announced)


class StampTests(unittest.TestCase):
    """Stored timestamps are UTC; the log beside them is stamped local."""

    def test_a_stored_stamp_is_shown_in_local_time(self) -> None:
        stored = "2026-09-21T18:12:04Z"
        expected = (
            datetime(2026, 9, 21, 18, 12, 4, tzinfo=timezone.utc)
            .astimezone()
            .strftime("%d/%m %H:%M")
        )
        self.assertEqual(format_stamp(stored), expected)

    def test_an_empty_stamp_stays_empty(self) -> None:
        self.assertEqual(format_stamp(""), "")
        self.assertEqual(format_stamp(None), "")

    def test_an_unexpected_shape_is_passed_through(self) -> None:
        """A hand-edited job file should still show whatever it says rather
        than losing the column."""
        self.assertEqual(format_stamp("ontem a tarde"), "ontem a tarde")

    def test_the_pattern_is_caller_chosen(self) -> None:
        stored = "2026-09-21T18:12:04Z"
        local = datetime(2026, 9, 21, 18, 12, 4, tzinfo=timezone.utc).astimezone()
        self.assertEqual(format_stamp(stored, "%H:%M"), local.strftime("%H:%M"))


if __name__ == "__main__":
    unittest.main()
