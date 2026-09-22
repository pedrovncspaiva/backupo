"""Folder-name validation, disc scanning, and the copy engine.

``scan_tree`` walks a disc once and hands the result to ``copy_tree``, so the
runner can start the grace countdown as soon as the disc kind is known while the
(much slower) sizing pass fills in behind it.

``copy_tree`` owns the per-file failure policy: a scratched sector costs one
file, not the whole disc. Destination failures are different in kind and always
end the disc - a network drop is never one file's fault.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Iterable, Sequence

from .errors import BackupCancelled, ErrorClass, SourceLost, classify_os_error
from .winapi import safe_path

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}

# How often the copy loop re-checks that the disc is still in the drive. Cheap
# enough to not matter, frequent enough that pulling a disc is noticed promptly
# instead of after a long run of confusing per-file errors.
SOURCE_PROBE_EVERY = 25

# Files at or above this size are copied in chunks so the progress bar moves
# *within* a file. shutil.copy2 copies a whole file in one opaque call, so a
# disc holding one big image would otherwise jump straight from 0% to 100%.
# Chunking also lets a cancel take effect mid-file instead of waiting for a
# multi-gigabyte copy to finish.
# 1 MiB: small enough that anything big enough to visibly stall the bar gets
# chunked, while the many-tiny-files case (an ordinary document CD) keeps the
# plain copy2 path.
CHUNKED_COPY_THRESHOLD = 1024 * 1024
COPY_CHUNK_BYTES = 512 * 1024

# How often a held copy looks up to see whether it may carry on. Short enough
# that resuming feels immediate, long enough that holding costs nothing.
PAUSE_POLL_SECONDS = 0.1


@dataclass(frozen=True)
class CopyProgress:
    copied_bytes: int
    total_bytes: int
    current_file: str
    # Carried on the ordinary heartbeat so trouble is visible while the copy
    # is still running, rather than only in the result at the end - by which
    # point a disc that fails on every file has already cost an hour.
    files_copied: int = 0
    files_failed: int = 0


@dataclass(frozen=True)
class FileCollision:
    """A file that already existed at the destination with different content.

    Recorded rather than acted on silently: the incoming file is written under
    a new name and the original is left exactly as it was, so nothing a
    previous disc wrote is ever lost to a later one landing in the same folder.
    """

    relative_path: str
    saved_as: str

    def to_dict(self) -> dict:
        return {"path": self.relative_path, "saved_as": self.saved_as}


@dataclass(frozen=True)
class FileFailure:
    relative_path: str
    error: str
    error_class: str
    winerror: int | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.relative_path,
            "error": self.error,
            "error_class": self.error_class,
            "winerror": self.winerror,
        }


@dataclass(frozen=True)
class ScanResult:
    """What one walk of a disc found. Handed to the copy step so the tree is
    walked once rather than twice."""

    files: tuple[tuple[Path, int], ...]  # (path relative to source, size in bytes)
    directories: tuple[Path, ...]  # every directory, relative - preserves empty ones
    total_bytes: int

    @property
    def file_count(self) -> int:
        return len(self.files)


@dataclass
class CopyResult:
    files_copied: int = 0
    bytes_copied: int = 0
    files_failed: int = 0
    failed_files: list[FileFailure] = field(default_factory=list)
    files_skipped_existing: int = 0
    files_renamed: int = 0
    renamed_files: list[FileCollision] = field(default_factory=list)
    partial: bool = False

    def to_dict(self) -> dict:
        return {
            "files_copied": self.files_copied,
            "bytes_copied": self.bytes_copied,
            "files_failed": self.files_failed,
            "failed_files": [failure.to_dict() for failure in self.failed_files],
            "files_skipped_existing": self.files_skipped_existing,
            "files_renamed": self.files_renamed,
            "renamed_files": [item.to_dict() for item in self.renamed_files],
            "partial": self.partial,
        }


@dataclass(frozen=True)
class CopyOptions:
    """Per-file failure policy and resume behaviour.

    Default: retry a flaky read three times, then skip that file and flag the
    disc. One scratched sector must not cost the whole disc.
    """

    on_file_error: str = "retry_then_skip"  # or "abort" to fail the whole disc
    retry_delays: Sequence[float] = (0.5, 2.0, 5.0)
    resume_existing: bool = True
    verify_size: bool = True
    source_probe_every: int = SOURCE_PROBE_EVERY


def validate_folder_name(name: str) -> str:
    if not name:
        raise ValueError("Folder names cannot be empty.")
    if name != name.strip() or name.endswith("."):
        raise ValueError(f"Invalid folder name: {name!r}.")
    if INVALID_CHARS.search(name):
        raise ValueError(f"Folder name contains an invalid character: {name!r}.")
    if name.split(".", 1)[0].upper() in RESERVED_NAMES:
        raise ValueError(f"Reserved Windows folder name: {name!r}.")
    return name


def parse_subfolders(text: str) -> list[str]:
    names = [line.strip() for line in text.splitlines() if line.strip()]
    if not names:
        raise ValueError("Enter at least one subfolder name.")

    validated = [validate_folder_name(name) for name in names]
    folded = [name.casefold() for name in validated]
    if len(folded) != len(set(folded)):
        raise ValueError("Subfolder names must be unique.")
    return validated


def ensure_folders(root: Path, parent_name: str, subfolders: Iterable[str]) -> list[Path]:
    """Validate the names and create ``root/parent_name/<each>``.

    Deliberately tolerant of folders that already contain files: that is the
    normal state when resuming an interrupted batch. Deciding what to do about a
    non-empty folder belongs to ``reconcile``, which can ask the user, not to an
    exception thrown at startup.
    """
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("Choose an existing destination folder.")

    parent_name = validate_folder_name(parent_name.strip())
    validated = [validate_folder_name(name) for name in subfolders]
    folded = [name.casefold() for name in validated]
    if len(folded) != len(set(folded)):
        raise ValueError("Subfolder names must be unique.")

    parent = root / parent_name
    targets = [parent / name for name in validated]
    for target in targets:
        target.mkdir(parents=True, exist_ok=True)
    return targets


def ensure_entry_folders(
    root: Path, parent_name: str, entries: Iterable[tuple[str, str]]
) -> list[Path]:
    """Create ``root/parent_name/[group/]name`` for each (group, name) pair.

    The optional middle level is what lets a delivery sheet's CAIXA > EG > disc
    hierarchy become real directories. An empty group puts the disc folder
    directly under the parent, which is the manual-entry case.

    Like ``ensure_folders`` this tolerates folders that already hold files -
    that is the normal state when resuming a batch.
    """
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("Choose an existing destination folder.")

    parent = root / validate_folder_name(parent_name.strip())
    created: list[Path] = []
    for group, name in entries:
        target = parent
        group = (group or "").strip()
        if group:
            target = target / validate_folder_name(group)
        target = target / validate_folder_name(name)
        target.mkdir(parents=True, exist_ok=True)
        created.append(target)
    return created


def scan_tree(source: Path, cancel: Event | None = None) -> ScanResult:
    """Walk *source* once, collecting every file, size and directory.

    Split out of the copy so the runner can start the grace countdown as soon as
    the disc kind is known and let the size fill in afterwards - sizing a full
    DVD on a USB drive takes 5-20 seconds, and blocking the countdown on it
    would make the app feel broken.
    """
    if not source.is_dir():
        raise OSError(f"Disc is no longer available: {source}")

    def raise_walk_error(error: OSError) -> None:
        raise error

    files: list[tuple[Path, int]] = []
    directories: list[Path] = []
    total_bytes = 0

    for current_root, dirnames, filenames in os.walk(source, onerror=raise_walk_error):
        if cancel is not None and cancel.is_set():
            raise BackupCancelled
        current = Path(current_root)
        relative_root = current.relative_to(source)
        if relative_root != Path("."):
            directories.append(relative_root)
        for dirname in dirnames:
            directories.append(relative_root / dirname)
        for filename in filenames:
            source_file = current / filename
            try:
                size = source_file.stat().st_size
            except OSError:
                # An unreadable entry still gets copied (and probably fails
                # there, where the per-file policy can handle it properly).
                size = 0
            files.append((relative_root / filename, size))
            total_bytes += size

    # De-duplicate while preserving order: a directory shows up both as a child
    # of its parent and as its own walk root.
    seen: set[Path] = set()
    unique_directories: list[Path] = []
    for directory in directories:
        if directory not in seen:
            seen.add(directory)
            unique_directories.append(directory)

    return ScanResult(
        files=tuple(files),
        directories=tuple(unique_directories),
        total_bytes=total_bytes,
    )


def _beat(result: "CopyResult", scan: ScanResult, current: str) -> CopyProgress:
    """One heartbeat, carrying where the copy is *and* how it is going."""
    return CopyProgress(
        copied_bytes=result.bytes_copied,
        total_bytes=scan.total_bytes,
        current_file=current,
        files_copied=result.files_copied,
        files_failed=result.files_failed,
    )


def _hold(cancel: Event, pause: Event | None, sleep: Callable[[float], None]) -> None:
    """Block while the copy is paused, returning as soon as it may go on.

    Polled rather than waited on, so a cancel arriving mid-pause is acted on at
    once instead of sitting behind a resume that may never come. Callers that
    pass no pause event never enter the loop at all.
    """
    while pause is not None and pause.is_set() and not cancel.is_set():
        sleep(PAUSE_POLL_SECONDS)


def copy_tree(
    source: Path,
    destination: Path,
    cancel: Event,
    on_progress: Callable[[CopyProgress], None],
    options: CopyOptions | None = None,
    scan: ScanResult | None = None,
    sleep: Callable[[float], None] = time.sleep,
    pause: Event | None = None,
) -> CopyResult:
    """Copy every file from *source* into *destination*.

    Returns a ``CopyResult`` describing what happened, including files that were
    skipped after repeated read failures. Raises only for conditions that end
    the disc: cancellation, the disc disappearing, or ``on_file_error="abort"``.

    While *pause* is set the copy holds where it is, between files and between
    chunks of a large one, and picks up exactly there when it is cleared.
    """
    options = options or CopyOptions()
    if scan is None:
        scan = scan_tree(source, cancel)

    destination.mkdir(parents=True, exist_ok=True)
    for relative_dir in scan.directories:
        _mkdir(destination / relative_dir)

    result = CopyResult()
    on_progress(CopyProgress(0, scan.total_bytes, "Iniciando cópia..."))

    for index, (relative_path, size) in enumerate(scan.files):
        _hold(cancel, pause, sleep)
        if cancel.is_set():
            result.partial = True
            raise BackupCancelled(result)

        # Notice a pulled disc promptly rather than through a cascade of
        # per-file errors that the retry policy would patiently chew through.
        if index and index % options.source_probe_every == 0:
            if not source.is_dir():
                result.partial = True
                raise SourceLost(f"Disc is no longer available: {source}")

        source_file = source / relative_path
        destination_file = destination / relative_path

        if options.resume_existing and _files_identical(source_file, destination_file, size):
            result.files_skipped_existing += 1
            result.bytes_copied += size
            on_progress(_beat(result, scan, str(relative_path)))
            continue

        # Something is already there under this name and it is not the same
        # file (checked regardless of resume_existing, since that setting only
        # controls skipping identical files, not what happens on a genuine
        # collision). Never overwrite it: write the incoming file under a new
        # name instead, so a disc redirected into an already-completed folder
        # never costs that folder anything it already had.
        if destination_file.exists() and not _files_identical(
            source_file, destination_file, size
        ):
            resolved_file = _unique_destination(destination_file)
            result.files_renamed += 1
            result.renamed_files.append(
                FileCollision(
                    relative_path=str(relative_path),
                    saved_as=str(resolved_file.relative_to(destination)),
                )
            )
            destination_file = resolved_file

        # Absolute byte count once this file is done, so mid-file reports can be
        # expressed against the whole-disc total.
        base_bytes = result.bytes_copied

        def report(done_in_file: int, _base: int = base_bytes, _name: str = str(relative_path)) -> None:
            on_progress(
                CopyProgress(
                    _base + done_in_file,
                    scan.total_bytes,
                    _name,
                    result.files_copied,
                    result.files_failed,
                )
            )

        try:
            _copy_one(
                source_file, destination_file, size, options, sleep, cancel, report, pause
            )
        except BackupCancelled:
            # Re-raised carrying what had been achieved, so a copy stopped
            # mid-file can still account for the files that failed before it.
            result.partial = True
            raise BackupCancelled(result) from None
        except OSError as exc:
            error_class = classify_os_error(exc)
            if error_class is ErrorClass.SOURCE_LOST or not source.is_dir():
                result.partial = True
                raise SourceLost(f"Disc is no longer available: {source}") from exc
            # Destination trouble is never one file's fault - it ends the disc.
            if error_class in (ErrorClass.TRANSIENT_DEST, ErrorClass.DEST_FULL):
                result.partial = True
                raise
            if options.on_file_error == "abort":
                result.partial = True
                raise
            result.files_failed += 1
            result.failed_files.append(
                FileFailure(
                    relative_path=str(relative_path),
                    error=str(exc),
                    error_class=error_class.value,
                    winerror=getattr(exc, "winerror", None),
                )
            )
        else:
            result.files_copied += 1
            result.bytes_copied += size

        on_progress(_beat(result, scan, str(relative_path)))

    return result


def _copy_one(
    source_file: Path,
    destination_file: Path,
    size: int,
    options: CopyOptions,
    sleep: Callable[[float], None],
    cancel: Event,
    on_bytes: Callable[[int], None],
    pause: Event | None = None,
) -> None:
    """Copy one file, retrying transient read failures per the policy."""
    attempts = 1 + len(options.retry_delays) if options.on_file_error != "abort" else 1
    last_error: OSError | None = None

    for attempt in range(attempts):
        try:
            _mkdir(destination_file.parent)
            if size >= CHUNKED_COPY_THRESHOLD:
                _copy_file_chunked(
                    source_file, destination_file, cancel, on_bytes, pause, sleep
                )
            else:
                shutil.copy2(safe_path(source_file), safe_path(destination_file))
            if options.verify_size:
                copied = os.stat(safe_path(destination_file)).st_size
                if copied != size:
                    raise OSError(f"Size verification failed for {source_file.name}")
            return
        except OSError as exc:
            last_error = exc
            error_class = classify_os_error(exc)

            # A path over MAX_PATH gets exactly one retry through the extended
            # form; safe_path() already tries, so this catches the case where
            # the *combined* destination path is what overflowed.
            if error_class is ErrorClass.PATH_TOO_LONG and attempt == 0:
                continue
            if error_class in (ErrorClass.TRANSIENT_DEST, ErrorClass.DEST_FULL):
                raise
            if attempt + 1 >= attempts:
                break
            sleep(options.retry_delays[attempt])

    assert last_error is not None
    raise last_error


def _copy_file_chunked(
    source_file: Path,
    destination_file: Path,
    cancel: Event,
    on_bytes: Callable[[int], None],
    pause: Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Copy one file a chunk at a time, reporting as it goes.

    Metadata is applied afterwards with ``copystat`` so the result matches what
    ``shutil.copy2`` would have produced. A cancel leaves the partial
    destination file in place, which is what the resume logic expects.

    Pausing is honoured between chunks as well as between files, or a single
    large file - the ones worth pausing for - would ignore the request until it
    finished.
    """
    copied = 0
    with open(safe_path(source_file), "rb") as reader:
        with open(safe_path(destination_file), "wb") as writer:
            while True:
                _hold(cancel, pause, sleep)
                if cancel.is_set():
                    raise BackupCancelled
                chunk = reader.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                writer.write(chunk)
                copied += len(chunk)
                on_bytes(copied)
    shutil.copystat(safe_path(source_file), safe_path(destination_file))


