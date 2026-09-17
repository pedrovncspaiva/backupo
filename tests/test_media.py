"""Media identity, the scanner seam, and disc-kind detection.

None of this needs an optical drive, which is the point: v1 called ctypes
directly inside its media lookup, so every one of these behaviours could only be
checked by physically feeding discs into a drive.
"""

import tempfile
import unittest
from pathlib import Path

from backupov2.jobmodel import DiscKind
from backupov2.media import (
    FakeScanner,
    MatchStrength,
    MediaInfo,
    detect_disc_kind,
    identity_match,
    same_media,
)


def disc(label="PROJ_01", serial=1234, root="D:\\", fs="CDFS") -> MediaInfo:
    return MediaInfo(root=Path(root), label=label, serial=serial, fs_name=fs)


class MediaInfoTests(unittest.TestCase):
    def test_drive_letter_form(self) -> None:
        self.assertEqual(disc().drive, "D:")

    def test_display_name(self) -> None:
        self.assertEqual(disc().display_name, "PROJ_01 (D:)")

    def test_unlabelled_display_name(self) -> None:
        self.assertEqual(disc(label="").display_name, "Disco sem rotulo (D:)")

    def test_to_record_carries_identity_and_totals(self) -> None:
        record = disc().to_record(file_count=812, total_bytes=4294901760)
        self.assertEqual(record.drive, "D:")
        self.assertEqual(record.serial, 1234)
        self.assertEqual(record.file_count, 812)
        self.assertEqual(record.to_dict()["serial_hex"], "0x000004D2")


class IdentityMatchTests(unittest.TestCase):
    def test_equal_serials_are_strong(self) -> None:
        self.assertIs(identity_match(disc(), disc()), MatchStrength.STRONG)

    def test_different_serials_never_match(self) -> None:
        self.assertIs(identity_match(disc(serial=1), disc(serial=2)), MatchStrength.NONE)

    def test_serial_wins_over_a_matching_label(self) -> None:
        """Two discs can share a label and still be different discs."""
        self.assertIs(
            identity_match(disc(serial=1), disc(serial=2)),
            MatchStrength.NONE,
        )

    def test_missing_serials_fall_back_to_label(self) -> None:
        self.assertIs(
            identity_match(disc(serial=None), disc(serial=None)),
            MatchStrength.WEAK,
        )

    def test_two_unlabelled_discs_in_one_drive_are_weak(self) -> None:
        """v1 treated this as 'the same disc', so a second unlabelled disc in
        the same drive looked like the previous one never being removed."""
        a = disc(label="", serial=None)
        b = disc(label="", serial=None)
        self.assertIs(identity_match(a, b), MatchStrength.WEAK)

    def test_none_operands(self) -> None:
        self.assertIs(identity_match(None, disc()), MatchStrength.NONE)
        self.assertIs(identity_match(disc(), None), MatchStrength.NONE)

    def test_same_media_helper(self) -> None:
        self.assertTrue(same_media(disc(), disc()))
        self.assertFalse(same_media(disc(serial=1), disc(serial=2)))


class FakeScannerTests(unittest.TestCase):
    def test_replays_the_script(self) -> None:
        a, b = disc(serial=1), disc(serial=2)
        scanner = FakeScanner([[], [a], [a], [], [b]])
        seen = [scanner.scan() for _ in range(5)]
        self.assertEqual([len(step) for step in seen], [0, 1, 1, 0, 1])
        self.assertEqual(seen[4][0].serial, 2)

    def test_last_state_repeats(self) -> None:
        """So a test only has to describe the transitions it cares about."""
        scanner = FakeScanner([[disc()]])
        for _ in range(5):
            self.assertEqual(len(scanner.scan()), 1)

    def test_empty_script_reports_no_media(self) -> None:
        self.assertEqual(FakeScanner().scan(), [])

    def test_feed_appends(self) -> None:
        scanner = FakeScanner([[]]).feed([disc()])
        self.assertEqual(scanner.scan(), [])
        self.assertEqual(len(scanner.scan()), 1)

    def test_returns_a_copy(self) -> None:
        scanner = FakeScanner([[disc()]])
        scanner.scan().clear()
        self.assertEqual(len(scanner.scan()), 1)


class DetectDiscKindTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, *names: str) -> None:
        for name in names:
            (self.root / name).write_bytes(b"x")

    def test_data_disc(self) -> None:
        self.write("readme.txt")
        (self.root / "VIDEO_TS").mkdir()
        probe = detect_disc_kind(self.root)
        self.assertIs(probe.kind, DiscKind.DATA)
        self.assertTrue(probe.confident)

    def test_audio_disc_from_cda_stubs(self) -> None:
        self.write("Track01.cda", "Track02.cda")
        probe = detect_disc_kind(self.root)
        self.assertIs(probe.kind, DiscKind.AUDIO)
        self.assertTrue(probe.confident)
        self.assertIn("2 faixa", probe.reason)

    def test_mixed_mode_is_treated_as_data(self) -> None:
        """Copying the files is the useful behaviour; ripping the audio half of
        a mixed disc is out of scope."""
        self.write("Track01.cda", "data.bin")
        probe = detect_disc_kind(self.root)
        self.assertIs(probe.kind, DiscKind.DATA)
        self.assertIn("misto", probe.reason)

    def test_empty_cdfs_is_suspected_audio_but_not_confident(self) -> None:
        probe = detect_disc_kind(self.root, fs_name="CDFS")
        self.assertIs(probe.kind, DiscKind.AUDIO)
        self.assertFalse(probe.confident)

    def test_empty_disc_without_cdfs_is_unknown(self) -> None:
        probe = detect_disc_kind(self.root, fs_name="UDF")
        self.assertIs(probe.kind, DiscKind.UNKNOWN)
        self.assertFalse(probe.confident)

    def test_unreadable_disc_is_unknown_not_an_exception(self) -> None:
        probe = detect_disc_kind(self.root / "not-there")
        self.assertIs(probe.kind, DiscKind.UNKNOWN)
        self.assertFalse(probe.confident)
        self.assertIn("ilegivel", probe.reason)

    def test_case_insensitive_cda_suffix(self) -> None:
        self.write("TRACK01.CDA")
        self.assertIs(detect_disc_kind(self.root).kind, DiscKind.AUDIO)


if __name__ == "__main__":
    unittest.main()
