"""Core tests: name validation, disc scanning, and the copy engine."""

import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from threading import Event

from backupov2.core import (
    CHUNKED_COPY_THRESHOLD,
    DEFECT_REPORT_NAME,
    CopyOptions,
    FileFailure,
    write_defect_report,
    ensure_entry_folders,
    copy_tree,
    ensure_folders,
    parse_subfolders,
    scan_tree,
    validate_folder_name,
)
from backupov2.errors import BackupCancelled, SourceLost


class FolderNameTests(unittest.TestCase):
    def test_parses_ordered_unique_names(self) -> None:
        self.assertEqual(
            parse_subfolders("Disco 01\n\nDisco 02\n"), ["Disco 01", "Disco 02"]
        )

    def test_blank_input_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_subfolders("   \n\n")

    def test_rejects_invalid_characters(self) -> None:
        for bad in ("bad/name", "bad:name", "bad?name", "bad|name"):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    validate_folder_name(bad)

    def test_rejects_reserved_windows_names(self) -> None:
        for bad in ("CON", "PRN", "COM1", "LPT9", "nul.txt"):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    validate_folder_name(bad)

    def test_rejects_trailing_dot_or_space(self) -> None:
        for bad in ("Disco.", "Disco ", " Disco"):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    validate_folder_name(bad)

    def test_rejects_duplicates_case_insensitively(self) -> None:
        with self.assertRaises(ValueError):
            parse_subfolders("Disco\ndisco")


# --------------------------------------------------------------------------
# New in v2
# --------------------------------------------------------------------------


def win_error(code: int, message: str) -> OSError:
    """Build an OSError carrying a real ``winerror``.

    OSError(code, message) sets *errno*, not winerror - so a two-arg construction
    silently produces an exception the classifier rightly refuses to recognise.
    Windows raises these with the 4-arg form.
    """
    return OSError(0, message, None, code)


def _make_disc(root: Path, files: dict[str, bytes]) -> None:
    for name, payload in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


class BasicCopyTests(unittest.TestCase):
    def test_rejects_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            with self.assertRaises(OSError):
                copy_tree(missing, Path(temporary), Event(), lambda progress: None)

    def test_copies_files_and_preserves_empty_directories(self) -> None:
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, destination = Path(src), Path(dst)
            (source / "nested" / "empty").mkdir(parents=True)
            (source / "nested" / "file.bin").write_bytes(b"backup-data")
            updates = []

            result = copy_tree(source, destination, Event(), updates.append)

            self.assertEqual(result.files_copied, 1)
            self.assertEqual(result.bytes_copied, 11)
            self.assertEqual((destination / "nested" / "file.bin").read_bytes(), b"backup-data")
            self.assertTrue((destination / "nested" / "empty").is_dir())
            self.assertEqual(updates[-1].copied_bytes, 11)

    def test_creates_the_destination_if_absent(self) -> None:
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source = Path(src)
            (source / "a.txt").write_bytes(b"x")
            destination = Path(dst) / "new" / "deep"
            copy_tree(source, destination, Event(), lambda progress: None)
            self.assertTrue((destination / "a.txt").exists())


