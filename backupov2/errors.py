"""Error taxonomy.

The whole point of this module is ``classify_os_error``: a pure function from an
exception to a policy class, so the copy loop and the state machine can decide
what to do without either of them knowing Windows error numbers. Being pure
makes it exhaustively table-testable, which matters because these are exactly
the paths that are painful to reproduce with real hardware.
"""

from __future__ import annotations

import errno
from enum import Enum


class BackupCancelled(Exception):
    """Raised when the user cancels an in-flight copy or rip.

    Carries the partial ``CopyResult`` when there is one. A copy stopped
    part-way still knows which files had failed, and that list is exactly what
    a defect report for a bad disc is made of - discarding it on the way out
    would mean the report could only say "it went wrong".
    """

    def __init__(self, result=None) -> None:
        super().__init__()
        self.result = result


class SourceLost(OSError):
    """The disc went away mid-copy (removed, or the drive stopped responding)."""


class ErrorClass(str, Enum):
    TRANSIENT_DEST = "transient_dest"
    SOURCE_MEDIA = "source_media"
    SOURCE_LOST = "source_lost"
    DEST_FULL = "dest_full"
    PATH_TOO_LONG = "path_too_long"
    PERMISSION = "permission"
    UNKNOWN = "unknown"


# Windows system error codes, grouped by how we react to them.
TRANSIENT_DEST_WINERRORS = frozenset(
    {
        53,    # ERROR_BAD_NETPATH
        59,    # ERROR_UNEXP_NET_ERR
        64,    # ERROR_NETNAME_DELETED
        67,    # ERROR_BAD_NET_NAME
        121,   # ERROR_SEM_TIMEOUT
        1231,  # ERROR_NETWORK_UNREACHABLE
        1450,  # ERROR_NO_SYSTEM_RESOURCES
        1453,  # ERROR_WORKING_SET_QUOTA
    }
)

SOURCE_MEDIA_WINERRORS = frozenset(
    {
        21,    # ERROR_NOT_READY
        23,    # ERROR_CRC - the classic scratched disc
        433,   # ERROR_NO_SUCH_DEVICE
        1117,  # ERROR_IO_DEVICE
    }
)

DEST_FULL_WINERRORS = frozenset({112})  # ERROR_DISK_FULL
PATH_TOO_LONG_WINERRORS = frozenset({3, 206})  # ERROR_PATH_NOT_FOUND, ERROR_FILENAME_EXCED_RANGE
PERMISSION_WINERRORS = frozenset({5})  # ERROR_ACCESS_DENIED

# POSIX fallbacks. Only reachable when the Windows code is absent, which in
# practice means our own tests raising synthetic OSErrors.
_ERRNO_CLASSES = {
    errno.ENOSPC: ErrorClass.DEST_FULL,
    errno.EACCES: ErrorClass.PERMISSION,
    errno.EPERM: ErrorClass.PERMISSION,
    errno.ENAMETOOLONG: ErrorClass.PATH_TOO_LONG,
    errno.EIO: ErrorClass.SOURCE_MEDIA,
    errno.ENODEV: ErrorClass.SOURCE_LOST,
}


def classify_os_error(exc: BaseException) -> ErrorClass:
    """Map an exception onto the policy class that decides how we react."""
    if isinstance(exc, SourceLost):
        return ErrorClass.SOURCE_LOST
    if not isinstance(exc, OSError):
        return ErrorClass.UNKNOWN

    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        if winerror in TRANSIENT_DEST_WINERRORS:
            return ErrorClass.TRANSIENT_DEST
        if winerror in SOURCE_MEDIA_WINERRORS:
            return ErrorClass.SOURCE_MEDIA
        if winerror in DEST_FULL_WINERRORS:
            return ErrorClass.DEST_FULL
        if winerror in PATH_TOO_LONG_WINERRORS:
            return ErrorClass.PATH_TOO_LONG
        if winerror in PERMISSION_WINERRORS:
            return ErrorClass.PERMISSION
        return ErrorClass.UNKNOWN

    if exc.errno is not None:
        return _ERRNO_CLASSES.get(exc.errno, ErrorClass.UNKNOWN)
    return ErrorClass.UNKNOWN


def is_retryable_source(error_class: ErrorClass) -> bool:
    """Per-file retry applies to flaky reads, not to a disc that is simply gone."""
    return error_class in (ErrorClass.SOURCE_MEDIA, ErrorClass.TRANSIENT_DEST)


def describe(error_class: ErrorClass) -> str:
    """pt-BR one-liner for logs and the Problemas tab."""
    return _DESCRIPTIONS.get(error_class, "Erro desconhecido")


_DESCRIPTIONS = {
    ErrorClass.TRANSIENT_DEST: "Destino temporariamente indisponível",
    ErrorClass.SOURCE_MEDIA: "Falha de leitura no disco",
    ErrorClass.SOURCE_LOST: "Disco removido ou drive não responde",
    ErrorClass.DEST_FULL: "Espaço insuficiente no destino",
    ErrorClass.PATH_TOO_LONG: "Caminho longo demais",
    ErrorClass.PERMISSION: "Permissão negada",
    ErrorClass.UNKNOWN: "Erro desconhecido",
}
