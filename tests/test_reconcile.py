"""Reconciliation tests - the full stored-status x on-disk-state matrix.

The headline case is ``test_pending_non_empty_folder_asks_instead_of_failing``:
that exact situation is what v1 raised a fatal ValueError for, which is why an
interrupted batch could never be resumed.
"""

import tempfile
import unittest
from pathlib import Path

from backupov2.jobmodel import EntryDraft, EntryResult, EntryStatus, Job
from backupov2.reconcile import (
    FindingKind,
    Resolution,
    apply_resolution,
    reconcile,
)


class ReconcileCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def make_job(self, names=("Disco 01",)) -> Job:
        job = Job(destination_root=str(self.root), parent_name="Projeto")
        job.add_entries([EntryDraft(name) for name in names])
        return job

    def folder(self, job: Job, index: int = 0) -> Path:
        return job.parent_path / job.entries[index].folder_name

    def fill(self, folder: Path, count: int = 1) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (folder / f"file{i}.txt").write_text("x", encoding="utf-8")

    def kinds(self, report) -> list[FindingKind]:
        return [finding.kind for finding in report.findings]


class PendingTests(ReconcileCase):
    def test_missing_folder_is_created_silently(self) -> None:
        job = self.make_job()
        report = reconcile(job)
        self.assertTrue(self.folder(job).is_dir())
        self.assertEqual(report.findings, [])

    def test_existing_empty_folder_is_silent(self) -> None:
        job = self.make_job()
        self.folder(job).mkdir(parents=True)
        self.assertEqual(reconcile(job).findings, [])

    def test_pending_non_empty_folder_asks_instead_of_failing(self) -> None:
        """v1 raised a fatal ValueError here, which made resume impossible."""
        job = self.make_job()
        self.fill(self.folder(job), 3)

        report = reconcile(job)

        self.assertIn(FindingKind.FOLDER_NOT_EMPTY, self.kinds(report))
        finding = report.questions[0]
        self.assertTrue(finding.needs_decision)
        self.assertIn(Resolution.RESUME_INTO, finding.options)
        self.assertIn(Resolution.MARK_DONE, finding.options)
        self.assertIn("3 item", finding.message)
        # The entry stays usable - it is flagged, not rejected.
        self.assertEqual(job.entries[0].status, EntryStatus.PENDING)
        self.assertTrue(job.entries[0].needs_review)

    def test_no_folder_created_when_disabled(self) -> None:
        job = self.make_job()
        reconcile(job, create_missing=False)
        self.assertFalse(self.folder(job).exists())


class InterruptedTests(ReconcileCase):
    def test_in_progress_is_downgraded_to_pending(self) -> None:
        job = self.make_job()
        job.entries[0].status = EntryStatus.IN_PROGRESS
        job.entries[0].result = EntryResult(files_copied=10)

        report = reconcile(job)

        self.assertEqual(job.entries[0].status, EntryStatus.PENDING)
        self.assertTrue(job.entries[0].result.partial)
        finding = report.findings[0]
        self.assertEqual(finding.kind, FindingKind.INTERRUPTED)
        self.assertTrue(finding.applied)
        self.assertFalse(finding.needs_decision)

    def test_survives_a_missing_result(self) -> None:
        job = self.make_job()
        job.entries[0].status = EntryStatus.IN_PROGRESS
        reconcile(job)
        self.assertEqual(job.entries[0].status, EntryStatus.PENDING)


class DoneTests(ReconcileCase):
    def test_matching_counts_are_silent(self) -> None:
        job = self.make_job()
        job.entries[0].status = EntryStatus.DONE
        job.entries[0].result = EntryResult(files_copied=2)
        self.fill(self.folder(job), 2)
        self.assertEqual(reconcile(job).findings, [])

    def test_changed_count_flags_for_review_but_stays_done(self) -> None:
        job = self.make_job()
        job.entries[0].status = EntryStatus.DONE
        job.entries[0].result = EntryResult(files_copied=5)
        self.fill(self.folder(job), 2)

        report = reconcile(job)

        self.assertIn(FindingKind.DONE_CONTENTS_CHANGED, self.kinds(report))
        self.assertEqual(job.entries[0].status, EntryStatus.DONE)
        self.assertTrue(job.entries[0].needs_review)

    def test_empty_done_folder_asks_rather_than_reverting(self) -> None:
        """The folder may have been archived on purpose - never re-copy silently."""
        job = self.make_job()
        job.entries[0].status = EntryStatus.DONE
        job.entries[0].result = EntryResult(files_copied=5)
        self.folder(job).mkdir(parents=True)

        report = reconcile(job)

        finding = report.questions[0]
        self.assertEqual(finding.kind, FindingKind.DONE_FOLDER_MISSING)
        self.assertIn(Resolution.MARK_PENDING, finding.options)
        self.assertEqual(job.entries[0].status, EntryStatus.DONE)

    def test_done_without_a_recorded_count_is_silent(self) -> None:
        job = self.make_job()
        job.entries[0].status = EntryStatus.DONE
        self.fill(self.folder(job), 1)
        self.assertEqual(reconcile(job).findings, [])


