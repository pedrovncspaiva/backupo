"""BackupApp: the "Fechar trabalho" affordance.

Builds and drives the real window (hidden, no mainloop) rather than testing
the handler in isolation - what matters here is the button actually flipping
back to enabled and the table actually emptying, which is exactly the kind of
thing a unit test of just the method's logic would miss.
"""

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

from backupov2.jobmodel import EntryDraft, EntryStatus, JobSettings
from backupov2.jobstore import JobStore
from backupov2.media import FakeScanner
from backupov2.runner import JobRunner, RunnerState
from backupov2.ui.app import BackupApp


class AppCase(unittest.TestCase):
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

    def build(self, name="Lote", entries=("Disco 01",), done=False) -> JobStore:
        store = JobStore.create(
            self.dest, name, settings=JobSettings(), local_root=Path(self._tmp.name) / "local"
        )
        store.job.add_entries([EntryDraft(n) for n in entries])
        if done:
            for entry in store.job.entries:
                entry.status = EntryStatus.DONE
        store.job.parent_path.mkdir(parents=True, exist_ok=True)
        store.save()
        return store


class CloseJobTests(AppCase):
    def test_criar_trabalho_starts_enabled_with_nothing_open(self) -> None:
        self.assertEqual(str(self.app.new_button["state"]), "normal")
        self.assertEqual(str(self.app.close_job_button["state"]), "disabled")

    def test_opening_a_job_disables_criar_trabalho(self) -> None:
        self.app._attach(self.build())
        self.assertEqual(str(self.app.new_button["state"]), "disabled")
        self.assertEqual(str(self.app.close_job_button["state"]), "normal")

    def test_closing_a_finished_job_re_enables_criar_trabalho(self) -> None:
        """The bug this fixes: finishing a batch left no way back to
        'Criar trabalho' short of restarting the app."""
        self.app._attach(self.build(done=True))

        self.app._close_job()

        self.assertEqual(str(self.app.new_button["state"]), "normal")
        self.assertEqual(str(self.app.close_job_button["state"]), "disabled")
        self.assertIsNone(self.app.store)
        self.assertIsNone(self.app.runner)

    def test_the_table_and_header_are_cleared(self) -> None:
        self.app._attach(self.build(name="Lote finalizado", entries=("Disco 01", "Disco 02")))

        self.app._close_job()

        self.assertEqual(self.app.entries_view.tree.get_children(), ())
        self.assertEqual(self.app.job_title_var.get(), "Nenhum trabalho aberto")
        self.assertEqual(self.app.parent_var.get(), "")

    def test_the_destination_is_kept_for_the_next_batch(self) -> None:
        """The common case is another batch to the same network share."""
        self.app._attach(self.build())
        self.app.destination_var.set("Z:/servidor/pastas")

        self.app._close_job()

        self.assertEqual(self.app.destination_var.get(), "Z:/servidor/pastas")

    def test_the_job_is_untouched_on_disk(self) -> None:
        store = self.build(name="Lote finalizado", done=True)
        self.app._attach(store)

        self.app._close_job()

        reloaded = JobStore.load(store.local_path).job
        self.assertEqual(reloaded.parent_name, "Lote finalizado")
        self.assertEqual(reloaded.entries[0].status, EntryStatus.DONE)

    def test_it_asks_before_closing_a_job_that_is_copying(self) -> None:
        store = self.build()
        self.app._attach(store)
        # Drive the real pool into a copy rather than swapping the runner
        # out: self.app.runner is now derived from the pool, so assigning to
        # it would only be testing the test.
        self.app.pool.runners["D:"]._set_state(RunnerState.WORKING, "Copiando")

        with mock.patch("backupov2.ui.app.messagebox.askyesno", return_value=False) as asked:
            self.app._close_job()
            asked.assert_called_once()

        self.assertIsNotNone(self.app.store)  # declined - stays open

    def test_confirming_while_copying_cancels_and_closes(self) -> None:
        store = self.build()
        self.app._attach(store)
        # Drive the real pool into a copy rather than swapping the runner
        # out: self.app.runner is now derived from the pool, so assigning to
        # it would only be testing the test.
        self.app.pool.runners["D:"]._set_state(RunnerState.WORKING, "Copiando")

        with mock.patch("backupov2.ui.app.messagebox.askyesno", return_value=True):
            self.app._close_job()

        self.assertIsNone(self.app.store)

    def test_closing_with_nothing_open_does_nothing(self) -> None:
        self.app._close_job()  # must not raise
        self.assertIsNone(self.app.store)

    def test_a_fresh_job_can_be_opened_right_after_closing(self) -> None:
        self.app._attach(self.build(name="Primeiro"))
        self.app._close_job()

        second = self.build(name="Segundo")
        self.app._attach(second)

        self.assertEqual(self.app.store.job.parent_name, "Segundo")
        self.assertEqual(str(self.app.new_button["state"]), "disabled")


if __name__ == "__main__":
    unittest.main()
