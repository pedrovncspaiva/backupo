"""Job model tests.

The important ones here are the ``next_pending`` cases: they are the proof that
skip, redirect, reorder and mid-run insertion work, since all four reduce to
"the first entry still PENDING" rather than to a pointer that has to be kept
correct. v1 had no equivalent and could do none of them.
"""

import json
import unittest
from pathlib import Path

from backupov2.jobmodel import (
    DiscEntry,
    DiscKind,
    EntryDraft,
    EntryResult,
    EntryStatus,
    Job,
    JobSettings,
    MediaRecord,
)


def make_job(count: int = 4) -> Job:
    job = Job(destination_root="Z:/FILE_SERVER/Backups", parent_name="Projeto 2019")
    job.add_entries([EntryDraft(f"Disco {i:02d}") for i in range(1, count + 1)])
    return job


class NextPendingTests(unittest.TestCase):
    def test_starts_at_the_first_entry(self) -> None:
        self.assertEqual(make_job().next_pending().folder_name, "Disco 01")

    def test_skip_advances_without_consuming_later_entries(self) -> None:
        job = make_job()
        job.entries[0].status = EntryStatus.SKIPPED
        self.assertEqual(job.next_pending().folder_name, "Disco 02")

    def test_redirect_leaves_the_earlier_entry_pending(self) -> None:
        """A disc sent to Disco 03 must not consume Disco 01's turn."""
        job = make_job()
        job.entries[2].status = EntryStatus.DONE
        self.assertEqual(job.next_pending().folder_name, "Disco 01")

    def test_reorder_changes_the_target(self) -> None:
        job = make_job()
        last = job.entries[-1]
        job.move_entry(last.entry_id, -99)  # clamps to the top
        self.assertEqual(job.next_pending().entry_id, last.entry_id)

    def test_insert_midrun_becomes_the_next_target(self) -> None:
        job = make_job()
        job.entries[0].status = EntryStatus.DONE
        job.add_entries([EntryDraft("Disco extra")], at_index=1)
        self.assertEqual(job.next_pending().folder_name, "Disco extra")

    def test_none_when_nothing_is_pending(self) -> None:
        job = make_job(2)
        job.entries[0].status = EntryStatus.DONE
        job.entries[1].status = EntryStatus.FAILED
        self.assertIsNone(job.next_pending())

    def test_failed_entries_do_not_block_the_queue(self) -> None:
        job = make_job()
        job.entries[0].status = EntryStatus.FAILED
        self.assertEqual(job.next_pending().folder_name, "Disco 02")


class CompletionTests(unittest.TestCase):
    def test_empty_job_is_not_complete(self) -> None:
        job = Job(destination_root="Z:/x", parent_name="p")
        self.assertFalse(job.is_complete)

    def test_complete_when_no_entry_is_open(self) -> None:
        job = make_job(2)
        job.entries[0].status = EntryStatus.DONE
        job.entries[1].status = EntryStatus.SKIPPED
        self.assertTrue(job.is_complete)

    def test_in_progress_counts_as_open(self) -> None:
        job = make_job(1)
        job.entries[0].status = EntryStatus.IN_PROGRESS
        self.assertFalse(job.is_complete)


class OrderingTests(unittest.TestCase):
    def test_move_preserves_entry_identity(self) -> None:
        """Reordering must never renumber anything - ids are the identity."""
        job = make_job()
        before = [entry.entry_id for entry in job.entries]
        moved = job.entries[0]
        job.move_entry(moved.entry_id, 2)
        self.assertEqual(sorted(entry.entry_id for entry in job.entries), sorted(before))
        self.assertEqual(job.index_of(moved.entry_id), 2)

    def test_move_clamps_at_both_ends(self) -> None:
        job = make_job(3)
        first, last = job.entries[0], job.entries[-1]
        self.assertEqual(job.move_entry(first.entry_id, -5), 0)
        self.assertEqual(job.move_entry(last.entry_id, 5), 2)

    def test_remove_by_id(self) -> None:
        job = make_job(3)
        target = job.entries[1]
        job.remove_entry(target.entry_id)
        self.assertEqual(len(job), 2)
        self.assertIsNone(job.entry_by_id(target.entry_id))

    def test_index_of_unknown_raises(self) -> None:
        with self.assertRaises(KeyError):
            make_job(1).index_of("no-such-id")


