"""Persistence tests.

The behaviours worth pinning are the ones that only show up when something goes
wrong: a crash mid-write must not destroy the previous job file, an unreachable
network share must not lose the local record, and a corrupt file must fall back
to its backup rather than stranding the batch.
"""

import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from backupov2.jobmodel import EntryDraft, EntryStatus, Job
from backupov2.jobstore import (
    JOB_FILE_NAME,
    LOG_FILE_NAME,
    JobStore,
    atomic_write_json,
    control_files,
    load_recent,
    remove_control_files,
)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.destination = self.root / "dest"
        self.local_root = self.root / "local"
        self.destination.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def make_store(self, entries: int = 3) -> JobStore:
        store = JobStore.create(
            self.destination, "Projeto 2019", local_root=self.local_root
        )
        store.job.add_entries([EntryDraft(f"Disco {i:02d}") for i in range(1, entries + 1)])
        return store


class AtomicWriteTests(StoreCase):
    def test_creates_parent_directories(self) -> None:
        target = self.root / "a" / "b" / "job.json"
        atomic_write_json(target, {"hello": "world"})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"hello": "world"})

    def test_keeps_a_backup_of_the_previous_version(self) -> None:
        target = self.root / "job.json"
        atomic_write_json(target, {"version": 1})
        atomic_write_json(target, {"version": 2})

        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["version"], 2)
        backup = target.with_name(target.name + ".bak")
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))["version"], 1)

    def test_failed_replace_leaves_the_original_intact(self) -> None:
        """A crash mid-save must never produce a half-written job file."""
        target = self.root / "job.json"
        atomic_write_json(target, {"version": 1})

        with unittest.mock.patch(
            "backupov2.jobstore.os.replace", side_effect=OSError(5, "denied")
        ):
            with self.assertRaises(OSError):
                atomic_write_json(target, {"version": 2})

        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["version"], 1)
        self.assertFalse(target.with_name(target.name + ".tmp").exists())

    def test_writes_utf8_without_escaping(self) -> None:
        target = self.root / "job.json"
        atomic_write_json(target, {"name": "Projeto Ação 2019"})
        self.assertIn("Ação", target.read_text(encoding="utf-8"))


class SaveTests(StoreCase):
    def test_writes_both_copies(self) -> None:
        store = self.make_store()
        outcome = store.save()

        self.assertTrue(outcome.local_ok)
        self.assertTrue(outcome.remote_ok)
        self.assertTrue(store.local_path.exists())
        self.assertEqual(store.remote_path.name, JOB_FILE_NAME)
        self.assertTrue(store.remote_path.exists())

    def test_unreachable_share_still_saves_locally(self) -> None:
        """The case the local-first ordering exists for."""
        store = JobStore.create(
            self.root / "no-such-share", "Projeto", local_root=self.local_root
        )
        store.job.add_entries([EntryDraft("Disco 01")])

        real_write = atomic_write_json

        def fail_on_remote(path, payload):
            if path.name == JOB_FILE_NAME:
                raise OSError(53, "The network path was not found")
            return real_write(path, payload)

        with unittest.mock.patch("backupov2.jobstore.atomic_write_json", fail_on_remote):
            outcome = store.save()

        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.local_ok)
        self.assertFalse(outcome.remote_ok)
        self.assertIn("network path", outcome.remote_error)

    def test_save_updates_the_timestamp(self) -> None:
        store = self.make_store()
        store.job.updated_utc = "2000-01-01T00:00:00Z"
        store.save()
        self.assertNotEqual(store.job.updated_utc, "2000-01-01T00:00:00Z")

    def test_mutations_persist_immediately(self) -> None:
        store = self.make_store(2)
        store.add_entries([EntryDraft("Disco 03")])
        reloaded = JobStore.load(store.local_path).job
        self.assertEqual([e.folder_name for e in reloaded.entries][-1], "Disco 03")


class LoadTests(StoreCase):
    def test_roundtrip_through_disk(self) -> None:
        store = self.make_store(2)
        store.job.entries[0].status = EntryStatus.DONE
        store.save()

        outcome = JobStore.load(store.local_path)

        self.assertEqual(outcome.job.job_id, store.job.job_id)
        self.assertEqual(outcome.job.entries[0].status, EntryStatus.DONE)
        self.assertFalse(outcome.used_backup)

    def test_corrupt_file_falls_back_to_backup(self) -> None:
        store = self.make_store(1)
        store.save()
        store.job.add_entries([EntryDraft("Disco 02")])
        store.save()  # the one-entry version is now the .bak

        store.local_path.write_text("{ this is not json", encoding="utf-8")
        outcome = JobStore.load(store.local_path)

        self.assertTrue(outcome.used_backup)
        self.assertEqual(len(outcome.job.entries), 1)
        self.assertTrue(outcome.notes)

    def test_corrupt_file_without_backup_raises(self) -> None:
        bad = self.root / "job.json"
        bad.write_text("{ nope", encoding="utf-8")
        with self.assertRaises(ValueError):
            JobStore.load(bad)

    def test_non_object_json_is_rejected(self) -> None:
        bad = self.root / "job.json"
        bad.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(ValueError):
            JobStore.load(bad)


