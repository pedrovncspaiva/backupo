"""Reconciling a saved job against what is actually on disk.

This module is what makes resume possible. A destination folder that already
holds files is exactly the state you are in when you reopen a half-finished
batch, so it cannot be treated as an error. Here it becomes a *finding*: a
question the user answers, with the safe options spelled out.

Nothing is mutated silently except the cases marked ``applied=True``, which are
mechanical (creating a missing folder, downgrading an entry that was mid-copy
when the app died). Everything else is proposed and waits for a decision.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

from .jobmodel import DiscEntry, EntryResult, EntryStatus, Job


class FindingKind(str, Enum):
    FOLDER_CREATED = "folder_created"
    INTERRUPTED = "interrupted"
    FOLDER_NOT_EMPTY = "folder_not_empty"
    DONE_FOLDER_MISSING = "done_folder_missing"
    DONE_CONTENTS_CHANGED = "done_contents_changed"
    DESTINATION_UNREACHABLE = "destination_unreachable"


class Resolution(str, Enum):
    """Choices the UI offers for a finding. The runner never picks these."""

    RESUME_INTO = "resume_into"  # keep the files, copy the rest
    OVERWRITE = "overwrite"
    MARK_DONE = "mark_done"
    MARK_PENDING = "mark_pending"
    RENAME = "rename"
    IGNORE = "ignore"


@dataclass
class Finding:
    kind: FindingKind
    entry_id: str | None
    message: str
    folder: Path | None = None
    options: tuple[Resolution, ...] = ()
    applied: bool = False  # True when reconcile already made the change

    @property
    def needs_decision(self) -> bool:
        return bool(self.options) and not self.applied


@dataclass
class ReconcileReport:
    findings: list[Finding] = field(default_factory=list)
    destination_reachable: bool = True

    @property
    def questions(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.needs_decision]

    def __bool__(self) -> bool:
        return bool(self.findings)


def _folder_state(folder: Path) -> tuple[bool, int]:
    """(exists, number of entries directly inside). Cheap - no recursive walk."""
    try:
        if not folder.is_dir():
            return False, 0
        count = 0
        with os.scandir(folder) as scanner:
            for _ in scanner:
                count += 1
        return True, count
    except OSError:
        return False, 0


def _count_files(folder: Path) -> int:
    """Recursive file count, used only to compare against a recorded result."""
    total = 0
    for _root, _dirs, files in os.walk(folder):
        total += len(files)
    return total


def reconcile(
    job: Job,
    create_missing: bool = True,
    count_files: Callable[[Path], int] = _count_files,
) -> ReconcileReport:
    """Compare every entry's stored status with its folder on disk."""
    report = ReconcileReport()

    try:
        destination_root = Path(job.destination_root)
        reachable = destination_root.is_dir()
    except OSError:
        reachable = False

    if not reachable:
        report.destination_reachable = False
        report.findings.append(
            Finding(
                kind=FindingKind.DESTINATION_UNREACHABLE,
                entry_id=None,
                message=(
                    f"Destino indisponivel: {job.destination_root}. "
                    "Reconecte a unidade de rede antes de continuar."
                ),
                folder=Path(job.destination_root),
            )
        )
        # Without the destination there is nothing on disk to compare against.
        return report

    for entry in job.entries:
        # Ask the job, so a grouped entry is checked at parent/group/disc
        # rather than at a path that no longer exists.
        folder = job.folder_for(entry)
        exists, child_count = _folder_state(folder)

        if entry.status is EntryStatus.IN_PROGRESS:
            _handle_interrupted(report, entry, folder)
            continue

        if entry.status is EntryStatus.PENDING:
            _handle_pending(report, entry, folder, exists, child_count, create_missing)
            continue

        if entry.status is EntryStatus.DONE:
            _handle_done(report, entry, folder, exists, child_count, count_files)
            continue

        # FAILED and SKIPPED are left exactly as they are; the Problemas tab
        # surfaces them and the user decides whether to retry.

    return report