class DuplicateDetectionTests(unittest.TestCase):
    def test_matches_on_non_zero_serial(self) -> None:
        job = make_job(2)
        job.entries[0].media = MediaRecord(drive="D:", label="A", serial=1234)
        self.assertIs(job.find_by_serial(1234), job.entries[0])

    def test_zero_serial_never_matches(self) -> None:
        """Some discs report serial 0; treating that as identity would flag
        every unlabelled disc as a duplicate of every other."""
        job = make_job(2)
        job.entries[0].media = MediaRecord(drive="D:", label="A", serial=0)
        self.assertIsNone(job.find_by_serial(0))

    def test_audio_discs_match_on_toc_hash(self) -> None:
        job = make_job(2)
        job.entries[0].media = MediaRecord(drive="D:", toc_hash="abc123")
        self.assertIs(job.find_by_toc_hash("abc123"), job.entries[0])
        self.assertIsNone(job.find_by_toc_hash(None))


class FolderNameTests(unittest.TestCase):
    def test_detects_case_insensitive_collision(self) -> None:
        job = make_job(2)
        self.assertTrue(job.has_folder_named("disco 01"))
        self.assertFalse(job.has_folder_named("Disco 99"))

    def test_excludes_the_entry_being_renamed(self) -> None:
        job = make_job(2)
        first = job.entries[0]
        self.assertFalse(job.has_folder_named("Disco 01", excluding=first.entry_id))


class SerialisationTests(unittest.TestCase):
    def roundtrip(self, job: Job) -> Job:
        return Job.from_dict(json.loads(json.dumps(job.to_dict())))

    def test_full_roundtrip(self) -> None:
        job = make_job(2)
        job.entries[0].status = EntryStatus.DONE
        job.entries[0].disc_kind = DiscKind.DATA
        job.entries[0].media = MediaRecord(
            drive="D:", label="PROJ_01", serial=1234567890, file_count=812,
            total_bytes=4294901760,
        )
        job.entries[0].result = EntryResult(files_copied=812, bytes_copied=4294901760)

        restored = self.roundtrip(job)

        self.assertEqual(restored.job_id, job.job_id)
        self.assertEqual(restored.entries[0].status, EntryStatus.DONE)
        self.assertEqual(restored.entries[0].media.serial, 1234567890)
        self.assertEqual(restored.entries[0].result.files_copied, 812)
        self.assertEqual(restored.entries[0].entry_id, job.entries[0].entry_id)

    def test_llm_provenance_survives_roundtrip(self) -> None:
        """The future photo/LLM step must need no schema change."""
        job = Job(destination_root="Z:/x", parent_name="p")
        job.add_entries(
            [
                EntryDraft(
                    folder_name="Obra 47 - Sondagem",
                    source="llm_photo",
                    name_confidence=0.82,
                    needs_review=True,
                    photo_ref="_media/disc-07.jpg",
                    llm={"model": "claude", "alternatives": ["Obra 47"]},
                )
            ]
        )
        entry = self.roundtrip(job).entries[0]
        self.assertEqual(entry.source, "llm_photo")
        self.assertAlmostEqual(entry.name_confidence, 0.82)
        self.assertTrue(entry.needs_review)
        self.assertEqual(entry.photo_ref, "_media/disc-07.jpg")
        self.assertEqual(entry.llm["alternatives"], ["Obra 47"])

    def test_unknown_keys_are_preserved(self) -> None:
        """An older build must never destroy a newer build's fields."""
        data = make_job(1).to_dict()
        data["future_top_level"] = {"x": 1}
        data["entries"][0]["future_entry_field"] = "keep me"
        data["settings"]["future_setting"] = True

        restored = Job.from_dict(data)
        self.assertEqual(restored.unknown["future_top_level"], {"x": 1})

        emitted = restored.to_dict()
        self.assertEqual(emitted["future_top_level"], {"x": 1})
        self.assertEqual(emitted["entries"][0]["future_entry_field"], "keep me")
        self.assertTrue(emitted["settings"]["future_setting"])

    def test_unrecognised_status_falls_back_to_pending(self) -> None:
        """A stale enum value must not make the whole job unopenable."""
        data = make_job(1).to_dict()
        data["entries"][0]["status"] = "some_future_status"
        self.assertEqual(Job.from_dict(data).entries[0].status, EntryStatus.PENDING)

    def test_missing_entry_id_gets_one(self) -> None:
        data = make_job(1).to_dict()
        del data["entries"][0]["entry_id"]
        self.assertTrue(Job.from_dict(data).entries[0].entry_id)

    def test_settings_defaults_match_the_agreed_decisions(self) -> None:
        settings = JobSettings()
        self.assertTrue(settings.auto_copy)
        self.assertEqual(settings.grace_seconds, 10)
        self.assertTrue(settings.auto_eject)
        self.assertEqual(settings.audio_format, "mp3")
        self.assertEqual(settings.audio_bitrate, "320k")
        self.assertEqual(settings.on_file_error, "retry_then_skip")

    def test_partial_settings_keep_defaults(self) -> None:
        settings = JobSettings.from_dict({"grace_seconds": 0})
        self.assertEqual(settings.grace_seconds, 0)
        self.assertTrue(settings.auto_eject)