class EnsureFoldersTests(unittest.TestCase):
    def test_accepts_non_empty_target(self) -> None:
        """The rule that made resume impossible in v1 is gone."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "Project" / "Disc 1"
            target.mkdir(parents=True)
            (target / "already-copied.txt").write_text("data", encoding="utf-8")

            targets = ensure_folders(root, "Project", ["Disc 1", "Disc 2"])

            self.assertEqual(len(targets), 2)
            self.assertTrue((targets[0] / "already-copied.txt").exists())

    def test_still_rejects_bad_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                ensure_folders(Path(temporary), "Project", ["COM1"])
            with self.assertRaises(ValueError):
                ensure_folders(Path(temporary), "Project", ["Disc", "disc"])


class ScanTreeTests(unittest.TestCase):
    def test_counts_files_bytes_and_empty_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            _make_disc(source, {"a.txt": b"12345", "sub/b.txt": b"678"})
            (source / "sub" / "empty").mkdir(parents=True)

            scan = scan_tree(source)

            self.assertEqual(scan.file_count, 2)
            self.assertEqual(scan.total_bytes, 8)
            self.assertIn(Path("sub/empty"), scan.directories)

    def test_directories_are_unique(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            _make_disc(source, {"sub/deep/c.txt": b"x"})
            scan = scan_tree(source)
            self.assertEqual(len(scan.directories), len(set(scan.directories)))


class CopyPolicyTests(unittest.TestCase):
    def test_skips_and_logs_unreadable_file(self) -> None:
        """A scratched sector costs one file, not the whole disc."""
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, destination = Path(src), Path(dst)
            _make_disc(source, {"good.txt": b"ok", "bad.txt": b"xx", "also-good.txt": b"ok"})

            real_copy = shutil.copy2

            def flaky_copy(a, b, *args, **kwargs):
                if "bad.txt" in str(a):
                    raise win_error(23, "Data error (cyclic redundancy check)")
                return real_copy(a, b, *args, **kwargs)

            with unittest.mock.patch("backupov2.core.shutil.copy2", flaky_copy):
                result = copy_tree(
                    source,
                    destination,
                    Event(),
                    lambda progress: None,
                    options=CopyOptions(retry_delays=(0, 0, 0)),
                )

            self.assertEqual(result.files_copied, 2)
            self.assertEqual(result.files_failed, 1)
            self.assertEqual(result.failed_files[0].relative_path, "bad.txt")
            self.assertEqual(result.failed_files[0].winerror, 23)
            self.assertTrue((destination / "good.txt").exists())

    def test_retries_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, destination = Path(src), Path(dst)
            _make_disc(source, {"flaky.txt": b"data"})
            real_copy = shutil.copy2
            calls = {"n": 0}

            def flaky_copy(a, b, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise win_error(1117, "The request could not be performed")
                return real_copy(a, b, *args, **kwargs)

            with unittest.mock.patch("backupov2.core.shutil.copy2", flaky_copy):
                result = copy_tree(
                    source,
                    destination,
                    Event(),
                    lambda progress: None,
                    options=CopyOptions(retry_delays=(0, 0, 0)),
                )

            self.assertEqual(calls["n"], 3)
            self.assertEqual(result.files_copied, 1)
            self.assertEqual(result.files_failed, 0)

    def test_abort_policy_matches_v1(self) -> None:
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, destination = Path(src), Path(dst)
            _make_disc(source, {"bad.txt": b"xx"})

            def always_fails(a, b, *args, **kwargs):
                raise win_error(23, "Data error")

            with unittest.mock.patch("backupov2.core.shutil.copy2", always_fails):
                with self.assertRaises(OSError):
                    copy_tree(
                        source,
                        destination,
                        Event(),
                        lambda progress: None,
                        options=CopyOptions(on_file_error="abort"),
                    )

    def test_destination_failure_ends_the_disc(self) -> None:
        """Network trouble is never one file's fault."""
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, destination = Path(src), Path(dst)
            _make_disc(source, {"a.txt": b"x", "b.txt": b"y"})

            def network_gone(a, b, *args, **kwargs):
                raise win_error(64, "The specified network name is no longer available")

            with unittest.mock.patch("backupov2.core.shutil.copy2", network_gone):
                with self.assertRaises(OSError) as caught:
                    copy_tree(source, destination, Event(), lambda progress: None)
            self.assertEqual(caught.exception.winerror, 64)

    def test_resume_skips_identical_files(self) -> None:
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, destination = Path(src), Path(dst)
            _make_disc(source, {"a.txt": b"hello", "b.txt": b"world"})

            first = copy_tree(source, destination, Event(), lambda progress: None)
            self.assertEqual(first.files_copied, 2)
            self.assertEqual(first.files_skipped_existing, 0)

            second = copy_tree(source, destination, Event(), lambda progress: None)
            self.assertEqual(second.files_copied, 0)
            self.assertEqual(second.files_skipped_existing, 2)
            # Byte accounting still reports the full disc, so progress reads 100%.
            self.assertEqual(second.bytes_copied, first.bytes_copied)

    def test_cancel_midway_raises(self) -> None:
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, destination = Path(src), Path(dst)
            _make_disc(source, {f"f{i}.txt": b"data" for i in range(10)})
            cancel = Event()

            def cancel_after_two(progress):
                if progress.copied_bytes >= 8:
                    cancel.set()

            with self.assertRaises(BackupCancelled):
                copy_tree(source, destination, cancel, cancel_after_two)

    def test_source_lost_midcopy(self) -> None:
        """Pulling the disc ends the entry as SourceLost, not as N file errors.

        No mocking: the scan is taken first, then the source really is removed,
        which is exactly what a yanked disc looks like to the copy loop.
        """
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, destination = Path(src) / "disc", Path(dst)
            source.mkdir()
            _make_disc(source, {"a.txt": b"x", "b.txt": b"y"})

            scan = scan_tree(source)
            shutil.rmtree(source)  # the disc is pulled

            with self.assertRaises(SourceLost):
                copy_tree(
                    source,
                    destination,
                    Event(),
                    lambda progress: None,
                    options=CopyOptions(retry_delays=(0,)),
                    scan=scan,
                )

    def test_size_verification_catches_short_write(self) -> None:
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, destination = Path(src), Path(dst)
            _make_disc(source, {"a.txt": b"full-length-content"})

            def truncating_copy(a, b, *args, **kwargs):
                Path(b).write_bytes(b"short")

            with unittest.mock.patch("backupov2.core.shutil.copy2", truncating_copy):
                result = copy_tree(
                    source,
                    destination,
                    Event(),
                    lambda progress: None,
                    options=CopyOptions(retry_delays=(0, 0)),
                )

            self.assertEqual(result.files_failed, 1)
            self.assertIn("verification", result.failed_files[0].error.lower())


