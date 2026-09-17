r"""Long-path handling.

Getting the \\?\ prefix wrong is silent and destructive: the prefix disables all
path normalisation, so a relative or dot-containing path would resolve somewhere
other than intended rather than raising. Hence the explicit rejections.
"""

import unittest
from pathlib import PureWindowsPath

from backupov2.winapi import (
    LONG_PATH_PREFIX,
    LONG_PATH_UNC_PREFIX,
    MAX_PATH,
    long_path,
    longest_destination_path,
    needs_long_path,
    safe_path,
)


class LongPathTests(unittest.TestCase):
    def test_drive_letter(self) -> None:
        self.assertEqual(
            long_path(r"Z:\Backups\Disco 01"),
            LONG_PATH_PREFIX + r"Z:\Backups\Disco 01",
        )

    def test_unc_share(self) -> None:
        self.assertEqual(
            long_path(r"\\server\share\Backups"),
            LONG_PATH_UNC_PREFIX + r"server\share\Backups",
        )

    def test_already_prefixed_is_unchanged(self) -> None:
        for already in (
            LONG_PATH_PREFIX + r"Z:\a",
            LONG_PATH_UNC_PREFIX + r"server\share\a",
        ):
            with self.subTest(path=already):
                self.assertEqual(long_path(already), already)

    def test_relative_is_rejected(self) -> None:
        for bad in ("Backups", r"Backups\Disco 01", "\\relative-to-drive"):
            with self.subTest(path=bad):
                with self.assertRaises(ValueError):
                    long_path(bad)

    def test_parent_segments_are_rejected(self) -> None:
        """.. must raise: the prefix disables normalisation, so Windows would
        take the path literally and land somewhere other than intended."""
        with self.assertRaises(ValueError):
            long_path(r"Z:\Backups\..\Other")

    def test_single_dot_is_normalised_away(self) -> None:
        """pathlib drops "." while parsing, so it never reaches the prefix."""
        self.assertEqual(
            long_path(r"Z:\Backups\.\Disco"),
            LONG_PATH_PREFIX + r"Z:\Backups\Disco",
        )

    def test_forward_slashes_are_normalised(self) -> None:
        self.assertEqual(
            long_path("Z:/Backups/Disco 01"),
            LONG_PATH_PREFIX + r"Z:\Backups\Disco 01",
        )


class NeedsLongPathTests(unittest.TestCase):
    def test_short_path_does_not(self) -> None:
        self.assertFalse(needs_long_path(r"Z:\Backups\Disco 01"))

    def test_path_at_max_does(self) -> None:
        long = "Z:\\" + "a" * (MAX_PATH - 3)
        self.assertEqual(len(long), MAX_PATH)
        self.assertTrue(needs_long_path(long))

    def test_already_prefixed_never_does(self) -> None:
        self.assertFalse(needs_long_path(LONG_PATH_PREFIX + "Z:\\" + "a" * 400))


class SafePathTests(unittest.TestCase):
    def test_short_path_stays_readable(self) -> None:
        """Short paths pass through untouched so tracebacks stay legible."""
        plain = r"Z:\Backups\Disco 01\file.txt"
        self.assertEqual(safe_path(plain), plain)

    def test_long_path_is_upgraded(self) -> None:
        long = "Z:\\Backups\\" + "deep\\" * 60 + "file.txt"
        self.assertGreater(len(long), MAX_PATH)
        self.assertTrue(safe_path(long).startswith(LONG_PATH_PREFIX + "Z:"))

    def test_long_relative_path_is_left_alone(self) -> None:
        """Nothing safe can be done, so let the OS raise rather than guess."""
        relative = "deep\\" * 60 + "file.txt"
        self.assertEqual(safe_path(relative), relative)


class LongestDestinationPathTests(unittest.TestCase):
    def test_reports_deepest_combined_length(self) -> None:
        root = PureWindowsPath(r"Z:\FILE_SERVER\Backups\Projeto 2019\Disco 01")
        relatives = [
            PureWindowsPath("a.txt"),
            PureWindowsPath(r"VIDEO_TS\VTS_01_3.VOB"),
        ]
        expected = len(str(root)) + 1 + len(r"VIDEO_TS\VTS_01_3.VOB")
        self.assertEqual(longest_destination_path(root, relatives), expected)

    def test_empty_disc_is_just_the_root(self) -> None:
        root = PureWindowsPath(r"Z:\Backups")
        self.assertEqual(longest_destination_path(root, []), len(str(root)))


if __name__ == "__main__":
    unittest.main()