class StatusCountTests(unittest.TestCase):
    def test_counts_every_status(self) -> None:
        job = make_job(3)
        job.entries[0].status = EntryStatus.DONE
        counts = job.count_by_status()
        self.assertEqual(counts[EntryStatus.DONE], 1)
        self.assertEqual(counts[EntryStatus.PENDING], 2)
        self.assertEqual(counts[EntryStatus.FAILED], 0)

    def test_failed_file_count_without_result(self) -> None:
        self.assertEqual(DiscEntry(folder_name="x").failed_file_count, 0)


class GroupedFolderTests(unittest.TestCase):
    """CAIXA > EG > disc, the structure a delivery sheet describes."""

    def make(self) -> Job:
        job = Job(
            destination_root="Z:/FILE_SERVER",
            parent_name="CAIXA 02 (EG 1841 ao EG 1852)",
        )
        job.add_entries(
            [
                EntryDraft("ABG-DI-8963-GI-19 R0", group="EG 1841 (BENGUELA - ABG)"),
                EntryDraft("ABG-DI-8963-GI-18 R0", group="EG 1841 (BENGUELA - ABG)"),
                EntryDraft("PAC-DI-1885-GM-01", group="EG 1885 (CAPANDA - PAC)"),
                EntryDraft("Disco avulso"),
            ]
        )
        return job

    def test_group_becomes_a_middle_directory(self) -> None:
        job = self.make()
        self.assertEqual(
            job.folder_for(job.entries[0]),
            Path("Z:/FILE_SERVER")
            / "CAIXA 02 (EG 1841 ao EG 1852)"
            / "EG 1841 (BENGUELA - ABG)"
            / "ABG-DI-8963-GI-19 R0",
        )

    def test_no_group_sits_directly_under_the_parent(self) -> None:
        job = self.make()
        self.assertEqual(
            job.folder_for(job.entries[3]),
            Path("Z:/FILE_SERVER") / "CAIXA 02 (EG 1841 ao EG 1852)" / "Disco avulso",
        )

    def test_group_survives_a_roundtrip(self) -> None:
        restored = Job.from_dict(json.loads(json.dumps(self.make().to_dict())))
        self.assertEqual(restored.entries[0].group, "EG 1841 (BENGUELA - ABG)")
        self.assertEqual(restored.entries[3].group, "")

    def test_group_names_lists_each_once_in_order(self) -> None:
        self.assertEqual(
            self.make().group_names(),
            ["EG 1841 (BENGUELA - ABG)", "EG 1885 (CAPANDA - PAC)"],
        )

    def test_same_name_in_two_groups_does_not_clash(self) -> None:
        """Different directories, so there is nothing to disambiguate."""
        job = self.make()
        self.assertFalse(
            job.has_folder_named("PAC-DI-1885-GM-01", group="EG 1841 (BENGUELA - ABG)")
        )
        self.assertTrue(
            job.has_folder_named("PAC-DI-1885-GM-01", group="EG 1885 (CAPANDA - PAC)")
        )

    def test_ungrouped_name_does_not_clash_with_a_grouped_one(self) -> None:
        job = self.make()
        self.assertFalse(job.has_folder_named("ABG-DI-8963-GI-19 R0"))

    def test_next_pending_is_unaffected_by_grouping(self) -> None:
        job = self.make()
        job.entries[0].status = EntryStatus.DONE
        self.assertEqual(job.next_pending().folder_name, "ABG-DI-8963-GI-18 R0")