if __name__ == "__main__":
    unittest.main()


class ProgressGranularityTests(unittest.TestCase):
    """Progress must move *within* a large file, not only between files.

    shutil.copy2 copies a whole file in one opaque call, so a disc holding a
    single large image reported 0% and then 100% with nothing in between.
    """

    def percentages(self, files: dict[str, int]) -> list[int]:
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, destination = Path(src), Path(dst)
            for name, size in files.items():
                (source / name).write_bytes(b"x" * size)
            seen: list[int] = []

            def record(progress):
                total = max(1, progress.total_bytes)
                seen.append(round(progress.copied_bytes / total * 100))

            copy_tree(source, destination, Event(), record)
            for name, size in files.items():
                self.assertEqual((destination / name).stat().st_size, size)
            return seen

    def test_single_large_file_reports_intermediate_progress(self) -> None:
        seen = self.percentages({"imagem.iso": CHUNKED_COPY_THRESHOLD * 6})
        between = [value for value in seen if 0 < value < 100]
        self.assertGreaterEqual(len(between), 4, f"progresso sem passos: {seen}")
        self.assertEqual(seen[-1], 100)

    def test_progress_never_goes_backwards(self) -> None:
        seen = self.percentages(
            {"a.bin": CHUNKED_COPY_THRESHOLD * 3, "b.bin": 1000, "c.bin": CHUNKED_COPY_THRESHOLD * 2}
        )
        self.assertEqual(seen, sorted(seen), f"progresso retrocedeu: {seen}")
        self.assertEqual(seen[-1], 100)

    def test_small_files_still_report_once_each(self) -> None:
        seen = self.percentages({f"f{i}.txt": 100 for i in range(5)})
        self.assertEqual(seen[-1], 100)
        self.assertGreaterEqual(len(seen), 5)

    def test_metadata_is_preserved_on_the_chunked_path(self) -> None:
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, destination = Path(src), Path(dst)
            big = source / "big.bin"
            big.write_bytes(b"y" * (CHUNKED_COPY_THRESHOLD * 2))
            copy_tree(source, destination, Event(), lambda p: None)
            copied = destination / "big.bin"
            self.assertEqual(copied.read_bytes(), big.read_bytes())
            self.assertAlmostEqual(copied.stat().st_mtime, big.stat().st_mtime, places=2)

    def test_cancel_takes_effect_partway_through_a_large_file(self) -> None:
        """Previously a cancel waited for a multi-gigabyte file to finish."""
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, destination = Path(src), Path(dst)
            (source / "huge.bin").write_bytes(b"z" * (CHUNKED_COPY_THRESHOLD * 8))
            cancel = Event()
            seen = []

            def record(progress):
                seen.append(progress.copied_bytes)
                if len(seen) == 2:
                    cancel.set()

            with self.assertRaises(BackupCancelled):
                copy_tree(source, destination, cancel, record)

            # It stopped early rather than finishing the file.
            written = (destination / "huge.bin").stat().st_size
            self.assertLess(written, CHUNKED_COPY_THRESHOLD * 8)


