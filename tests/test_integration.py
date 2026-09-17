"""End-to-end: real files, real copy engine, real persistence.

Only the optical drive is faked. Directories stand in for discs, so these
exercise the actual bytes-on-disk path that a real batch takes - scan, copy,
verify, commit, eject, advance - including skip, redirect, and resuming an
interrupted job from the file that was written along the way.
"""

import tempfile
import unittest
from pathlib import Path

from backupov2.core import ensure_folders
from backupov2.jobmodel import DiscKind, EntryDraft, EntryStatus
from backupov2.jobstore import JobStore
from backupov2.media import FakeScanner, KindProbe, MediaInfo
from backupov2.reconcile import reconcile
from backupov2.runner import InlineExecutor, JobRunner, RunnerState


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingEjector:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def eject(self, drive: str) -> bool:
        self.calls.append(drive)
        return True


class IntegrationCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self.destination = self.root / "dest"
        self.destination.mkdir()
        self.discs = self.root / "discs"
        self.discs.mkdir()

        self.clock = FakeClock()
        self.ejector = RecordingEjector()

    def make_disc(self, name: str, files: dict[str, bytes], serial: int) -> MediaInfo:
        """A directory standing in for a mounted disc."""
        root = self.discs / name
        for relative, payload in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        return MediaInfo(root=root, label=name, serial=serial, fs_name="CDFS")

    def build(self, names, script):
        store = JobStore.create(
            self.destination, "Projeto", local_root=self.root / "local"
        )
        store.job.add_entries([EntryDraft(name) for name in names])
        ensure_folders(self.destination, "Projeto", names)
        store.save()
        self.store = store

        self.scanner = FakeScanner(script)
        self.runner = JobRunner(
            store=store,
            scanner=self.scanner,
            ejector=self.ejector,
            executor=InlineExecutor(),
            clock=self.clock,
            kind_probe=lambda root, fs: KindProbe(DiscKind.DATA, True, "teste"),
        )
        self.runner.start()
        return self.runner

    def insert(self, media: MediaInfo) -> None:
        """Simulate inserting a disc: settle, then let the countdown expire."""
        self.scanner.script = [[media], [media]]
        self.scanner.calls = 0
        self.runner.poll()
        self.runner.poll()
        self.clock.advance(11)
        self.runner.tick()

    def remove(self) -> None:
        self.scanner.script = [[], []]
        self.scanner.calls = 0
        self.runner.poll()
        self.runner.poll()

    def folder(self, name: str) -> Path:
        return self.destination / "Projeto" / name


class FullBatchTests(IntegrationCase):
    def test_three_discs_land_on_disk(self) -> None:
        a = self.make_disc("A", {"doc.txt": b"primeiro", "sub/x.bin": b"12345"}, 101)
        b = self.make_disc("B", {"planta.dwg": b"segundo disco"}, 102)
        c = self.make_disc("C", {"memorial.pdf": b"terceiro"}, 103)

        self.build(("Disco 01", "Disco 02", "Disco 03"), [[]])

        for media in (a, b, c):
            self.insert(media)
            self.remove()

        # Every file is where it should be, with the right bytes.
        self.assertEqual((self.folder("Disco 01") / "doc.txt").read_bytes(), b"primeiro")
        self.assertEqual((self.folder("Disco 01") / "sub" / "x.bin").read_bytes(), b"12345")
        self.assertEqual(
            (self.folder("Disco 02") / "planta.dwg").read_bytes(), b"segundo disco"
        )
        self.assertEqual(
            (self.folder("Disco 03") / "memorial.pdf").read_bytes(), b"terceiro"
        )

        job = self.store.job
        self.assertEqual([e.status for e in job.entries], [EntryStatus.DONE] * 3)
        self.assertEqual([e.media.label for e in job.entries], ["A", "B", "C"])
        self.assertEqual(len(self.ejector.calls), 3)
        self.assertIs(self.runner.state, RunnerState.JOB_COMPLETE)

    def test_empty_directories_are_preserved(self) -> None:
        media = self.make_disc("A", {"keep/file.txt": b"x"}, 201)
        (media.root / "empty-dir").mkdir()
        self.build(("Disco 01",), [[]])
        self.insert(media)
        self.assertTrue((self.folder("Disco 01") / "empty-dir").is_dir())