def _files_identical(source_file: Path, destination_file: Path, size: int) -> bool:
    """True when the destination already holds an identical-looking copy.

    Used two ways: to make retrying an interrupted disc nearly free (the
    "folder already has files" case, governed by ``resume_existing``), and,
    unconditionally, to decide whether a name collision is actually the same
    file or two different files that happen to share a name.
    """
    try:
        existing = os.stat(safe_path(destination_file))
    except OSError:
        return False
    if existing.st_size != size:
        return False
    try:
        original = os.stat(safe_path(source_file))
    except OSError:
        return False
    # FAT/UDF timestamps are coarse; two seconds of slack avoids needless recopies.
    return abs(existing.st_mtime - original.st_mtime) <= 2


def _unique_destination(path: Path) -> Path:
    """The Explorer-style "name (2).ext" for a path that already exists.

    Only called after a collision has been confirmed to be two different
    files - a genuine resume is handled earlier by ``_files_identical`` and
    never renames anything.
    """
    stem, suffix = path.stem, path.suffix
    counter = 2
    while True:
        candidate = path.with_name(f"{stem} ({counter}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _mkdir(path: Path) -> None:
    os.makedirs(safe_path(path), exist_ok=True)


def format_bytes(size: float) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


DEFECT_REPORT_NAME = "_DISCO_COM_DEFEITO.txt"


REPORT_WIDTH = 60
_FIELD_WIDTH = 28


def _field(label: str, value: str) -> str:
    """One aligned ``Rotulo....: valor`` line.

    Built rather than written out so the dotted leaders cannot drift: the two
    file-count lines used to be one character longer than every other line,
    which in Notepad is exactly visible enough to look like a mistake.
    """
    return f"{label}{'.' * max(1, _FIELD_WIDTH - len(label))}: {value}"


def _recovered(bytes_copied: int, total_bytes: int, files_copied: int,
               total_files: int) -> str:
    """How much of the disc actually made it, as a percentage.

    Bytes when the disc was measured, files when it was not - a count of
    files is a poorer proxy for "how much of it is here" (one video file can
    outweigh a thousand small ones), but it beats saying nothing.

    A disc written off without ever being read has no total to divide by, and
    reports that honestly instead of showing 0% as though that were measured.
    """
    if total_bytes > 0:
        percent = min(100.0, bytes_copied / total_bytes * 100)
        # Decimal comma, to match every size the app shows on screen.
        return (
            f"{percent:.0f}%  ({format_bytes(bytes_copied).replace('.', ',')} "
            f"de {format_bytes(total_bytes).replace('.', ',')})"
        )
    if total_files > 0:
        percent = min(100.0, files_copied / total_files * 100)
        return f"{percent:.0f}%  ({files_copied} de {total_files} arquivos)"
    if files_copied:
        return f"não foi possível medir ({files_copied} arquivos copiados)"
    return "0%  (nenhum arquivo foi copiado)"


def write_defect_report(
    folder: Path,
    disc_name: str,
    reason: str,
    files_copied: int,
    failed_files: Sequence[FileFailure] | Sequence[dict],
    when: str,
    serial: int | None = None,
    note: str = "",
    skipped: bool = False,
    bytes_copied: int = 0,
    total_bytes: int = 0,
    total_files: int = 0,
) -> Path:
    """Leave a note in the folder saying this disc could not be read fully.

    Written into the disc's own folder, not the batch folder, so it travels
    with the incomplete data it describes - and so "Limpar arquivos de
    controle", which only ever touches the batch folder, never removes it.
    This file is about the data, not about the tool.

    ``skipped`` distinguishes the two ways a disc gets written off: abandoned
    part-way through a copy, or declared bad and never attempted. Someone
    opening the folder months later needs to know which, because one means
    "some of it is here" and the other means "none of it is".
    """
    folder.mkdir(parents=True, exist_ok=True)
    # Measured, not assumed. A folder marked defective after a copy that did
    # finish would otherwise be handed over claiming to be incomplete on the
    # same page as "100% recuperado".
    whole_disc = total_bytes > 0 and bytes_copied >= total_bytes
    if skipped:
        if not files_copied:
            second = "NENHUM ARQUIVO FOI COPIADO DESTE DISCO."
        elif whole_disc:
            second = "O que foi copiado antes da marcação parece estar completo."
        else:
            second = "O CONTEÚDO DESTA PASTA ESTÁ INCOMPLETO."
        headline = [
            "Esta pasta foi marcada como PULADA porque o disco está defeituoso.",
            second,
        ]
    else:
        headline = [
            "Este disco foi marcado como defeituoso durante a cópia.",
            "O CONTEÚDO DESTA PASTA ESTÁ INCOMPLETO.",
        ]

    lines = ["DISCO COM DEFEITO", "=" * REPORT_WIDTH, "", *headline, ""]
    lines.append(_field("Disco", disc_name))
    if serial:
        lines.append(_field("Número de série", f"0x{serial:08X}"))
    lines += [
        _field("Marcado em", when),
        _field("Motivo", reason),
    ]
    if note.strip():
        observation = note.strip().splitlines()
        lines.append(_field("Observação", observation[0]))
        # Keep any further lines aligned under the first, so a long note
        # stays readable in Notepad.
        lines += [f"{' ' * (_FIELD_WIDTH + 2)}{extra}" for extra in observation[1:]]
    lines += [
        "",
        _field("Arquivos copiados", str(files_copied)),
        # Deliberately not a count of files that failed. A disc the drive
        # gives up on never reports individual failures, so that number was
        # always 0 sitting under "INCOMPLETO" - which read as "nothing went
        # wrong". How much of the disc came back is the honest measure.
        _field("Conteúdo recuperado",
               _recovered(bytes_copied, total_bytes, files_copied, total_files)),
    ]

    if failed_files:
        lines += ["", "ARQUIVOS QUE NÃO PUDERAM SER LIDOS", "-" * REPORT_WIDTH]
        for failure in failed_files:
            if isinstance(failure, dict):
                path, error = failure.get("path", "?"), failure.get("error", "")
            else:
                path, error = failure.relative_path, failure.error
            lines.append(path)
            if error:
                lines.append(f"    {error}")
    elif skipped:
        lines += ["", "O disco não chegou a ser lido, então não há lista de arquivos."]
    else:
        lines += [
            "",
            "Nenhum arquivo individual chegou a ser identificado: a unidade",
            "parou de responder antes disso.",
        ]

    lines += [
        "",
        "-" * REPORT_WIDTH,
        "Arquivo gerado automaticamente pelo backupov2.",
        "",
    ]

    report = folder / DEFECT_REPORT_NAME
    # utf-8-sig, not plain utf-8: this file now carries accents and gets
    # opened by double-click on whatever machine the delivery lands on. The
    # BOM is what stops a tool that still defaults to ANSI turning "cópia"
    # into "cÃ³pia" in a document that goes out with the discs.
    report.write_text("\n".join(lines), encoding="utf-8-sig")
    return report