class EnsureEntryFoldersTests(unittest.TestCase):
    """The three-level layout a delivery sheet describes."""

    def test_creates_the_group_level(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            created = ensure_entry_folders(
                root,
                "CAIXA 02",
                [("EG 1841", "Disco A"), ("EG 1841", "Disco B"), ("EG 1885", "Disco C")],
            )
            self.assertTrue((root / "CAIXA 02" / "EG 1841" / "Disco A").is_dir())
            self.assertTrue((root / "CAIXA 02" / "EG 1885" / "Disco C").is_dir())
            self.assertEqual(len(created), 3)

    def test_empty_group_stays_flat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ensure_entry_folders(root, "CAIXA 02", [("", "Disco avulso")])
            self.assertTrue((root / "CAIXA 02" / "Disco avulso").is_dir())

    def test_rejects_an_invalid_group_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                ensure_entry_folders(Path(temporary), "CAIXA", [("EG/1841", "Disco")])

    def test_tolerates_folders_that_already_hold_files(self) -> None:
        """The normal state when resuming a batch."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "CAIXA 02" / "EG 1841" / "Disco A"
            existing.mkdir(parents=True)
            (existing / "copiado.txt").write_text("x", encoding="utf-8")

            ensure_entry_folders(root, "CAIXA 02", [("EG 1841", "Disco A")])

            self.assertTrue((existing / "copiado.txt").exists())


class NeverOverwriteTests(unittest.TestCase):
    """Sending a different disc into an already-completed folder must never
    lose what that folder already had - only add to it."""

    def test_colliding_name_with_different_content_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as src1, tempfile.TemporaryDirectory() as src2,              tempfile.TemporaryDirectory() as dst:
            source_a, source_b, destination = Path(src1), Path(src2), Path(dst)
            (source_a / "info.txt").write_bytes(b"do disco A")
            copy_tree(source_a, destination, Event(), lambda p: None)

            (source_b / "info.txt").write_bytes(b"do disco B - conteudo diferente")
            result = copy_tree(source_b, destination, Event(), lambda p: None)

            self.assertEqual((destination / "info.txt").read_bytes(), b"do disco A")
            self.assertTrue((destination / "info (2).txt").exists())
            self.assertEqual(
                (destination / "info (2).txt").read_bytes(),
                b"do disco B - conteudo diferente",
            )
            self.assertEqual(result.files_renamed, 1)
            self.assertEqual(result.renamed_files[0].relative_path, "info.txt")
            self.assertEqual(result.renamed_files[0].saved_as, "info (2).txt")

    def test_a_third_collision_gets_the_next_number(self) -> None:
        with tempfile.TemporaryDirectory() as dst:
            destination = Path(dst)
            (destination / "a.txt").write_bytes(b"primeiro")
            (destination / "a (2).txt").write_bytes(b"segundo")
            with tempfile.TemporaryDirectory() as src:
                source = Path(src)
                (source / "a.txt").write_bytes(b"terceiro, diferente dos outros")
                copy_tree(source, destination, Event(), lambda p: None)

            self.assertEqual((destination / "a.txt").read_bytes(), b"primeiro")
            self.assertEqual((destination / "a (2).txt").read_bytes(), b"segundo")
            self.assertEqual(
                (destination / "a (3).txt").read_bytes(),
                b"terceiro, diferente dos outros",
            )

    def test_files_that_only_exist_in_the_old_disc_are_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as src1, tempfile.TemporaryDirectory() as src2,              tempfile.TemporaryDirectory() as dst:
            source_a, source_b, destination = Path(src1), Path(src2), Path(dst)
            _make_disc(source_a, {"only_in_a.txt": b"x", "shared.txt": b"same"})
            copy_tree(source_a, destination, Event(), lambda p: None)

            _make_disc(source_b, {"shared.txt": b"same", "only_in_b.txt": b"y"})
            copy_tree(source_b, destination, Event(), lambda p: None)

            self.assertTrue((destination / "only_in_a.txt").exists())
            self.assertTrue((destination / "only_in_b.txt").exists())
            # Identical content and a fresh mtime is treated as the same file,
            # not a collision - no "(2)" is created for it.
            self.assertFalse((destination / "shared (2).txt").exists())

    def test_no_extension_names_get_the_suffix_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as src1, tempfile.TemporaryDirectory() as src2,              tempfile.TemporaryDirectory() as dst:
            source_a, source_b, destination = Path(src1), Path(src2), Path(dst)
            (source_a / "README").write_bytes(b"versao A")
            copy_tree(source_a, destination, Event(), lambda p: None)
            (source_b / "README").write_bytes(b"versao B, diferente")
            copy_tree(source_b, destination, Event(), lambda p: None)

            self.assertTrue((destination / "README (2)").exists())
            self.assertEqual((destination / "README (2)").read_bytes(), b"versao B, diferente")

    def test_resume_of_the_same_disc_still_skips_rather_than_duplicates(self) -> None:
        """The collision guard must not turn an ordinary resume into a rename."""
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, destination = Path(src), Path(dst)
            _make_disc(source, {"a.txt": b"conteudo", "b.txt": b"outro"})

            first = copy_tree(source, destination, Event(), lambda p: None)
            second = copy_tree(source, destination, Event(), lambda p: None)

            self.assertEqual(first.files_renamed, 0)
            self.assertEqual(second.files_renamed, 0)
            self.assertEqual(second.files_skipped_existing, 2)
            self.assertFalse((destination / "a (2).txt").exists())


class PauseTests(unittest.TestCase):
    """Holding a copy in place, and letting go of it again.

    The sleep function is injected, so these drive the real hold loop without
    threads or wall-clock time: the fake sleep is what decides when the pause
    ends, which makes the whole thing deterministic.
    """

    def build(self, files: dict[str, int]):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        source = Path(temporary.name) / "src"
        destination = Path(temporary.name) / "dst"
        source.mkdir()
        for name, size in files.items():
            (source / name).write_bytes(b"x" * size)
        return source, destination

    def test_a_paused_copy_holds_and_then_carries_on(self) -> None:
        source, destination = self.build({"a.txt": 10, "b.txt": 10})
        pause, cancel = Event(), Event()
        pause.set()
        naps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            naps.append(seconds)
            if len(naps) >= 3:
                pause.clear()

        result = copy_tree(
            source, destination, cancel, lambda p: None, sleep=fake_sleep, pause=pause
        )

        self.assertEqual(len(naps), 3)  # held, then released
        self.assertEqual(result.files_copied, 2)
        self.assertEqual((destination / "b.txt").read_bytes(), b"x" * 10)

    def test_no_pause_event_means_the_copy_never_waits(self) -> None:
        source, destination = self.build({"a.txt": 10})
        naps: list[float] = []

        result = copy_tree(
            source, destination, Event(), lambda p: None, sleep=naps.append
        )

        self.assertEqual(naps, [])
        self.assertEqual(result.files_copied, 1)

    def test_cancelling_while_paused_stops_without_waiting_for_a_resume(self) -> None:
        """Otherwise a cancel would sit behind a resume that may never come."""
        source, destination = self.build({"a.txt": 10, "b.txt": 10})
        pause, cancel = Event(), Event()
        pause.set()
        naps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            naps.append(seconds)
            if len(naps) >= 2:
                cancel.set()  # pause is deliberately left set

        with self.assertRaises(BackupCancelled):
            copy_tree(
                source,
                destination,
                cancel,
                lambda p: None,
                sleep=fake_sleep,
                pause=pause,
            )
        self.assertEqual(len(naps), 2)
        self.assertTrue(pause.is_set())

    def test_a_large_file_pauses_mid_transfer(self) -> None:
        """Between files is not enough: a single big file is exactly the case
        worth pausing, and it would otherwise ignore the request until done."""
        source, destination = self.build({"grande.bin": CHUNKED_COPY_THRESHOLD * 4})
        pause, cancel = Event(), Event()
        pause.set()
        naps: list[float] = []
        sizes: list[int] = []

        def fake_sleep(seconds: float) -> None:
            naps.append(seconds)
            try:
                sizes.append((destination / "grande.bin").stat().st_size)
            except OSError:
                sizes.append(-1)
            if len(naps) % 2 == 0:
                pause.clear()

        def repause(progress) -> None:
            pause.set()

        result = copy_tree(
            source,
            destination,
            cancel,
            repause,
            sleep=fake_sleep,
            pause=pause,
        )

        # It was held partway through the file, not only before or after it.
        self.assertTrue(any(0 < size < CHUNKED_COPY_THRESHOLD * 4 for size in sizes))
        self.assertEqual(result.files_copied, 1)
        self.assertEqual(
            (destination / "grande.bin").stat().st_size, CHUNKED_COPY_THRESHOLD * 4
        )


class TroubleReportingTests(unittest.TestCase):
    """What the copy engine tells the outside world about a disc going badly."""

    def build(self, good: int = 2, bad: int = 3):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        source = Path(temporary.name) / "src"
        destination = Path(temporary.name) / "dst"
        source.mkdir()
        unreadable = set()
        for index in range(good + bad):
            name = f"f{index:02d}.bin"
            (source / name).write_bytes(b"x" * 100)
            if index >= good:
                unreadable.add(name)

        real = shutil.copy2

        def flaky(src, dst, *args, **kwargs):
            if Path(str(src)).name in unreadable:
                raise OSError(23, "Data error (cyclic redundancy check)")
            return real(src, dst, *args, **kwargs)

        patch = unittest.mock.patch("backupov2.core.shutil.copy2", flaky)
        patch.start()
        self.addCleanup(patch.stop)
        return source, destination

    def test_progress_carries_the_running_failure_count(self) -> None:
        """The count has to ride the heartbeat: waiting for the final result
        to learn a disc is bad means waiting out the whole disc."""
        source, destination = self.build(good=2, bad=3)
        seen: list[int] = []

        copy_tree(
            source,
            destination,
            Event(),
            lambda progress: seen.append(progress.files_failed),
            CopyOptions(retry_delays=()),
        )

        self.assertEqual(max(seen), 3)
        self.assertEqual(seen[0], 0)  # starts clean
        self.assertEqual(sorted(set(seen)), [0, 1, 2, 3])  # climbs one at a time

    def test_progress_carries_the_copied_count_too(self) -> None:
        source, destination = self.build(good=2, bad=1)
        seen: list[int] = []

        copy_tree(
            source,
            destination,
            Event(),
            lambda progress: seen.append(progress.files_copied),
            CopyOptions(retry_delays=()),
        )

        self.assertEqual(max(seen), 2)

    def test_a_cancelled_copy_hands_back_what_it_had_achieved(self) -> None:
        """The defect report is made of this list; discarding it on the way
        out would leave the report able to say only "it went wrong"."""
        source, destination = self.build(good=2, bad=3)
        cancel = Event()
        calls = []

        def stop_after_a_while(progress):
            calls.append(progress)
            if progress.files_failed >= 2:
                cancel.set()

        with self.assertRaises(BackupCancelled) as caught:
            copy_tree(
                source,
                destination,
                cancel,
                stop_after_a_while,
                CopyOptions(retry_delays=()),
            )

        result = caught.exception.result
        self.assertIsNotNone(result)
        self.assertTrue(result.partial)
        self.assertEqual(result.files_copied, 2)
        self.assertEqual(result.files_failed, 2)
        self.assertEqual(
            [failure.relative_path for failure in result.failed_files],
            ["f02.bin", "f03.bin"],
        )


class DefectReportTests(unittest.TestCase):
    def folder(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name) / "Disco 07"

    def test_it_names_the_disc_and_lists_the_unreadable_files(self) -> None:
        report = write_defect_report(
            folder=self.folder(),
            disc_name="PROJ2019_07 (D:)",
            reason="2 arquivo(s) nao puderam ser lidos",
            files_copied=812,
            failed_files=[
                FileFailure("VIDEO_TS/VTS_01_3.VOB", "[WinError 23] CRC", "source_media", 23),
                FileFailure("LEIAME.TXT", "[WinError 23] CRC", "source_media", 23),
            ],
            when="2026-09-17 14:32:05",
            serial=0x499602D2,
        )

        text = report.read_text(encoding="utf-8")
        self.assertEqual(report.name, DEFECT_REPORT_NAME)
        self.assertIn("PROJ2019_07 (D:)", text)
        self.assertIn("0x499602D2", text)
        self.assertIn("812", text)
        self.assertIn("VIDEO_TS/VTS_01_3.VOB", text)
        self.assertIn("LEIAME.TXT", text)
        self.assertIn("INCOMPLETO", text)

    def test_it_creates_the_folder_if_the_copy_never_got_that_far(self) -> None:
        folder = self.folder()
        self.assertFalse(folder.exists())

        write_defect_report(
            folder=folder,
            disc_name="CD (D:)",
            reason="a unidade parou de responder durante a copia",
            files_copied=0,
            failed_files=[],
            when="2026-09-17 14:32:05",
        )

        self.assertTrue((folder / DEFECT_REPORT_NAME).is_file())

    def test_it_says_so_when_no_individual_file_was_identified(self) -> None:
        report = write_defect_report(
            folder=self.folder(),
            disc_name="CD (D:)",
            reason="a unidade parou de responder durante a copia",
            files_copied=0,
            failed_files=[],
            when="2026-09-17 14:32:05",
        )

        text = report.read_text(encoding="utf-8")
        self.assertIn("parou de responder", text)
        self.assertNotIn("ARQUIVOS QUE NAO PUDERAM SER LIDOS", text)

    def test_it_accepts_the_serialised_failure_shape_too(self) -> None:
        """Entries store failures as plain dicts once saved, so a report
        rebuilt from a loaded job must not need the dataclass."""
        report = write_defect_report(
            folder=self.folder(),
            disc_name="CD (D:)",
            reason="1 arquivo(s) nao puderam ser lidos",
            files_copied=3,
            failed_files=[{"path": "DADOS/x.bin", "error": "[WinError 23] CRC"}],
            when="2026-09-17 14:32:05",
        )

        self.assertIn("DADOS/x.bin", report.read_text(encoding="utf-8"))


class SkippedDefectReportTests(unittest.TestCase):
    """The report's other voice: a disc written off without being read.

    Someone opening the folder months later needs to know which of the two
    happened, because one means "some of it is here" and the other means
    "none of it is".
    """

    def folder(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name) / "Disco 07"

    def write(self, **overrides) -> str:
        arguments = dict(
            folder=self.folder(),
            disc_name="(nao lido)",
            reason="marcado manualmente como disco defeituoso",
            files_copied=0,
            failed_files=[],
            when="2026-09-17 14:32:05",
            skipped=True,
        )
        arguments.update(overrides)
        return write_defect_report(**arguments).read_text(encoding="utf-8")

    def test_it_says_the_folder_was_skipped_and_nothing_copied(self) -> None:
        text = self.write()
        self.assertIn("PULADA", text)
        self.assertIn("NENHUM ARQUIVO FOI COPIADO", text)
        self.assertNotIn("durante a copia", text)

    def test_a_part_copied_folder_is_called_incomplete_instead(self) -> None:
        text = self.write(files_copied=42)
        self.assertIn("INCOMPLETO", text)
        self.assertNotIn("NENHUM ARQUIVO FOI COPIADO", text)

    def test_the_note_is_included(self) -> None:
        text = self.write(note="disco trincado ao meio")
        self.assertIn("Observacao", text)
        self.assertIn("disco trincado ao meio", text)

    def test_a_multi_line_note_stays_aligned(self) -> None:
        text = self.write(note="primeira linha\nsegunda linha")
        self.assertIn("primeira linha", text)
        self.assertIn(f"{' ' * 30}segunda linha", text)

    def test_an_empty_note_adds_no_empty_field(self) -> None:
        self.assertNotIn("Observacao", self.write(note="   \n  "))

    def test_the_mid_copy_wording_is_untouched(self) -> None:
        text = self.write(skipped=False, files_copied=5)
        self.assertIn("durante a copia", text)
        self.assertNotIn("PULADA", text)