def _handle_interrupted(report: ReconcileReport, entry: DiscEntry, folder: Path) -> None:
    """The app died mid-copy. Downgrade so the disc can simply be retried.

    Retrying is cheap because the copy runs with ``resume_existing``, so the
    files already written are skipped rather than re-read from the disc.
    """
    entry.status = EntryStatus.PENDING
    # Always record that this entry was interrupted, even if it died before any
    # progress was written - otherwise a crash early in a disc is indistinguishable
    # from a disc never started, and the partial files left behind go unexplained.
    if entry.result is None:
        entry.result = EntryResult()
    entry.result.partial = True
    report.findings.append(
        Finding(
            kind=FindingKind.INTERRUPTED,
            entry_id=entry.entry_id,
            message=(
                f"'{entry.folder_name}' foi interrompida durante a copia e voltou "
                "para pendente. Reinsira o disco para continuar de onde parou."
            ),
            folder=folder,
            applied=True,
        )
    )


def _handle_pending(
    report: ReconcileReport,
    entry: DiscEntry,
    folder: Path,
    exists: bool,
    child_count: int,
    create_missing: bool,
) -> None:
    if not exists:
        if create_missing:
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                report.findings.append(
                    Finding(
                        kind=FindingKind.FOLDER_CREATED,
                        entry_id=entry.entry_id,
                        message=f"Nao foi possivel criar '{entry.folder_name}': {exc}",
                        folder=folder,
                    )
                )
        return

    if child_count == 0:
        return  # empty and waiting, exactly as expected

    # Not an error - just a question, because this is what resuming looks like.
    entry.needs_review = True
    report.findings.append(
        Finding(
            kind=FindingKind.FOLDER_NOT_EMPTY,
            entry_id=entry.entry_id,
            message=(
                f"'{entry.folder_name}' ja contem {child_count} item(ns), mas esta "
                "marcada como pendente. O que deseja fazer?"
            ),
            folder=folder,
            options=(
                Resolution.RESUME_INTO,
                Resolution.OVERWRITE,
                Resolution.MARK_DONE,
                Resolution.RENAME,
            ),
        )
    )


def _handle_done(
    report: ReconcileReport,
    entry: DiscEntry,
    folder: Path,
    exists: bool,
    child_count: int,
    count_files: Callable[[Path], int],
) -> None:
    if not exists or child_count == 0:
        # Do not silently revert: the folder may have been moved or archived on
        # purpose, and re-copying a disc the user already filed away is worse
        # than asking.
        report.findings.append(
            Finding(
                kind=FindingKind.DONE_FOLDER_MISSING,
                entry_id=entry.entry_id,
                message=(
                    f"'{entry.folder_name}' esta marcada como concluida, mas a pasta "
                    "esta vazia ou nao existe mais."
                ),
                folder=folder,
                options=(Resolution.MARK_PENDING, Resolution.IGNORE),
            )
        )
        return

    expected = entry.result.files_copied if entry.result else None
    if not expected:
        return

    actual = count_files(folder)
    if actual == expected:
        return

    entry.needs_review = True
    report.findings.append(
        Finding(
            kind=FindingKind.DONE_CONTENTS_CHANGED,
            entry_id=entry.entry_id,
            message=(
                f"'{entry.folder_name}': {expected} arquivo(s) foram copiados, mas "
                f"a pasta contem {actual} agora."
            ),
            folder=folder,
        )
    )


def apply_resolution(
    job: Job,
    finding: Finding,
    resolution: Resolution,
    new_name: str | None = None,
) -> None:
    """Apply the user's answer to a finding."""
    entry = job.entry_by_id(finding.entry_id) if finding.entry_id else None
    if entry is None:
        return

    if resolution is Resolution.RESUME_INTO:
        entry.status = EntryStatus.PENDING
        entry.needs_review = False
    elif resolution is Resolution.OVERWRITE:
        entry.status = EntryStatus.PENDING
        entry.needs_review = False
        entry.notes = (entry.notes + " Sobrescrever ao copiar.").strip()
    elif resolution is Resolution.MARK_DONE:
        entry.status = EntryStatus.DONE
        entry.needs_review = False
    elif resolution is Resolution.MARK_PENDING:
        entry.status = EntryStatus.PENDING
        entry.needs_review = False
    elif resolution is Resolution.RENAME:
        if new_name:
            entry.folder_name = new_name
            entry.status = EntryStatus.PENDING
            entry.needs_review = False
    elif resolution is Resolution.IGNORE:
        entry.needs_review = False