class UntouchedStatusTests(ReconcileCase):
    def test_failed_and_skipped_are_left_alone(self) -> None:
        job = self.make_job(("Disco 01", "Disco 02"))
        job.entries[0].status = EntryStatus.FAILED
        job.entries[1].status = EntryStatus.SKIPPED

        report = reconcile(job)

        self.assertEqual(job.entries[0].status, EntryStatus.FAILED)
        self.assertEqual(job.entries[1].status, EntryStatus.SKIPPED)
        self.assertEqual(report.findings, [])


class DestinationTests(ReconcileCase):
    def test_unreachable_destination_reports_and_stops(self) -> None:
        job = Job(destination_root=str(self.root / "gone"), parent_name="Projeto")
        job.add_entries([EntryDraft("Disco 01")])

        report = reconcile(job)

        self.assertFalse(report.destination_reachable)
        self.assertEqual(report.findings[0].kind, FindingKind.DESTINATION_UNREACHABLE)
        # No per-entry findings: there is nothing on disk to compare against.
        self.assertEqual(len(report.findings), 1)


class ApplyResolutionTests(ReconcileCase):
    def setUp(self) -> None:
        super().setUp()
        self.job = self.make_job()
        self.fill(self.folder(self.job), 2)
        self.report = reconcile(self.job)
        self.finding = self.report.questions[0]

    def test_resume_into_clears_the_flag(self) -> None:
        apply_resolution(self.job, self.finding, Resolution.RESUME_INTO)
        self.assertEqual(self.job.entries[0].status, EntryStatus.PENDING)
        self.assertFalse(self.job.entries[0].needs_review)

    def test_mark_done_takes_it_out_of_the_queue(self) -> None:
        apply_resolution(self.job, self.finding, Resolution.MARK_DONE)
        self.assertEqual(self.job.entries[0].status, EntryStatus.DONE)
        self.assertIsNone(self.job.next_pending())

    def test_overwrite_notes_the_intent(self) -> None:
        apply_resolution(self.job, self.finding, Resolution.OVERWRITE)
        self.assertEqual(self.job.entries[0].status, EntryStatus.PENDING)
        self.assertIn("Sobrescrever", self.job.entries[0].notes)

    def test_rename_changes_the_folder_name(self) -> None:
        apply_resolution(self.job, self.finding, Resolution.RENAME, new_name="Disco 01b")
        self.assertEqual(self.job.entries[0].folder_name, "Disco 01b")

    def test_rename_without_a_name_does_nothing(self) -> None:
        apply_resolution(self.job, self.finding, Resolution.RENAME)
        self.assertEqual(self.job.entries[0].folder_name, "Disco 01")

    def test_unknown_entry_is_ignored(self) -> None:
        self.finding.entry_id = "no-such-entry"
        apply_resolution(self.job, self.finding, Resolution.MARK_DONE)
        self.assertEqual(self.job.entries[0].status, EntryStatus.PENDING)


class ResumeScenarioTests(ReconcileCase):
    def test_interrupted_batch_resumes_at_the_right_entry(self) -> None:
        """End to end: two discs done, one interrupted, two untouched."""
        job = self.make_job(("Disco 01", "Disco 02", "Disco 03", "Disco 04", "Disco 05"))
        for index in (0, 1):
            job.entries[index].status = EntryStatus.DONE
            job.entries[index].result = EntryResult(files_copied=1)
            self.fill(self.folder(job, index), 1)
        job.entries[2].status = EntryStatus.IN_PROGRESS
        self.fill(self.folder(job, 2), 1)  # partial copy left behind

        report = reconcile(job)

        self.assertEqual(job.next_pending().folder_name, "Disco 03")
        self.assertEqual(self.kinds(report), [FindingKind.INTERRUPTED])
        self.assertEqual(report.questions, [])


if __name__ == "__main__":
    unittest.main()
