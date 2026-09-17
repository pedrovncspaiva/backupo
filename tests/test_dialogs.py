"""SendToDialog: the folder picker for redirecting a disc.

Builds and drives the real Tkinter dialog (hidden, no mainloop) rather than
testing string formatting in isolation - the ordering and the entry_id
resolution through the "recent:"/"all:" iid prefixes are exactly the kind of
thing that looks right in the source and is wrong on screen.
"""

import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from backupov2.core import DEFECT_REPORT_NAME
from backupov2.jobmodel import EntryDraft, EntryStatus
from backupov2.jobstore import JobStore
from backupov2.ui.dialogs import (
    RECENT_LIMIT,
    MarkDefectiveDialog,
    SendToDialog,
)


class DialogCase(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display available for Tk: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        dest = Path(self._tmp.name) / "dest"
        dest.mkdir()
        self.store = JobStore.create(
            dest, "CAIXA 02", local_root=Path(self._tmp.name) / "local"
        )

    def build(self, count: int = 5) -> None:
        self.store.job.add_entries(
            [EntryDraft(f"Disco {i:02d}") for i in range(1, count + 1)]
        )

    def finish(self, index: int, when: str) -> None:
        entry = self.store.job.entries[index]
        entry.status = EntryStatus.DONE
        entry.finished_utc = when

    def dialog(self) -> SendToDialog:
        dlg = SendToDialog(self.root, self.store.job)
        dlg.withdraw()
        dlg.update_idletasks()
        self.addCleanup(dlg.destroy)
        return dlg

    def rows(self, dlg: SendToDialog) -> list[tuple[str, tuple]]:
        return [(iid, dlg.tree.item(iid, "values")) for iid in dlg.tree.get_children()]


class NoHistoryTests(DialogCase):
    def test_fresh_job_has_no_recent_section(self) -> None:
        self.build(3)
        dlg = self.dialog()
        iids = list(dlg.tree.get_children())
        self.assertNotIn("heading:recentes", iids)
        self.assertNotIn("heading:todas", iids)
        self.assertEqual(len(iids), 3)

    def test_first_pending_entry_is_preselected(self) -> None:
        self.build(3)
        dlg = self.dialog()
        entry = dlg._selected_entry()
        self.assertEqual(entry.folder_name, "Disco 01")


class RecentSectionTests(DialogCase):
    def test_recent_entries_sorted_most_recent_first(self) -> None:
        self.build(4)
        self.finish(0, "2026-09-16T10:00:00Z")
        self.finish(3, "2026-09-16T14:30:00Z")
        self.finish(2, "2026-09-16T12:15:00Z")
        dlg = self.dialog()

        recent_ids = [
            iid for iid in dlg.tree.get_children() if iid.startswith("recent:")
        ]
        names = [dlg.tree.set(iid, "folder") for iid in recent_ids]
        self.assertEqual(names, ["Disco 04", "Disco 03", "Disco 01"])

    def test_most_recent_entry_is_preselected(self) -> None:
        self.build(4)
        self.finish(0, "2026-09-16T10:00:00Z")
        self.finish(3, "2026-09-16T14:30:00Z")
        dlg = self.dialog()

        entry = dlg._selected_entry()
        self.assertEqual(entry.folder_name, "Disco 04")

    def test_recent_rows_are_visually_marked(self) -> None:
        self.build(2)
        self.finish(0, "2026-09-16T10:00:00Z")
        dlg = self.dialog()
        recent_id = next(
            iid for iid in dlg.tree.get_children() if iid.startswith("recent:")
        )
        self.assertIn("recent", dlg.tree.item(recent_id, "tags"))

    def test_recent_entry_still_appears_in_the_full_list_too(self) -> None:
        """Order and numbering in the main list must match the job exactly,
        so the same entry can be reached from either section."""
        self.build(3)
        self.finish(1, "2026-09-16T10:00:00Z")
        dlg = self.dialog()

        all_ids = [iid for iid in dlg.tree.get_children() if iid.startswith("all:")]
        self.assertEqual(len(all_ids), 3)
        entry_id = self.store.job.entries[1].entry_id
        self.assertIn(f"all:{entry_id}", all_ids)
        self.assertIn(f"recent:{entry_id}", dlg.tree.get_children())

    def test_limited_to_the_most_recent_few(self) -> None:
        self.build(RECENT_LIMIT + 3)
        for i in range(RECENT_LIMIT + 3):
            self.finish(i, f"2026-09-16T{10 + i:02d}:00:00Z")
        dlg = self.dialog()

        recent_ids = [
            iid for iid in dlg.tree.get_children() if iid.startswith("recent:")
        ]
        self.assertEqual(len(recent_ids), RECENT_LIMIT)

    def test_pending_entries_are_never_in_the_recent_section(self) -> None:
        self.build(3)
        dlg = self.dialog()
        recent_ids = [
            iid for iid in dlg.tree.get_children() if iid.startswith("recent:")
        ]
        self.assertEqual(recent_ids, [])


class SelectionResolutionTests(DialogCase):
    def test_heading_rows_resolve_to_no_entry(self) -> None:
        self.build(2)
        self.finish(0, "2026-09-16T10:00:00Z")
        dlg = self.dialog()
        dlg.tree.selection_set("heading:recentes")
        self.assertIsNone(dlg._selected_entry())

    def test_selecting_in_either_section_resolves_the_same_entry(self) -> None:
        self.build(2)
        self.finish(0, "2026-09-16T10:00:00Z")
        entry_id = self.store.job.entries[0].entry_id
        dlg = self.dialog()

        dlg.tree.selection_set(f"recent:{entry_id}")
        from_recent = dlg._selected_entry()
        dlg.tree.selection_set(f"all:{entry_id}")
        from_all = dlg._selected_entry()

        self.assertEqual(from_recent.entry_id, from_all.entry_id)

    def test_accept_returns_the_resolved_entry_id(self) -> None:
        self.build(2)
        dlg = self.dialog()
        dlg.tree.selection_set(f"all:{self.store.job.entries[1].entry_id}")
        dlg._accept()
        self.assertEqual(dlg.result, self.store.job.entries[1].entry_id)


class WarningTests(DialogCase):
    def test_no_warning_for_a_pending_folder(self) -> None:
        self.build(2)
        dlg = self.dialog()
        dlg.tree.selection_set(f"all:{self.store.job.entries[0].entry_id}")
        dlg._check_selection()
        self.assertEqual(dlg.warn_var.get(), "")

    def test_warns_without_threatening_deletion(self) -> None:
        """The old wording only promised existing content would be 'kept'.
        The new behaviour is stronger - nothing is ever overwritten - and the
        warning should say so plainly rather than hedge."""
        self.build(1)
        self.finish(0, "2026-09-16T10:00:00Z")
        dlg = self.dialog()
        dlg.tree.selection_set(f"all:{self.store.job.entries[0].entry_id}")
        dlg._check_selection()
        message = dlg.warn_var.get().lower()
        self.assertIn("nada sera apagado", message)
        self.assertNotIn("sobrescrit", message)


if __name__ == "__main__":
    unittest.main()


class MarkDefectiveDialogTests(DialogCase):
    """The confirm-and-explain step before a disc is written off."""

    def dialog(self, label: str = "Disco 01") -> MarkDefectiveDialog:
        dlg = MarkDefectiveDialog(self.root, label)
        dlg.withdraw()
        dlg.update_idletasks()
        self.addCleanup(dlg.destroy)
        return dlg

    def test_cancelling_returns_nothing(self) -> None:
        dlg = self.dialog()
        dlg.destroy()
        self.assertIsNone(dlg.result)

    def test_confirming_without_a_note_is_still_a_confirmation(self) -> None:
        """An empty note must not read as "cancelled" - the mark still stands."""
        dlg = self.dialog()
        dlg._accept()
        self.assertEqual(dlg.result, {"note": ""})

    def test_the_note_is_carried_back(self) -> None:
        dlg = self.dialog()
        dlg.text.insert("1.0", "  disco trincado ao meio \n")
        dlg._accept()
        self.assertEqual(dlg.result, {"note": "disco trincado ao meio"})

    def test_it_names_the_folder_and_the_file_it_will_write(self) -> None:
        dlg = self.dialog("EG 1841 > ABG-DI-8963-GI-19 R0")
        texts = [
            child.cget("text")
            for child in dlg.winfo_children()[0].winfo_children()
            if child.winfo_class() == "TLabel"
        ]
        joined = " ".join(texts)
        self.assertIn("ABG-DI-8963-GI-19 R0", joined)
        self.assertIn(DEFECT_REPORT_NAME, joined)
        # It has to promise nothing already in the folder is destroyed.
        self.assertIn("Nada que ja esteja na pasta e apagado", joined)