class OpenNewestTests(StoreCase):
    def test_newest_copy_wins(self) -> None:
        """The local copy runs ahead whenever the share was offline."""
        store = self.make_store(1)
        store.save()

        remote = store.remote_path
        stale = json.loads(remote.read_text(encoding="utf-8"))
        stale["updated_utc"] = "2000-01-01T00:00:00Z"
        stale["entries"] = []
        remote.write_text(json.dumps(stale), encoding="utf-8")

        outcome = JobStore.open_newest([store.local_path, remote])

        self.assertEqual(outcome.source_path, store.local_path)
        self.assertEqual(len(outcome.job.entries), 1)
        self.assertTrue(any("mais recente" in note for note in outcome.notes))

    def test_missing_candidates_are_skipped(self) -> None:
        store = self.make_store(1)
        store.save()
        outcome = JobStore.open_newest([self.root / "gone.json", store.local_path])
        self.assertEqual(outcome.source_path, store.local_path)

    def test_no_readable_candidate_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            JobStore.open_newest([self.root / "a.json", self.root / "b.json"])

    def test_open_for_finds_the_portable_copy(self) -> None:
        store = self.make_store(2)
        store.save()
        outcome = JobStore.open_for(self.destination, "Projeto 2019")
        self.assertEqual(len(outcome.job.entries), 2)


class RecentJobsTests(StoreCase):
    def test_saving_records_the_job(self) -> None:
        store = self.make_store(2)
        store.save()
        recent = load_recent(self.local_root)
        self.assertEqual(recent[0]["job_id"], store.job.job_id)
        self.assertEqual(recent[0]["parent_name"], "Projeto 2019")
        self.assertEqual(recent[0]["entry_count"], 2)

    def test_resaving_moves_the_job_to_the_front_without_duplicating(self) -> None:
        first = self.make_store(1)
        first.save()
        second = JobStore.create(self.destination, "Outro", local_root=self.local_root)
        second.save()
        first.save()

        recent = load_recent(self.local_root)
        ids = [item["job_id"] for item in recent]
        self.assertEqual(ids[0], first.job.job_id)
        self.assertEqual(len(ids), len(set(ids)))

    def test_missing_recent_file_is_empty(self) -> None:
        self.assertEqual(load_recent(self.root / "nowhere"), [])

    def test_corrupt_recent_file_is_empty(self) -> None:
        path = self.local_root / "recent.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        self.assertEqual(load_recent(self.local_root), [])


class ControlFileCleanupTests(StoreCase):
    """The cleanup must remove only this app's bookkeeping, never backed-up data."""

    def setUp(self) -> None:
        super().setUp()
        self.store = self.make_store(2)
        self.store.save()
        self.parent = self.store.job.parent_path
        # Backed-up content that must survive untouched.
        disc_folder = self.parent / "Disco 01"
        disc_folder.mkdir(parents=True, exist_ok=True)
        (disc_folder / "documento.pdf").write_bytes(b"dados importantes")
        (self.parent / "leia-me.txt").write_text("nota do usuario", encoding="utf-8")

    def test_finds_the_job_file(self) -> None:
        names = {path.name for path in control_files(self.parent)}
        self.assertIn(JOB_FILE_NAME, names)

    def test_finds_bak_and_log_too(self) -> None:
        (self.parent / (JOB_FILE_NAME + ".bak")).write_text("{}", encoding="utf-8")
        (self.parent / LOG_FILE_NAME).write_text("linha", encoding="utf-8")
        names = {path.name for path in control_files(self.parent)}
        self.assertEqual(
            names, {JOB_FILE_NAME, JOB_FILE_NAME + ".bak", LOG_FILE_NAME}
        )

    def test_ignores_user_files(self) -> None:
        names = {path.name for path in control_files(self.parent)}
        self.assertNotIn("leia-me.txt", names)

    def test_removes_control_files_and_leaves_data_alone(self) -> None:
        (self.parent / (JOB_FILE_NAME + ".bak")).write_text("{}", encoding="utf-8")

        removed, failures = remove_control_files(self.parent)

        self.assertEqual(failures, [])
        self.assertEqual(len(removed), 2)
        self.assertFalse((self.parent / JOB_FILE_NAME).exists())
        self.assertFalse((self.parent / (JOB_FILE_NAME + ".bak")).exists())
        # The backup itself is untouched.
        self.assertEqual(
            (self.parent / "Disco 01" / "documento.pdf").read_bytes(),
            b"dados importantes",
        )
        self.assertTrue((self.parent / "leia-me.txt").exists())

    def test_local_copy_survives_so_the_job_can_be_reopened(self) -> None:
        remove_control_files(self.parent)
        self.assertTrue(self.store.local_path.exists())
        reopened = JobStore.load(self.store.local_path).job
        self.assertEqual(len(reopened.entries), 2)

    def test_cleaning_twice_is_harmless(self) -> None:
        remove_control_files(self.parent)
        removed, failures = remove_control_files(self.parent)
        self.assertEqual(removed, [])
        self.assertEqual(failures, [])

    def test_missing_folder_is_not_an_error(self) -> None:
        self.assertEqual(control_files(self.root / "nao-existe"), [])
        self.assertEqual(remove_control_files(self.root / "nao-existe"), ([], []))

    def test_saving_again_recreates_the_portable_copy(self) -> None:
        """Documented behaviour: an unfinished job writes the file back."""
        remove_control_files(self.parent)
        self.store.save()
        self.assertTrue((self.parent / JOB_FILE_NAME).exists())


if __name__ == "__main__":
    unittest.main()
