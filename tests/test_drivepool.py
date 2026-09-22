"""Two drives over one job, replayed without hardware.

The thing that has to be true and is not obvious: ``Job.next_pending`` is
derived from the list rather than stored, so two drives asked in the same
moment are handed the same folder unless something stops them. These tests
exist mostly to pin that down, and to pin down the advisory "put the next one
in E:" the operator reads off the panels.
"""

import tempfile
import unittest
from pathlib import Path

from backupov2.core import CopyResult, ScanResult
from backupov2.jobmodel import DiscKind, EntryDraft, EntryStatus
from backupov2.jobstore import JobStore
from backupov2.media import FakeScanner, KindProbe, MediaInfo
from backupov2.runner import (
    ClaimRegistry,
    DrivePool,
    InlineExecutor,
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
    def __init__(self, files=3, total_bytes=300) -> None:
        self.files = files
        self.total_bytes = total_bytes
        self.copied: list[tuple[Path, Path]] = []

    def scan(self, source: Path) -> ScanResult:
        return ScanResult(
            files=tuple((Path(f"f{i}.bin"), 100) for i in range(self.files)),
            directories=(),
            total_bytes=self.total_bytes,
        )

    def copy(self, source, destination, cancel, on_progress, options, scan=None,
             pause=None):
        self.copied.append((source, destination))
        return CopyResult(files_copied=self.files, bytes_copied=self.total_bytes)


class RecordingEjector:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def eject(self, drive: str) -> bool:
        self.calls.append(drive)
        return True


def disc(label="DISC", serial=1, root="D:\\") -> MediaInfo:
    return MediaInfo(root=Path(root), label=label, serial=serial, fs_name="CDFS")


class PoolCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.destination = self.root / "dest"
        self.destination.mkdir()
        self.clock = FakeClock()
        self.copier = FakeCopier()
        self.ejector = RecordingEjector()

    def build(self, names=("Disco 01", "Disco 02", "Disco 03", "Disco 04"),
              script=None, drives=("D:", "E:")):
        store = JobStore.create(
            self.destination, "Projeto", local_root=self.root / "local"
        )
        store.job.add_entries([EntryDraft(name) for name in names])
        store.save()
        self.store = store
        self.scanner = FakeScanner(script or [], drives=drives)
        self.pool = DrivePool(
            store=store,
            scanner=self.scanner,
            copier=self.copier,
            ejector=self.ejector,
            executor=InlineExecutor(),
            clock=self.clock,
            kind_probe=lambda root, fs: KindProbe(DiscKind.DATA, True, "teste"),
        )
        self.pool.start()
        return self.pool

    def settle(self, polls=2):
        for _ in range(polls):
            self.pool.poll()

    def run_countdown(self):
        self.clock.advance(11)
        self.pool.tick()

    def statuses(self):
        return [entry.status for entry in self.store.job.entries]


class DiscoveryTests(PoolCase):
    def test_it_makes_one_runner_per_drive(self) -> None:
        pool = self.build()
        self.assertEqual(pool.drives, ["D:", "E:"])
        self.assertEqual(sorted(pool.runners), ["D:", "E:"])

    def test_each_runner_is_bound_to_its_own_drive(self) -> None:
        pool = self.build()
        self.assertEqual(pool.runner_for("D:").drive, "D:")
        self.assertEqual(pool.runner_for("e:").drive, "E:")

    def test_drives_are_sorted_so_panels_do_not_reshuffle(self) -> None:
        pool = self.build(drives=("F:", "D:", "E:"))
        self.assertEqual(pool.drives, ["D:", "E:", "F:"])

    def test_a_scanner_that_cannot_list_drives_still_works(self) -> None:
        """The pool falls back to whatever has a disc in it."""

        class OldScanner:
            def scan(self):
                return [disc(root="D:\\"), disc(serial=2, root="E:\\")]

        store = JobStore.create(
            self.destination, "Projeto", local_root=self.root / "local"
        )
        store.save()
        pool = DrivePool(store=store, scanner=OldScanner(), executor=InlineExecutor())
        self.assertEqual(pool.drives, ["D:", "E:"])


class SeparationTests(PoolCase):
    """The core of it: two drives must not target one folder."""

    def test_two_discs_go_to_two_different_folders(self) -> None:
        self.build(script=[
            [disc(serial=1, root="D:\\"), disc(serial=2, root="E:\\")],
            [disc(serial=1, root="D:\\"), disc(serial=2, root="E:\\")],
        ])
        self.settle()

        d_target = self.pool.runner_for("D:").current_entry_id
        e_target = self.pool.runner_for("E:").current_entry_id
        self.assertIsNotNone(d_target)
        self.assertIsNotNone(e_target)
        self.assertNotEqual(d_target, e_target)

    def test_the_first_two_folders_are_taken_in_order(self) -> None:
        self.build(script=[
            [disc(serial=1, root="D:\\"), disc(serial=2, root="E:\\")],
            [disc(serial=1, root="D:\\"), disc(serial=2, root="E:\\")],
        ])
        self.settle()

        entries = self.store.job.entries
        self.assertEqual(self.pool.runner_for("D:").current_entry_id,
                         entries[0].entry_id)
        self.assertEqual(self.pool.runner_for("E:").current_entry_id,
                         entries[1].entry_id)

    def test_both_copies_land_in_their_own_folder(self) -> None:
        self.build(script=[
            [disc(serial=1, root="D:\\"), disc(serial=2, root="E:\\")],
            [disc(serial=1, root="D:\\"), disc(serial=2, root="E:\\")],
        ])
        self.settle()
        self.run_countdown()

        destinations = sorted(dest.name for _, dest in self.copier.copied)
        self.assertEqual(destinations, ["Disco 01", "Disco 02"])
        self.assertEqual(self.statuses()[:2], [EntryStatus.DONE, EntryStatus.DONE])

    def test_a_released_folder_returns_to_the_queue(self) -> None:
        """Skipping in one drive must not strand that folder."""
        self.build(script=[
            [disc(serial=1, root="D:\\")],
            [disc(serial=1, root="D:\\")],
        ])
        self.settle()
        first = self.store.job.entries[0].entry_id
        self.assertEqual(self.pool.claims.owner_of(first), "D:")

        self.pool.runner_for("D:").skip_disc()

        self.assertIsNone(self.pool.claims.owner_of(first))

    def test_one_drive_alone_still_takes_the_first_folder(self) -> None:
        self.build(script=[[disc(root="E:\\", serial=9)],
                           [disc(root="E:\\", serial=9)]])
        self.settle()
        self.assertIsNone(self.pool.runner_for("D:").current_entry_id)
        self.assertEqual(self.pool.runner_for("E:").current_entry_id,
                         self.store.job.entries[0].entry_id)


class ClaimRegistryTests(unittest.TestCase):
    def test_a_claim_is_invisible_to_its_own_owner(self) -> None:
        claims = ClaimRegistry()
        claims.claim("D:", "abc")
        self.assertEqual(claims.held_by_others("D:"), set())
        self.assertEqual(claims.held_by_others("E:"), {"abc"})

    def test_one_owner_holds_one_folder_at_a_time(self) -> None:
        claims = ClaimRegistry()
        claims.claim("D:", "abc")
        claims.claim("D:", "def")
        self.assertEqual(claims.held_by_others("E:"), {"def"})

    def test_releasing_an_unheld_owner_is_harmless(self) -> None:
        claims = ClaimRegistry()
        claims.release("D:")  # must not raise

    def test_it_reports_who_holds_what(self) -> None:
        claims = ClaimRegistry()
        claims.claim("D:", "abc")
        claims.claim("E:", "def")
        self.assertEqual(claims.as_dict(), {"abc": "D:", "def": "E:"})
        self.assertEqual(claims.owner_of("def"), "E:")


class AdvisoryTests(PoolCase):
    """What the operator reads off the panels to keep two drives straight."""

    def test_idle_drives_are_dealt_the_queue_in_order(self) -> None:
        self.build()
        expected = self.pool.expected()
        entries = self.store.job.entries
        self.assertEqual(expected["D:"].entry_id, entries[0].entry_id)
        self.assertEqual(expected["E:"].entry_id, entries[1].entry_id)

    def test_a_busy_drive_reports_the_folder_it_actually_holds(self) -> None:
        self.build(script=[[disc(serial=1, root="D:\\")],
                           [disc(serial=1, root="D:\\")]])
        self.settle()
        expected = self.pool.expected()
        entries = self.store.job.entries
        self.assertEqual(expected["D:"].entry_id, entries[0].entry_id)
        # E: is still free, and must be advised the *next* one, not the one
        # D: is already holding.
        self.assertEqual(expected["E:"].entry_id, entries[1].entry_id)

    def test_it_never_advises_the_same_folder_twice(self) -> None:
        self.build()
        expected = self.pool.expected()
        ids = [entry.entry_id for entry in expected.values() if entry]
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_short_queue_leaves_the_spare_drive_with_nothing(self) -> None:
        self.build(names=("Disco 01",))
        expected = self.pool.expected()
        self.assertIsNotNone(expected["D:"])
        self.assertIsNone(expected["E:"])

    def test_nothing_is_reserved_by_asking(self) -> None:
        """Advisory means advisory: reading the panel must not claim."""
        self.build()
        self.pool.expected()
        self.assertEqual(self.pool.claimed_by(), {})


class AggregateTests(PoolCase):
    def test_busy_is_true_while_any_drive_copies(self) -> None:
        self.build(script=[[disc(serial=1, root="D:\\")],
                           [disc(serial=1, root="D:\\")]])
        self.settle()
        self.assertFalse(self.pool.is_busy)  # counting down, not copying yet

    def test_working_entry_ids_is_empty_when_idle(self) -> None:
        self.build()
        self.assertEqual(self.pool.working_entry_ids(), set())

    def test_events_say_which_drive_they_came_from(self) -> None:
        self.build(script=[
            [disc(serial=1, root="D:\\"), disc(serial=2, root="E:\\")],
            [disc(serial=1, root="D:\\"), disc(serial=2, root="E:\\")],
        ])
        self.settle()
        drives = {
            event.drive for event in self.pool.drain()
            if isinstance(event, StateChanged)
        }
        self.assertEqual(drives, {"D:", "E:"})

    def test_every_drive_starts_out_waiting(self) -> None:
        pool = self.build()
        for drive, runner in pool.runners.items():
            with self.subTest(drive=drive):
                self.assertIs(runner.state, RunnerState.READY_NO_DISC)


if __name__ == "__main__":
    unittest.main()