class SkipAndRedirectTests(IntegrationCase):
    def test_skipped_disc_writes_nothing_and_keeps_the_folder_free(self) -> None:
        a = self.make_disc("A", {"a.txt": b"aaa"}, 301)
        b = self.make_disc("B", {"b.txt": b"bbb"}, 302)
        self.build(("Disco 01", "Disco 02"), [[]])

        # Disc A shows up but the user skips it.
        self.scanner.script = [[a], [a]]
        self.scanner.calls = 0
        self.runner.poll()
        self.runner.poll()
        self.runner.skip_disc()
        self.remove()

        self.assertEqual(list(self.folder("Disco 01").iterdir()), [])

        # The next disc takes the folder the skipped one would have used.
        self.insert(b)
        self.assertEqual((self.folder("Disco 01") / "b.txt").read_bytes(), b"bbb")
        self.assertEqual(self.store.job.entries[1].status, EntryStatus.PENDING)

    def test_redirect_writes_to_the_chosen_folder(self) -> None:
        a = self.make_disc("A", {"a.txt": b"conteudo"}, 401)
        self.build(("Disco 01", "Disco 02", "Disco 03"), [[]])

        self.scanner.script = [[a], [a]]
        self.scanner.calls = 0
        self.runner.poll()
        self.runner.poll()
        self.runner.send_to(self.store.job.entries[2].entry_id)
        self.clock.advance(11)
        self.runner.tick()

        self.assertEqual((self.folder("Disco 03") / "a.txt").read_bytes(), b"conteudo")
        self.assertEqual(list(self.folder("Disco 01").iterdir()), [])
        self.assertEqual(self.store.job.next_pending().folder_name, "Disco 01")


class ResumeTests(IntegrationCase):
    def test_interrupted_job_resumes_and_reuses_copied_files(self) -> None:
        a = self.make_disc("A", {"a.txt": b"aaa"}, 501)
        b = self.make_disc("B", {"b.txt": b"bbb"}, 502)
        self.build(("Disco 01", "Disco 02"), [[]])

        self.insert(a)
        self.remove()

        # The app "dies" mid-copy of the second disc.
        entry = self.store.job.entries[1]
        entry.status = EntryStatus.IN_PROGRESS
        self.store.save()
        partial = self.folder("Disco 02")
        (partial / "b.txt").write_bytes(b"bbb")  # already written before the crash

        # Reopen from the file on disk, exactly as a restart would.
        reopened = JobStore.load(self.store.local_path).job
        report = reconcile(reopened)

        self.assertEqual(reopened.entries[0].status, EntryStatus.DONE)
        self.assertEqual(reopened.entries[1].status, EntryStatus.PENDING)
        self.assertTrue(reopened.entries[1].result.partial)
        self.assertEqual(reopened.next_pending().folder_name, "Disco 02")
        self.assertEqual(len(report.questions), 0)

        # Finishing the job reuses the file that was already there.
        store = JobStore(reopened, local_root=self.root / "local")
        runner = JobRunner(
            store=store,
            scanner=FakeScanner([[b], [b]]),
            ejector=self.ejector,
            executor=InlineExecutor(),
            clock=self.clock,
            kind_probe=lambda root, fs: KindProbe(DiscKind.DATA, True, "teste"),
        )
        runner.start()
        runner.poll()
        runner.poll()
        self.clock.advance(11)
        runner.tick()

        finished = store.job.entries[1]
        self.assertEqual(finished.status, EntryStatus.DONE)
        self.assertEqual(finished.result.files_skipped_existing, 1)
        self.assertEqual(finished.result.files_copied, 0)

    def test_reopening_a_finished_job_reports_nothing_to_do(self) -> None:
        a = self.make_disc("A", {"a.txt": b"aaa"}, 601)
        self.build(("Disco 01",), [[]])
        self.insert(a)

        reopened = JobStore.load(self.store.local_path).job
        report = reconcile(reopened)

        self.assertTrue(reopened.is_complete)
        self.assertEqual(report.questions, [])


class DamagedDiscTests(IntegrationCase):
    def test_unreadable_file_costs_one_file_not_the_disc(self) -> None:
        import unittest.mock

        import backupov2.core

        media = self.make_disc(
            "A", {"ok1.txt": b"aa", "bad.txt": b"bb", "ok2.txt": b"cc"}, 701
        )
        self.build(("Disco 01",), [[]])

        real_copy = backupov2.core.shutil.copy2

        def scratched(src, dst, *args, **kwargs):
            if "bad.txt" in str(src):
                raise OSError(0, "Data error (cyclic redundancy check)", None, 23)
            return real_copy(src, dst, *args, **kwargs)

        with unittest.mock.patch("backupov2.core.shutil.copy2", scratched):
            with unittest.mock.patch(
                "backupov2.core.CopyOptions.retry_delays", (0, 0, 0)
            ):
                self.insert(media)

        entry = self.store.job.entries[0]
        self.assertEqual(entry.status, EntryStatus.DONE)
        self.assertTrue(entry.needs_review)
        self.assertEqual(entry.result.files_failed, 1)
        self.assertEqual(entry.result.files_copied, 2)
        self.assertTrue((self.folder("Disco 01") / "ok1.txt").exists())
        self.assertTrue((self.folder("Disco 01") / "ok2.txt").exists())
        self.assertEqual(entry.result.failed_files[0]["path"], "bad.txt")


if __name__ == "__main__":
    unittest.main()
