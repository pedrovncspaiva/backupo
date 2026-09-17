"""Table-driven coverage of the error taxonomy.

These are the branches that decide whether a disc survives a bad sector, so they
are worth pinning exhaustively - and they are far cheaper to check here than to
reproduce with damaged media.
"""

import errno
import unittest

from backupov2.errors import (
    BackupCancelled,
    ErrorClass,
    SourceLost,
    classify_os_error,
    describe,
    is_retryable_source,
)


def win_error(code: int, message: str = "test") -> OSError:
    """OSError carrying a real winerror, the way Windows raises them."""
    return OSError(0, message, None, code)


class ClassifyWinErrorTests(unittest.TestCase):
    CASES = [
        # Network destination dropped - retry the whole disc, never skip files.
        (53, ErrorClass.TRANSIENT_DEST),
        (59, ErrorClass.TRANSIENT_DEST),
        (64, ErrorClass.TRANSIENT_DEST),
        (67, ErrorClass.TRANSIENT_DEST),
        (121, ErrorClass.TRANSIENT_DEST),
        (1231, ErrorClass.TRANSIENT_DEST),
        (1450, ErrorClass.TRANSIENT_DEST),
        (1453, ErrorClass.TRANSIENT_DEST),
        # Flaky disc reads - retry, then skip the file.
        (21, ErrorClass.SOURCE_MEDIA),
        (23, ErrorClass.SOURCE_MEDIA),
        (433, ErrorClass.SOURCE_MEDIA),
        (1117, ErrorClass.SOURCE_MEDIA),
        # Terminal conditions.
        (112, ErrorClass.DEST_FULL),
        (3, ErrorClass.PATH_TOO_LONG),
        (206, ErrorClass.PATH_TOO_LONG),
        (5, ErrorClass.PERMISSION),
        # Anything unmapped must not be silently treated as retryable.
        (1, ErrorClass.UNKNOWN),
        (9999, ErrorClass.UNKNOWN),
    ]

    def test_every_mapped_winerror(self) -> None:
        for code, expected in self.CASES:
            with self.subTest(winerror=code):
                self.assertIs(classify_os_error(win_error(code)), expected)

    def test_winerror_wins_over_derived_errno(self) -> None:
        """Windows derives an errno that can contradict the winerror.

        OSError(0, msg, None, 23) reports errno 13 (EACCES) but winerror 23
        (CRC). Classifying on the errno would call a scratched disc a permission
        problem and skip the retry that usually recovers it.
        """
        exc = win_error(23)
        self.assertEqual(exc.errno, errno.EACCES)
        self.assertIs(classify_os_error(exc), ErrorClass.SOURCE_MEDIA)


class ClassifyPosixTests(unittest.TestCase):
    def test_errno_fallback(self) -> None:
        for code, expected in [
            (errno.ENOSPC, ErrorClass.DEST_FULL),
            (errno.EACCES, ErrorClass.PERMISSION),
            (errno.EPERM, ErrorClass.PERMISSION),
            (errno.ENAMETOOLONG, ErrorClass.PATH_TOO_LONG),
            (errno.EIO, ErrorClass.SOURCE_MEDIA),
            (errno.ENODEV, ErrorClass.SOURCE_LOST),
        ]:
            with self.subTest(errno=code):
                self.assertIs(classify_os_error(OSError(code, "test")), expected)

    def test_unmapped_errno_is_unknown(self) -> None:
        self.assertIs(classify_os_error(OSError(errno.EEXIST, "test")), ErrorClass.UNKNOWN)


class ClassifyOtherTests(unittest.TestCase):
    def test_source_lost_is_recognised_by_type(self) -> None:
        self.assertIs(classify_os_error(SourceLost("gone")), ErrorClass.SOURCE_LOST)

    def test_non_oserror_is_unknown(self) -> None:
        self.assertIs(classify_os_error(ValueError("nope")), ErrorClass.UNKNOWN)
        self.assertIs(classify_os_error(BackupCancelled()), ErrorClass.UNKNOWN)

    def test_bare_oserror_is_unknown(self) -> None:
        self.assertIs(classify_os_error(OSError("no code at all")), ErrorClass.UNKNOWN)


class PolicyTests(unittest.TestCase):
    def test_only_flaky_classes_retry(self) -> None:
        self.assertTrue(is_retryable_source(ErrorClass.SOURCE_MEDIA))
        self.assertTrue(is_retryable_source(ErrorClass.TRANSIENT_DEST))
        for cls in (
            ErrorClass.SOURCE_LOST,
            ErrorClass.DEST_FULL,
            ErrorClass.PERMISSION,
            ErrorClass.UNKNOWN,
        ):
            with self.subTest(cls=cls):
                self.assertFalse(is_retryable_source(cls))

    def test_every_class_has_a_ptbr_description(self) -> None:
        for cls in ErrorClass:
            with self.subTest(cls=cls):
                self.assertTrue(describe(cls))


if __name__ == "__main__":
    unittest.main()