class CollectingFolderTests(unittest.TestCase):
    """A folder flagged to keep receiving discs until it is flagged off."""

    def test_a_collecting_folder_is_the_target_out_of_turn(self) -> None:
        job = make_job(3)
        job.set_collecting(job.entries[2].entry_id)
        self.assertEqual(job.next_pending().folder_name, "Disco 03")

    def test_it_stays_the_target_after_a_disc_is_written(self) -> None:
        """The point of the flag: finishing a disc must not advance the list."""
        job = make_job(3)
        job.set_collecting(job.entries[1].entry_id)
        job.entries[1].status = EntryStatus.DONE
        self.assertEqual(job.next_pending().folder_name, "Disco 02")

    def test_turning_it_off_restores_plain_list_order(self) -> None:
        job = make_job(3)
        job.set_collecting(job.entries[1].entry_id)
        job.entries[1].status = EntryStatus.DONE
        job.set_collecting(job.entries[1].entry_id, False)
        self.assertEqual(job.next_pending().folder_name, "Disco 01")

    def test_only_one_folder_can_collect(self) -> None:
        """Two would both claim every disc and list order would break the tie -
        which is not a choice anyone made."""
        job = make_job(3)
        job.set_collecting(job.entries[0].entry_id)
        job.set_collecting(job.entries[2].entry_id)
        self.assertEqual([e.collecting for e in job.entries], [False, False, True])

    def test_the_job_is_not_complete_while_a_folder_collects(self) -> None:
        job = make_job(2)
        for entry in job.entries:
            entry.status = EntryStatus.DONE
        self.assertTrue(job.is_complete)

        job.set_collecting(job.entries[0].entry_id)
        self.assertFalse(job.is_complete)

        job.set_collecting(job.entries[0].entry_id, False)
        self.assertTrue(job.is_complete)

    def test_absorb_accumulates_totals_and_keeps_each_disc(self) -> None:
        job = make_job(1)
        entry = job.entries[0]
        job.set_collecting(entry.entry_id)

        entry.absorb(
            MediaRecord(label="A", serial=1),
            EntryResult(files_copied=3, bytes_copied=300),
        )
        entry.absorb(
            MediaRecord(label="B", serial=2),
            EntryResult(files_copied=5, bytes_copied=500, files_renamed=2),
        )

        self.assertEqual(entry.result.files_copied, 8)
        self.assertEqual(entry.result.bytes_copied, 800)
        self.assertEqual(entry.result.files_renamed, 2)
        self.assertEqual(entry.disc_count, 2)
        self.assertEqual([d.media.label for d in entry.collected], ["A", "B"])
        self.assertEqual([d.files_copied for d in entry.collected], [3, 5])

    def test_the_label_column_names_the_most_recent_disc(self) -> None:
        job = make_job(1)
        entry = job.entries[0]
        entry.absorb(MediaRecord(label="A", serial=1), EntryResult())
        entry.absorb(MediaRecord(label="B", serial=2), EntryResult())
        self.assertEqual(entry.media.label, "B")

    def test_an_earlier_disc_is_still_recognised_as_already_copied(self) -> None:
        """``media`` only holds the last disc, so without looking through the
        collected list the earlier ones would silently stop matching."""
        job = make_job(1)
        entry = job.entries[0]
        entry.absorb(MediaRecord(label="A", serial=111), EntryResult())
        entry.absorb(MediaRecord(label="B", serial=222), EntryResult())

        self.assertIs(job.find_by_serial(111), entry)
        self.assertIs(job.find_by_serial(222), entry)
        self.assertIsNone(job.find_by_serial(333))

    def test_round_trip_preserves_the_flag_and_the_discs(self) -> None:
        job = make_job(2)
        entry = job.entries[1]
        job.set_collecting(entry.entry_id)
        entry.absorb(
            MediaRecord(label="A", serial=1), EntryResult(files_copied=4)
        )

        restored = Job.from_dict(json.loads(json.dumps(job.to_dict())))
        reloaded = restored.entries[1]

        self.assertTrue(reloaded.collecting)
        self.assertEqual(reloaded.disc_count, 1)
        self.assertEqual(reloaded.collected[0].media.label, "A")
        self.assertEqual(reloaded.collected[0].files_copied, 4)
        self.assertEqual(restored.next_pending().entry_id, entry.entry_id)

    def test_setting_it_on_a_missing_entry_is_a_no_op(self) -> None:
        job = make_job(2)
        self.assertIsNone(job.set_collecting("nao-existe"))
        self.assertEqual([e.collecting for e in job.entries], [False, False])


if __name__ == "__main__":
    unittest.main()
