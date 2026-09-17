"""State-machine tests - whole disc sessions replayed without hardware.

Every collaborator is injected, so these exercise the real transitions: skip,
redirect, duplicate detection, cancellation, a pulled disc, a dead network
destination, eject failure, and close-then-resume. The clock is fake, so
countdowns are instantaneous and the tests stay fast and deterministic.
"""

import tempfile
import unittest
from pathlib import Path

from backupov2.core import (
    DEFECT_REPORT_NAME,
    CopyProgress,
    CopyResult,
    FileFailure,
    ScanResult,
)
from backupov2.errors import BackupCancelled, SourceLost
from backupov2.jobmodel import DiscKind, EntryDraft, EntryStatus
from backupov2.jobstore import JobStore
from backupov2.media import FakeScanner, KindProbe, MediaInfo
from backupov2.runner import (
    ABANDON_SECONDS,
    STALL_SECONDS,
    TROUBLE_FILE_THRESHOLD,
    CountdownTick,
    DiscFailed,
    DiscFinished,
    DiscTrouble,
    DuplicateWarning,
    Ejected,
    InlineExecutor,
    JobComplete,
    JobRunner,
    NoTarget,
    RunnerState,
    StateChanged,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeCopier:
    """Records what it was asked to copy; can be told to fail."""

    def __init__(self, files=3, total_bytes=300, error=None) -> None:
        self.files = files
        self.total_bytes = total_bytes
        self.error = error
        self.copied: list[tuple[Path, Path]] = []

    def scan(self, source: Path) -> ScanResult:
        return ScanResult(
            files=tuple((Path(f"f{i}.bin"), 100) for i in range(self.files)),
            directories=(),
            total_bytes=self.total_bytes,
        )

    def copy(self, source, destination, cancel, on_progress, options, scan=None, pause=None):
        if self.error is not None:
            raise self.error
        self.copied.append((source, destination))
        return CopyResult(
            files_copied=self.files,
            bytes_copied=self.total_bytes,
        )


class RecordingEjector:
    def __init__(self, ok=True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    def eject(self, drive: str) -> bool:
        self.calls.append(drive)
        return self.ok


def disc(label="DISC_A", serial=111, root="D:\\") -> MediaInfo:
    return MediaInfo(root=Path(root), label=label, serial=serial, fs_name="CDFS")


class RunnerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.destination = self.root / "dest"
        self.destination.mkdir()
        self.addCleanup(self._tmp.cleanup)

        self.clock = FakeClock()
        self.copier = FakeCopier()
        self.ejector = RecordingEjector()

    def build(self, names=("Disco 01", "Disco 02", "Disco 03"), script=None, kind=DiscKind.DATA):
        store = JobStore.create(
            self.destination, "Projeto", local_root=self.root / "local"
        )
        store.job.add_entries([EntryDraft(name) for name in names])
        store.save()

        self.store = store
        self.scanner = FakeScanner(script or [])
        self.runner = JobRunner(
            store=store,
            scanner=self.scanner,
            copier=self.copier,
            ejector=self.ejector,
            executor=InlineExecutor(),
            clock=self.clock,
            kind_probe=lambda root, fs: KindProbe(kind, True, "teste"),
        )
        self.runner.start()
        return self.runner

    # -- helpers ----------------------------------------------------------

    def settle(self, polls=2):
        """Feed enough polls for a disc to be accepted (settling needs two)."""
        for _ in range(polls):
            self.runner.poll()

    def run_countdown(self):
        self.clock.advance(11)
        self.runner.tick()

    def events_of(self, kind):
        return [e for e in self.runner.drain() if isinstance(e, kind)]

    def states(self):
        return [e.state for e in self.runner.drain() if isinstance(e, StateChanged)]

    def names(self):
        return [(e.folder_name, e.status) for e in self.store.job.entries]


class HappyPathTests(RunnerCase):
    def test_disc_copies_into_the_first_pending_folder(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.assertIs(self.runner.state, RunnerState.GRACE_COUNTDOWN)

        self.run_countdown()

        job = self.store.job
        self.assertEqual(job.entries[0].status, EntryStatus.DONE)
        self.assertEqual(job.entries[0].result.files_copied, 3)
        self.assertEqual(job.entries[0].media.serial, 111)
        self.assertEqual(self.copier.copied[0][1].name, "Disco 01")

    def test_settling_requires_two_sightings(self) -> None:
        """One sighting is not enough - Windows takes seconds to mount."""
        self.build(script=[[disc()]])
        self.runner.poll()
        self.assertIs(self.runner.state, RunnerState.DISC_SETTLING)
        self.runner.poll()
        self.assertIs(self.runner.state, RunnerState.GRACE_COUNTDOWN)

    def test_ejects_after_committing_the_result(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()

        self.assertEqual(self.ejector.calls, ["D:"])
        self.assertIs(self.runner.state, RunnerState.WAIT_DISC_REMOVED)
        # The result is durable before the tray ever opens.
        saved = JobStore.load(self.store.local_path).job
        self.assertEqual(saved.entries[0].status, EntryStatus.DONE)

    def test_three_discs_in_a_row(self) -> None:
        a, b, c = disc("A", 1), disc("B", 2), disc("C", 3)
        self.build(script=[[a], [a]])
        self.settle()
        self.run_countdown()

        for nxt in (b, c):
            self.scanner.script = [[], []]
            self.scanner.calls = 0
            self.settle()  # removal detected
            self.scanner.script = [[nxt], [nxt]]
            self.scanner.calls = 0
            self.settle()
            self.run_countdown()

        self.assertEqual(
            [entry.status for entry in self.store.job.entries],
            [EntryStatus.DONE] * 3,
        )
        self.assertEqual(
            [entry.media.label for entry in self.store.job.entries],
            ["A", "B", "C"],
        )

    def test_countdown_emits_ticks(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        ticks = self.events_of(CountdownTick)
        self.assertTrue(ticks)
        self.assertEqual(ticks[-1].target_name, "Disco 01")

    def test_job_completes_after_the_last_disc(self) -> None:
        self.build(names=("Disco 01",), script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()

        self.scanner.script = [[], []]
        self.scanner.calls = 0
        self.settle()

        self.assertIs(self.runner.state, RunnerState.JOB_COMPLETE)


class SkipAndRedirectTests(RunnerCase):
    def test_skip_disc_consumes_no_entry(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()

        self.runner.skip_disc()

        self.assertEqual(self.store.job.entries[0].status, EntryStatus.PENDING)
        self.assertEqual(self.store.job.next_pending().folder_name, "Disco 01")
        self.assertEqual(self.ejector.calls, ["D:"])

    def test_send_to_a_later_entry(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        third = self.store.job.entries[2]

        self.runner.send_to(third.entry_id)
        self.run_countdown()

        job = self.store.job
        self.assertEqual(job.entries[2].status, EntryStatus.DONE)
        self.assertEqual(job.entries[0].status, EntryStatus.PENDING)
        # The next disc still goes to the first untouched folder.
        self.assertEqual(job.next_pending().folder_name, "Disco 01")
        self.assertEqual(self.copier.copied[0][1].name, "Disco 03")

    def test_redirect_records_the_disc_that_was_actually_written(self) -> None:
        self.build(script=[[disc(label="OUT_OF_ORDER", serial=77)], [disc(label="OUT_OF_ORDER", serial=77)]])
        self.settle()
        self.runner.send_to(self.store.job.entries[1].entry_id)
        self.run_countdown()

        self.assertEqual(self.store.job.entries[1].media.label, "OUT_OF_ORDER")
        self.assertIsNone(self.store.job.entries[0].media)

    def test_no_pending_entry_reports_instead_of_stalling(self) -> None:
        self.build(names=("Disco 01",), script=[[disc()], [disc()]])
        for entry in self.store.job.entries:
            entry.status = EntryStatus.DONE
        self.settle()

        self.assertIs(self.runner.state, RunnerState.NO_TARGET)
        self.assertTrue(self.events_of(NoTarget))

    def test_manual_mode_waits_for_an_explicit_start(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.store.job.settings.grace_seconds = 0
        self.settle()

        self.clock.advance(120)
        self.runner.tick()
        self.assertEqual(self.store.job.entries[0].status, EntryStatus.PENDING)

        self.runner.start_now()
        self.runner.tick()  # the pump collects the worker's result
        self.assertEqual(self.store.job.entries[0].status, EntryStatus.DONE)


class DuplicateTests(RunnerCase):
    def test_same_serial_warns_instead_of_recopying(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()
        self.runner.drain()

        # The same disc comes back round.
        self.scanner.script = [[], []]
        self.scanner.calls = 0
        self.settle()
        self.clock.advance(120)
        self.scanner.script = [[disc()], [disc()]]
        self.scanner.calls = 0
        self.settle()

        self.assertIs(self.runner.state, RunnerState.DUPLICATE_WARNING)
        warning = self.events_of(DuplicateWarning)[0]
        self.assertEqual(warning.entry_name, "Disco 01")
        self.assertEqual(warning.strength, "strong")
        # Nothing was copied a second time.
        self.assertEqual(len(self.copier.copied), 1)
        self.assertEqual(self.store.job.entries[1].status, EntryStatus.PENDING)

    def test_copy_anyway_sends_it_to_the_next_pending_folder(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()
        self.scanner.script = [[], []]
        self.scanner.calls = 0
        self.settle()
        self.clock.advance(120)
        self.scanner.script = [[disc()], [disc()]]
        self.scanner.calls = 0
        self.settle()

        self.runner.copy_anyway()
        self.run_countdown()

        self.assertEqual(self.store.job.entries[1].status, EntryStatus.DONE)

    def test_a_just_finished_disc_is_not_seen_as_new(self) -> None:
        """It is still in the tray; that must not look like the next disc."""
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()

        self.scanner.script = [[disc()], [disc()]]
        self.scanner.calls = 0
        self.settle()

        self.assertIs(self.runner.state, RunnerState.WAIT_DISC_REMOVED)
        self.assertEqual(len(self.copier.copied), 1)


class FailureTests(RunnerCase):
    def test_disc_pulled_midcopy_fails_the_entry_and_holds(self) -> None:
        self.copier.error = SourceLost("Disc is no longer available")
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()

        entry = self.store.job.entries[0]
        self.assertEqual(entry.status, EntryStatus.FAILED)
        self.assertTrue(entry.result.partial)
        self.assertIs(self.runner.state, RunnerState.ERROR_HOLD)
        self.assertTrue(self.events_of(DiscFailed))

    def test_dead_network_destination_is_classified(self) -> None:
        self.copier.error = OSError(0, "network name no longer available", None, 64)
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()

        failure = self.events_of(DiscFailed)[0]
        self.assertEqual(failure.error_class, "transient_dest")
        self.assertEqual(self.store.job.entries[0].status, EntryStatus.FAILED)

    def test_failed_entry_does_not_block_the_next_disc(self) -> None:
        self.copier.error = SourceLost("gone")
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()

        self.assertEqual(self.store.job.next_pending().folder_name, "Disco 02")

    def test_cancel_returns_the_entry_to_pending(self) -> None:
        self.copier.error = BackupCancelled()
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()

        entry = self.store.job.entries[0]
        self.assertEqual(entry.status, EntryStatus.PENDING)
        self.assertTrue(entry.result.partial)
        self.assertEqual(self.ejector.calls, [])  # nothing ejected on cancel

    def test_retry_clears_a_failure(self) -> None:
        self.copier.error = SourceLost("gone")
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()
        entry = self.store.job.entries[0]

        self.runner.retry_entry(entry.entry_id)

        self.assertEqual(entry.status, EntryStatus.PENDING)
        self.assertIsNone(entry.error)

    def test_eject_failure_still_waits_for_removal(self) -> None:
        """Eject is advisory - a USB tray that ignores us must not end the batch."""
        self.ejector.ok = False
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()

        self.assertIs(self.runner.state, RunnerState.WAIT_DISC_REMOVED)
        self.assertFalse(self.events_of(Ejected)[0].ok)
        self.assertEqual(self.store.job.entries[0].status, EntryStatus.DONE)

    def test_eject_raising_does_not_break_the_run(self) -> None:
        class Exploding:
            def eject(self, drive):
                raise OSError("device gone")

        self.ejector = Exploding()
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()

        self.assertEqual(self.store.job.entries[0].status, EntryStatus.DONE)
        self.assertIs(self.runner.state, RunnerState.WAIT_DISC_REMOVED)

    def test_partial_failures_mark_the_entry_for_review(self) -> None:
        class PartialCopier(FakeCopier):
            def copy(self, source, destination, cancel, on_progress, options, scan=None, pause=None):
                return CopyResult(files_copied=2, bytes_copied=200, files_failed=1)

        self.copier = PartialCopier()
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()

        entry = self.store.job.entries[0]
        self.assertEqual(entry.status, EntryStatus.DONE)
        self.assertTrue(entry.needs_review)
        self.assertEqual(self.events_of(DiscFinished)[0].files_failed, 1)


class AudioTests(RunnerCase):
    def test_audio_disc_without_ffmpeg_is_skipped_not_fatal(self) -> None:
        self.build(script=[[disc()], [disc()]], kind=DiscKind.AUDIO)
        self.settle()
        self.run_countdown()

        entry = self.store.job.entries[0]
        self.assertEqual(entry.status, EntryStatus.SKIPPED)
        self.assertEqual(entry.error, "audio_unsupported")
        self.assertTrue(entry.needs_review)
        # The batch carries on rather than stopping.
        self.assertIsNot(self.runner.state, RunnerState.ERROR_HOLD)


class SendToTimingTests(RunnerCase):
    """send_to() must never leave a window in which the auto-detected disc
    can start copying somewhere the user did not choose - this is what a
    modal picker dialog staying open depends on."""

    def test_send_to_starts_immediately_no_countdown(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        third = self.store.job.entries[2]

        self.runner.send_to(third.entry_id)

        self.assertEqual(self.store.job.entries[2].status, EntryStatus.DONE)

    def test_pausing_during_the_countdown_prevents_the_default_target(self) -> None:
        """Simulates a picker dialog staying open past the grace period."""
        self.build(script=[[disc()], [disc()]])
        self.settle()

        self.runner.pause()
        for _ in range(50):
            self.clock.advance(1.0)
            self.runner.tick()

        self.assertEqual(self.store.job.entries[0].status, EntryStatus.PENDING)
        self.assertIs(self.runner.state, RunnerState.PAUSED)

    def test_choosing_a_target_after_a_long_pause_hits_only_that_target(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.runner.pause()
        for _ in range(50):
            self.clock.advance(1.0)
            self.runner.tick()

        third = self.store.job.entries[2]
        self.runner.send_to(third.entry_id)

        self.assertEqual(self.store.job.entries[0].status, EntryStatus.PENDING)
        self.assertEqual(self.store.job.entries[2].status, EntryStatus.DONE)

    def test_resume_restores_no_target_not_ready_no_disc(self) -> None:
        """A resume() that forgets NO_TARGET loses the disc that is still in
        the drive, silently going back to 'waiting' as if it were empty."""
        self.build(names=("Disco 01",), script=[[], []])
        self.store.job.entries[0].status = EntryStatus.DONE
        self.scanner.script = [[disc()], [disc()]]
        self.scanner.calls = 0
        self.settle()
        self.assertIs(self.runner.state, RunnerState.NO_TARGET)

        self.runner.pause()
        self.runner.resume()

        self.assertIs(self.runner.state, RunnerState.NO_TARGET)

    def test_resume_restores_duplicate_warning(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()
        self.scanner.script = [[], []]
        self.scanner.calls = 0
        self.settle()
        self.clock.advance(120)
        self.scanner.script = [[disc()], [disc()]]
        self.scanner.calls = 0
        self.settle()
        self.assertIs(self.runner.state, RunnerState.DUPLICATE_WARNING)

        self.runner.pause()
        self.runner.resume()

        self.assertIs(self.runner.state, RunnerState.DUPLICATE_WARNING)

    def test_resume_after_disc_removed_goes_to_ready(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.runner.pause()
        self.runner.current_media = None  # the disc was taken out while paused

        self.runner.resume()

        self.assertIs(self.runner.state, RunnerState.READY_NO_DISC)


class PauseTests(RunnerCase):
    def test_pause_stops_the_countdown(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.runner.pause()

        self.clock.advance(120)
        self.runner.tick()

        self.assertEqual(self.store.job.entries[0].status, EntryStatus.PENDING)
        self.assertIs(self.runner.state, RunnerState.PAUSED)

    def test_resume_restarts_the_countdown(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.runner.pause()
        self.runner.resume()
        self.assertIs(self.runner.state, RunnerState.GRACE_COUNTDOWN)
        self.run_countdown()
        self.assertEqual(self.store.job.entries[0].status, EntryStatus.DONE)


class ResumeSessionTests(RunnerCase):
    def test_close_and_reopen_continues_at_the_right_entry(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()

        reopened = JobStore.load(self.store.local_path).job

        self.assertEqual(reopened.entries[0].status, EntryStatus.DONE)
        self.assertEqual(reopened.next_pending().folder_name, "Disco 02")
        self.assertEqual(reopened.entries[0].media.label, "DISC_A")

    def test_edits_during_a_run_are_persisted(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()

        extra = self.store.add_entries([EntryDraft("Disco extra")], at_index=1)[0]
        reopened = JobStore.load(self.store.local_path).job

        self.assertEqual(reopened.entries[1].entry_id, extra.entry_id)
        self.assertEqual(reopened.next_pending().folder_name, "Disco extra")


class CollectingFolderTests(RunnerCase):
    """One folder flagged to swallow every disc until it is flagged off.

    The list order is untouched throughout - the flag is the only thing
    redirecting discs, so turning it off must put everything back exactly as
    it was.
    """

    def feed(self, media) -> None:
        """A whole cycle: previous disc removed, this one inserted and copied."""
        self.scanner.script = [[], []]
        self.scanner.calls = 0
        self.settle()
        self.scanner.script = [[media], [media]]
        self.scanner.calls = 0
        self.settle()
        self.run_countdown()

    def test_every_disc_lands_in_the_collecting_folder(self) -> None:
        self.build(script=[[]])
        target = self.store.job.entries[1]
        self.runner.set_collecting(target.entry_id, True)

        for media in (disc("A", 1), disc("B", 2), disc("C", 3)):
            self.feed(media)

        self.assertEqual(
            [dest.name for _src, dest in self.copier.copied],
            ["Disco 02"] * 3,
        )
        self.assertEqual(
            [entry.status for entry in self.store.job.entries],
            [EntryStatus.PENDING, EntryStatus.DONE, EntryStatus.PENDING],
        )

    def test_the_folder_totals_every_disc_it_received(self) -> None:
        self.build(script=[[]])
        target = self.store.job.entries[0]
        self.runner.set_collecting(target.entry_id, True)

        for media in (disc("A", 1), disc("B", 2)):
            self.feed(media)

        self.assertEqual(target.disc_count, 2)
        self.assertEqual(target.result.files_copied, 6)  # 3 per disc
        self.assertEqual(target.result.bytes_copied, 600)
        self.assertEqual([d.media.label for d in target.collected], ["A", "B"])
        self.assertEqual([d.files_copied for d in target.collected], [3, 3])

    def test_the_job_never_declares_itself_finished_while_collecting(self) -> None:
        self.build(names=("Unica",), script=[[]])
        target = self.store.job.entries[0]
        self.runner.set_collecting(target.entry_id, True)

        self.feed(disc("A", 1))

        self.assertFalse(self.store.job.is_complete)
        self.assertIsNot(self.runner.state, RunnerState.JOB_COMPLETE)
        self.assertEqual(self.events_of(JobComplete), [])

    def test_turning_it_off_hands_the_next_disc_back_to_the_list(self) -> None:
        self.build(script=[[]])
        target = self.store.job.entries[1]
        self.runner.set_collecting(target.entry_id, True)
        self.feed(disc("A", 1))

        self.runner.set_collecting(target.entry_id, False)
        self.feed(disc("B", 2))

        self.assertEqual(
            [dest.name for _src, dest in self.copier.copied],
            ["Disco 02", "Disco 01"],
        )

    def test_turning_it_on_re_aims_the_disc_already_in_the_drive(self) -> None:
        """Flipping the switch mid-countdown has to apply to the disc you are
        looking at, not only to the one after it."""
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.assertIs(self.runner.state, RunnerState.GRACE_COUNTDOWN)
        self.assertEqual(self.runner.current_entry_id, self.store.job.entries[0].entry_id)

        target = self.store.job.entries[2]
        self.runner.set_collecting(target.entry_id, True)
        self.assertEqual(self.runner.current_entry_id, target.entry_id)

        self.run_countdown()
        self.assertEqual(self.copier.copied[0][1].name, "Disco 03")

    def test_a_copy_under_way_keeps_its_own_target(self) -> None:
        """The flag is about where the *next* disc goes; it must never move a
        destination out from under a copy that is already running."""
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.runner._set_state(RunnerState.WORKING, "teste")

        self.runner.set_collecting(self.store.job.entries[2].entry_id, True)

        self.assertEqual(
            self.runner.current_entry_id, self.store.job.entries[0].entry_id
        )

    def test_reinserting_an_earlier_disc_still_warns(self) -> None:
        self.build(script=[[]])
        target = self.store.job.entries[0]
        self.runner.set_collecting(target.entry_id, True)
        self.feed(disc("A", 1))
        self.feed(disc("B", 2))

        self.scanner.script = [[], []]
        self.scanner.calls = 0
        self.settle()
        again = disc("A", 1)
        self.scanner.script = [[again], [again]]
        self.scanner.calls = 0
        self.settle()

        self.assertIs(self.runner.state, RunnerState.DUPLICATE_WARNING)
        warnings = self.events_of(DuplicateWarning)
        self.assertEqual(warnings[-1].entry_id, target.entry_id)

    def test_copy_anyway_after_that_warning_still_targets_the_folder(self) -> None:
        self.build(script=[[]])
        target = self.store.job.entries[0]
        self.runner.set_collecting(target.entry_id, True)
        self.feed(disc("A", 1))

        self.scanner.script = [[], []]
        self.scanner.calls = 0
        self.settle()
        again = disc("A", 1)
        self.scanner.script = [[again], [again]]
        self.scanner.calls = 0
        self.settle()
        self.runner.copy_anyway()
        self.run_countdown()

        self.assertEqual(
            [dest.name for _src, dest in self.copier.copied], ["Disco 01"] * 2
        )

    def test_renames_in_a_collecting_folder_are_not_flagged_for_review(self) -> None:
        """Pooled discs share filenames as a matter of course, so a rename is
        the mechanism working - flagging it would make the warning meaningless."""
        self.build(script=[[]])
        target = self.store.job.entries[0]
        self.runner.set_collecting(target.entry_id, True)

        self.copier.copy = lambda *a, **k: CopyResult(
            files_copied=3, bytes_copied=300, files_renamed=2
        )
        self.feed(disc("A", 1))

        self.assertEqual(target.result.files_renamed, 2)
        self.assertFalse(target.needs_review)

    def test_a_read_failure_in_a_collecting_folder_is_still_flagged(self) -> None:
        self.build(script=[[]])
        target = self.store.job.entries[0]
        self.runner.set_collecting(target.entry_id, True)

        self.copier.copy = lambda *a, **k: CopyResult(
            files_copied=2, bytes_copied=200, files_failed=1
        )
        self.feed(disc("A", 1))

        self.assertTrue(target.needs_review)

    def test_the_flag_survives_closing_and_reopening(self) -> None:
        self.build(script=[[]])
        target = self.store.job.entries[1]
        self.runner.set_collecting(target.entry_id, True)
        self.feed(disc("A", 1))

        reopened = JobStore.load(self.store.local_path).job

        self.assertTrue(reopened.entries[1].collecting)
        self.assertEqual(reopened.next_pending().folder_name, "Disco 02")
        self.assertEqual(reopened.entries[1].disc_count, 1)


class InterruptibleCopier(FakeCopier):
    """A copier that hands control back mid-transfer.

    Stands in for the point inside a real copy where the worker checks whether
    it has been told to stop or hold, so skip/pause/cancel can be exercised
    against a copy that is genuinely in progress, with the inline executor.
    """

    def __init__(self, during=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.during = during
        self.saw_pause_event = None

    def copy(self, source, destination, cancel, on_progress, options, scan=None, pause=None):
        self.saw_pause_event = pause
        if self.during is not None:
            self.during()
        if cancel.is_set():
            raise BackupCancelled
        return super().copy(source, destination, cancel, on_progress, options, scan)


class DuringCopyTests(RunnerCase):
    """Skip, pause and cancel while a transfer is actually running."""

    def start_copy(self, during, names=("Disco 01", "Disco 02")):
        self.copier = InterruptibleCopier(during=during)
        self.build(names=names, script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()

    # -- skip -------------------------------------------------------------

    def test_skip_stops_the_transfer_and_ejects(self) -> None:
        self.start_copy(during=lambda: self.runner.skip_disc())

        self.assertEqual(self.ejector.calls, ["D:"])
        self.assertIs(self.runner.state, RunnerState.WAIT_DISC_REMOVED)

    def test_skip_leaves_the_folder_pending_and_consumes_no_entry(self) -> None:
        self.start_copy(during=lambda: self.runner.skip_disc())

        entry = self.store.job.entries[0]
        self.assertEqual(entry.status, EntryStatus.PENDING)
        self.assertTrue(entry.result.partial)
        self.assertEqual(self.store.job.next_pending().folder_name, "Disco 01")

    def test_a_disc_skipped_mid_copy_can_simply_be_reinserted(self) -> None:
        self.start_copy(during=lambda: self.runner.skip_disc())

        self.copier.during = None  # this time let it run to the end
        self.scanner.script = [[], []]
        self.scanner.calls = 0
        self.settle()
        again = disc("DISC_A", 111)
        self.scanner.script = [[again], [again]]
        self.scanner.calls = 0
        self.settle()
        self.run_countdown()

        self.assertEqual(self.store.job.entries[0].status, EntryStatus.DONE)

    # -- cancel -----------------------------------------------------------

    def test_cancel_stops_the_transfer_but_keeps_the_disc(self) -> None:
        """The only difference from skip: the tray stays shut."""
        self.start_copy(during=lambda: self.runner.cancel())

        self.assertEqual(self.ejector.calls, [])
        self.assertIs(self.runner.state, RunnerState.READY_NO_DISC)
        self.assertEqual(self.store.job.entries[0].status, EntryStatus.PENDING)

    # -- pause ------------------------------------------------------------

    def test_pause_holds_the_copy_without_leaving_the_working_state(self) -> None:
        """The entry is still mid-write, so everything asking whether this
        folder is busy has to keep getting yes."""
        seen = {}

        def during():
            self.runner.pause()
            seen["state"] = self.runner.state
            seen["paused"] = self.runner.is_paused
            seen["busy"] = self.runner.is_busy
            seen["event"] = self.runner.pause_event.is_set()
            self.runner.resume()
            seen["after"] = self.runner.is_paused

        self.start_copy(during=during)

        self.assertIs(seen["state"], RunnerState.WORKING)
        self.assertTrue(seen["paused"])
        self.assertTrue(seen["busy"])
        self.assertTrue(seen["event"])
        self.assertFalse(seen["after"])

    def test_the_copier_is_handed_the_pause_event(self) -> None:
        self.start_copy(during=None)
        self.assertIs(self.copier.saw_pause_event, self.runner.pause_event)

    def test_a_pause_does_not_count_against_the_transfer_rate(self) -> None:
        """Speed and ETA come from elapsed time; a two-minute hold is not the
        network getting slower."""
        marks = {}

        def during():
            marks["before"] = self.runner._work.started
            self.runner.pause()
            self.clock.advance(120)
            self.runner.resume()
            marks["after"] = self.runner._work.started

        self.start_copy(during=during)

        self.assertAlmostEqual(marks["after"] - marks["before"], 120)

    def test_resuming_a_copy_that_is_not_paused_does_nothing(self) -> None:
        seen = {}

        def during():
            self.runner.resume()
            seen["state"] = self.runner.state
            seen["paused"] = self.runner.is_paused

        self.start_copy(during=during)

        self.assertIs(seen["state"], RunnerState.WORKING)
        self.assertFalse(seen["paused"])

    # -- stopping a held copy ---------------------------------------------

    def test_skipping_a_paused_copy_releases_it_so_it_can_stop(self) -> None:
        """A worker asleep on the pause has to be woken, or the skip would
        wait for a resume that is never coming."""
        seen = {}

        def during():
            self.runner.pause()
            self.runner.skip_disc()
            seen["paused"] = self.runner.pause_event.is_set()
            seen["cancelled"] = self.runner.cancel_event.is_set()

        self.start_copy(during=during)

        self.assertFalse(seen["paused"])
        self.assertTrue(seen["cancelled"])
        self.assertEqual(self.ejector.calls, ["D:"])

    def test_cancelling_a_paused_copy_releases_it_too(self) -> None:
        seen = {}

        def during():
            self.runner.pause()
            self.runner.cancel()
            seen["paused"] = self.runner.pause_event.is_set()

        self.start_copy(during=during)

        self.assertFalse(seen["paused"])
        self.assertIs(self.runner.state, RunnerState.READY_NO_DISC)

    def test_pausing_outside_a_copy_still_uses_the_paused_state(self) -> None:
        """The two kinds of pause must not have blurred into one another."""
        self.build(script=[[disc()], [disc()]])
        self.settle()

        self.runner.pause()

        self.assertIs(self.runner.state, RunnerState.PAUSED)
        self.assertTrue(self.runner.is_paused)
        self.assertFalse(self.runner.pause_event.is_set())


class FlakyCopier(FakeCopier):
    """Reports read failures on the heartbeat, the way the real engine does."""

    def __init__(self, failures=0, during=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.failures = failures
        self.during = during

    def copy(self, source, destination, cancel, on_progress, options, scan=None, pause=None):
        for number in range(1, self.failures + 1):
            on_progress(
                CopyProgress(
                    copied_bytes=number * 10,
                    total_bytes=self.total_bytes,
                    current_file=f"ruim{number}.bin",
                    files_copied=2,
                    files_failed=number,
                )
            )
        if self.during is not None:
            self.during()
        if cancel.is_set():
            raise BackupCancelled(
                CopyResult(
                    files_copied=2,
                    bytes_copied=200,
                    files_failed=self.failures,
                    failed_files=[
                        FileFailure(f"ruim{n}.bin", "[WinError 23] CRC", "source_media", 23)
                        for n in range(1, self.failures + 1)
                    ],
                    partial=True,
                )
            )
        self.copied.append((source, destination))
        return CopyResult(files_copied=self.files, bytes_copied=self.total_bytes)


class NeverExecutor:
    """Accepts work and never runs it - a drive that has stopped answering."""

    def __init__(self) -> None:
        self.submitted = []

    def submit(self, work) -> None:
        self.submitted.append(work)


class CorruptDiscTests(RunnerCase):
    """Writing off a disc that will not read, instead of grinding on it."""

    def start_copy(self, copier, names=("Disco 01", "Disco 02")):
        self.copier = copier
        self.build(names=names, script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()

    def report_for(self, index=0) -> Path:
        entry = self.store.job.entries[index]
        return self.store.job.folder_for(entry) / DEFECT_REPORT_NAME

    def start_frozen_copy(self, names=("Disco 01", "Disco 02")):
        """A copy that has begun and will never report back."""
        self.executor = NeverExecutor()
        self.copier = FakeCopier()
        self.build(names=names, script=[[disc()], [disc()]])
        self.runner.executor = self.executor
        self.settle()
        self.run_countdown()
        self.runner.drain()

    # -- when the offer appears -------------------------------------------

    def test_a_single_unreadable_file_stays_quiet(self) -> None:
        """One bad file is handled by skipping it - that is not a bad disc,
        and offering to write the disc off would be noise."""
        self.start_copy(FlakyCopier(failures=1))
        self.assertEqual(self.events_of(DiscTrouble), [])

    def test_several_unreadable_files_offer_a_way_out(self) -> None:
        self.start_copy(FlakyCopier(failures=TROUBLE_FILE_THRESHOLD))

        trouble = self.events_of(DiscTrouble)
        self.assertEqual(len(trouble), 1)
        self.assertEqual(trouble[0].files_failed, TROUBLE_FILE_THRESHOLD)
        self.assertFalse(trouble[0].stalled)
        self.assertEqual(trouble[0].entry_id, self.store.job.entries[0].entry_id)

    def test_the_offer_is_made_only_once_per_disc(self) -> None:
        self.start_copy(FlakyCopier(failures=TROUBLE_FILE_THRESHOLD + 5))
        self.assertEqual(len(self.events_of(DiscTrouble)), 1)

    def test_a_drive_that_goes_quiet_also_offers_a_way_out(self) -> None:
        """The other face of a bad disc: no errors, no progress, forever."""
        self.start_frozen_copy()
        self.assertIs(self.runner.state, RunnerState.WORKING)

        self.clock.advance(STALL_SECONDS + 1)
        self.runner.tick()

        trouble = self.events_of(DiscTrouble)
        self.assertEqual(len(trouble), 1)
        self.assertTrue(trouble[0].stalled)

    # -- what marking it does ---------------------------------------------

    def test_marking_it_fails_the_folder_and_ejects(self) -> None:
        self.start_copy(
            FlakyCopier(failures=4, during=lambda: self.runner.mark_corrupted())
        )

        entry = self.store.job.entries[0]
        self.assertEqual(entry.status, EntryStatus.FAILED)
        self.assertEqual(entry.error, "disc_corrupted")
        self.assertTrue(entry.needs_review)
        self.assertTrue(entry.result.partial)
        self.assertEqual(self.ejector.calls, ["D:"])

    def test_it_keeps_what_was_copied_and_lists_what_was_not(self) -> None:
        self.start_copy(
            FlakyCopier(failures=4, during=lambda: self.runner.mark_corrupted())
        )

        entry = self.store.job.entries[0]
        self.assertEqual(entry.result.files_copied, 2)
        self.assertEqual(entry.result.files_failed, 4)
        self.assertEqual(len(entry.result.failed_files), 4)

    def test_a_report_is_written_into_the_disc_folder(self) -> None:
        self.start_copy(
            FlakyCopier(failures=4, during=lambda: self.runner.mark_corrupted())
        )

        report = self.report_for()
        self.assertTrue(report.is_file())
        text = report.read_text(encoding="utf-8")
        self.assertIn("DISCO COM DEFEITO", text)
        self.assertIn("INCOMPLETO", text)
        self.assertIn("ruim1.bin", text)
        self.assertIn("ruim4.bin", text)
        self.assertIn("DISC_A", text)

    def test_the_next_disc_goes_to_the_next_folder(self) -> None:
        """A written-off folder is finished with, not retried by accident."""
        self.start_copy(
            FlakyCopier(failures=4, during=lambda: self.runner.mark_corrupted())
        )

        self.assertEqual(self.store.job.next_pending().folder_name, "Disco 02")

    def test_marking_outside_a_copy_does_nothing(self) -> None:
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.assertIs(self.runner.state, RunnerState.GRACE_COUNTDOWN)

        self.runner.mark_corrupted()

        self.assertIs(self.runner.state, RunnerState.GRACE_COUNTDOWN)
        self.assertEqual(self.store.job.entries[0].status, EntryStatus.PENDING)

    def test_the_verdict_survives_closing_and_reopening(self) -> None:
        self.start_copy(
            FlakyCopier(failures=4, during=lambda: self.runner.mark_corrupted())
        )

        reopened = JobStore.load(self.store.local_path).job

        self.assertEqual(reopened.entries[0].status, EntryStatus.FAILED)
        self.assertEqual(reopened.entries[0].error, "disc_corrupted")
        self.assertEqual(len(reopened.entries[0].result.failed_files), 4)

    # -- the frozen drive --------------------------------------------------

    def test_a_worker_that_never_returns_is_given_up_on(self) -> None:
        """A read wedged in the kernel never comes back; the app must not wait
        on it forever."""
        self.start_frozen_copy()

        self.runner.mark_corrupted()
        self.assertIs(self.runner.state, RunnerState.WORKING)  # still unwinding

        self.clock.advance(ABANDON_SECONDS + 1)
        self.runner.tick()

        self.assertEqual(self.store.job.entries[0].status, EntryStatus.FAILED)
        self.assertEqual(self.ejector.calls, ["D:"])
        self.assertTrue(self.report_for().is_file())

    def test_the_report_says_so_when_the_drive_never_answered(self) -> None:
        self.start_frozen_copy()
        self.runner.mark_corrupted()
        self.clock.advance(ABANDON_SECONDS + 1)
        self.runner.tick()

        text = self.report_for().read_text(encoding="utf-8")
        self.assertIn("parou de responder", text)

    def test_a_late_answer_from_an_abandoned_worker_is_ignored(self) -> None:
        """Otherwise a drive that woke up an hour later would reopen a folder
        the user had already moved past."""
        self.start_frozen_copy()
        work = self.runner._work  # hold on to it before it is written off
        self.runner.mark_corrupted()
        self.clock.advance(ABANDON_SECONDS + 1)
        self.runner.tick()
        self.assertEqual(self.store.job.entries[0].status, EntryStatus.FAILED)

        # The thread finally comes back with a successful result.
        work.result = CopyResult(files_copied=3, bytes_copied=300)
        work.error = None
        self.runner._results.put(work)
        self.runner.tick()

        self.assertEqual(self.store.job.entries[0].status, EntryStatus.FAILED)
        self.assertEqual(self.ejector.calls, ["D:"])


class MarkEntryDefectiveTests(RunnerCase):
    """Writing a disc off from the list, without ever reading it."""

    def report_for(self, index=0) -> Path:
        entry = self.store.job.entries[index]
        return self.store.job.folder_for(entry) / DEFECT_REPORT_NAME

    def test_it_marks_the_folder_skipped_not_failed(self) -> None:
        """It was never tried and found wanting - it was written off."""
        self.build(script=[[]])
        entry = self.store.job.entries[0]

        self.assertTrue(self.runner.mark_entry_defective(entry.entry_id))

        self.assertEqual(entry.status, EntryStatus.SKIPPED)
        self.assertEqual(entry.error, "disc_corrupted")
        self.assertTrue(entry.needs_review)
        self.assertTrue(entry.finished_utc)

    def test_it_writes_the_report_into_the_folder(self) -> None:
        self.build(script=[[]])
        self.runner.mark_entry_defective(self.store.job.entries[0].entry_id)

        text = self.report_for().read_text(encoding="utf-8")
        self.assertIn("PULADA", text)
        self.assertIn("NENHUM ARQUIVO FOI COPIADO", text)
        self.assertIn("nao chegou a ser lido", text)

    def test_the_note_reaches_both_the_report_and_the_job(self) -> None:
        self.build(script=[[]])
        entry = self.store.job.entries[0]

        self.runner.mark_entry_defective(entry.entry_id, "disco trincado ao meio")

        self.assertEqual(entry.notes, "disco trincado ao meio")
        self.assertIn("disco trincado ao meio", self.report_for().read_text("utf-8"))

    def test_it_creates_the_folder_when_none_exists_yet(self) -> None:
        """A folder is only made when a disc arrives, and this disc never will."""
        self.build(script=[[]])
        entry = self.store.job.entries[0]
        self.assertFalse(self.store.job.folder_for(entry).exists())

        self.runner.mark_entry_defective(entry.entry_id)

        self.assertTrue(self.report_for().is_file())

    def test_the_next_disc_goes_to_the_following_folder(self) -> None:
        self.build(script=[[]])
        self.runner.mark_entry_defective(self.store.job.entries[0].entry_id)

        self.assertEqual(self.store.job.next_pending().folder_name, "Disco 02")

    def test_it_refuses_while_that_folder_is_being_copied(self) -> None:
        """There is a transfer to stop first, and mark_corrupted is what does
        that - silently marking here would leave a copy writing into a folder
        already declared dead."""
        self.build(script=[[disc()], [disc()]])
        self.settle()
        entry = self.store.job.entries[0]
        entry.status = EntryStatus.IN_PROGRESS

        self.assertFalse(self.runner.mark_entry_defective(entry.entry_id))
        self.assertEqual(entry.status, EntryStatus.IN_PROGRESS)
        self.assertFalse(self.report_for().exists())

    def test_it_keeps_whatever_a_previous_attempt_had_copied(self) -> None:
        """A part-copied folder written off later still says how much is there."""
        self.build(script=[[disc()], [disc()]])
        self.settle()
        self.run_countdown()
        entry = self.store.job.entries[0]
        self.assertEqual(entry.result.files_copied, 3)

        self.runner.mark_entry_defective(entry.entry_id, "resto do disco ilegivel")

        text = self.report_for().read_text(encoding="utf-8")
        self.assertIn("INCOMPLETO", text)
        self.assertIn("Arquivos copiados com sucesso: 3", text)

    def test_an_unknown_entry_is_a_no_op(self) -> None:
        self.build(script=[[]])
        self.assertFalse(self.runner.mark_entry_defective("nao-existe"))

    def test_the_verdict_survives_closing_and_reopening(self) -> None:
        self.build(script=[[]])
        entry = self.store.job.entries[0]
        self.runner.mark_entry_defective(entry.entry_id, "disco quebrado")

        reopened = JobStore.load(self.store.local_path).job

        self.assertEqual(reopened.entries[0].status, EntryStatus.SKIPPED)
        self.assertEqual(reopened.entries[0].error, "disc_corrupted")
        self.assertEqual(reopened.entries[0].notes, "disco quebrado")

    def test_a_finished_job_reopens_when_one_is_marked_pending_again(self) -> None:
        self.build(names=("Unica",), script=[[]])
        entry = self.store.job.entries[0]
        self.runner.mark_entry_defective(entry.entry_id)
        self.assertTrue(self.store.job.is_complete)

        self.runner.retry_entry(entry.entry_id)

        self.assertEqual(entry.status, EntryStatus.PENDING)
        self.assertIsNone(entry.error)
        self.assertIs(self.runner.state, RunnerState.READY_NO_DISC)

    def test_a_successful_copy_later_removes_the_stale_report(self) -> None:
        """Otherwise the folder would contradict itself: full of files, with a
        note inside saying nothing could be copied."""
        self.build(script=[[disc()], [disc()]])
        entry = self.store.job.entries[0]
        self.runner.mark_entry_defective(entry.entry_id, "leitor nao reconhece")
        self.assertTrue(self.report_for().is_file())

        self.runner.retry_entry(entry.entry_id)
        self.settle()
        self.run_countdown()

        self.assertEqual(entry.status, EntryStatus.DONE)
        self.assertFalse(self.report_for().exists())


if __name__ == "__main__":
    unittest.main()
