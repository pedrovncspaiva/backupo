"""Persisting a job, durably and in two places.

Every job is written twice:

* a **local** copy under %LOCALAPPDATA%, which is authoritative for in-flight
  state, and
* a **portable** copy inside the backup parent folder, which makes that folder
  self-describing - hand it to someone else and the job reopens.

Local is written first and the remote copy is best-effort. The destination is
normally a network share, and a job file that lives only on the share cannot
record the failure of that share. That ordering is the whole reason a dropped
SMB connection mid-batch does not cost you the record of what was already done.

Writes are atomic (temp file, then ``os.replace``) and keep one ``.bak``
generation, because a half-written job file is worse than an old one.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import __version__
from .jobmodel import DiscEntry, EntryDraft, Job, JobSettings

JOB_FILE_NAME = "_backupov2-job.json"
LOG_FILE_NAME = "_backupov2.log"
RECENT_FILE_NAME = "recent.json"
RECENT_LIMIT = 20

# The files backupov2 leaves inside a batch folder. Everything else in there is
# the user's backed-up data and is never touched by the cleanup helpers below.
CONTROL_FILE_NAMES = (
    JOB_FILE_NAME,
    JOB_FILE_NAME + ".bak",
    JOB_FILE_NAME + ".tmp",
    LOG_FILE_NAME,
)


def default_local_root() -> Path:
    """%LOCALAPPDATA%\\backupov2, with a home-directory fallback off Windows."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "backupov2"
    return Path.home() / ".backupov2"


def local_job_path(job_id: str, local_root: Path | None = None) -> Path:
    root = local_root or default_local_root()
    return root / "jobs" / f"{job_id}.json"


def remote_job_path(destination_root: str | Path, parent_name: str) -> Path:
    return Path(destination_root) / parent_name / JOB_FILE_NAME


def atomic_write_json(path: Path, payload: dict) -> None:
    """Write *payload* to *path* without ever leaving it half-written.

    The temp file is created in the same directory so ``os.replace`` stays on
    one filesystem and is therefore atomic. The previous version is retained as
    ``.bak`` so a corrupted write has something to fall back to.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    text = json.dumps(payload, indent=2, ensure_ascii=False)

    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())

    try:
        if path.exists():
            try:
                shutil.copy2(path, path.with_name(path.name + ".bak"))
            except OSError:
                pass  # a missing backup must never block the real write
        os.replace(temporary, path)
    except BaseException:
        # Leave the existing file untouched and clean up after ourselves.
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Job file is not a JSON object: {path}")
    return data


@dataclass
class SaveOutcome:
    local_ok: bool = False
    remote_ok: bool = False
    local_error: str | None = None
    remote_error: str | None = None

    @property
    def ok(self) -> bool:
        """A save counts as successful if the authoritative copy landed."""
        return self.local_ok


@dataclass
class LoadOutcome:
    job: Job
    source_path: Path
    used_backup: bool = False
    notes: list[str] = field(default_factory=list)


class JobStore:
    """Owns one job and knows where its two copies live."""

    def __init__(
        self,
        job: Job,
        local_root: Path | None = None,
        source_path: Path | None = None,
    ) -> None:
        self.job = job
        self.local_root = local_root or default_local_root()
        self.source_path = source_path

    # -- construction -----------------------------------------------------

    @classmethod
    def create(
        cls,
        destination_root: str | Path,
        parent_name: str,
        settings: JobSettings | None = None,
        local_root: Path | None = None,
    ) -> "JobStore":
        job = Job(
            destination_root=str(destination_root),
            parent_name=parent_name,
            app_version=__version__,
            settings=settings or JobSettings(),
        )
        return cls(job, local_root=local_root)

    @classmethod
    def load(cls, path: Path, local_root: Path | None = None) -> LoadOutcome:
        """Load one job file, falling back to its ``.bak`` if it is unreadable."""
        path = Path(path)
        notes: list[str] = []
        try:
            data = read_json(path)
            return LoadOutcome(
                job=Job.from_dict(data),
                source_path=path,
                notes=notes,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            backup = path.with_name(path.name + ".bak")
            if not backup.exists():
                raise
            notes.append(
                f"Arquivo do trabalho ilegivel ({exc}); usando a copia de seguranca."
            )
            data = read_json(backup)
            return LoadOutcome(
                job=Job.from_dict(data),
                source_path=backup,
                used_backup=True,
                notes=notes,
            )

    @classmethod
    def open_newest(
        cls,
        candidates: Sequence[Path],
        local_root: Path | None = None,
    ) -> LoadOutcome:
        """Open whichever of *candidates* was updated most recently.

        Used when both the local and portable copies exist and might disagree -
        for instance the share was offline for the last few discs, so the local
        copy is ahead.
        """
        loaded: list[LoadOutcome] = []
        errors: list[str] = []
        for candidate in candidates:
            candidate = Path(candidate)
            if not candidate.exists():
                continue
            try:
                loaded.append(cls.load(candidate, local_root=local_root))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{candidate}: {exc}")

        if not loaded:
            raise FileNotFoundError(
                "Nenhum arquivo de trabalho legivel encontrado. " + "; ".join(errors)
            )

        loaded.sort(key=lambda outcome: outcome.job.updated_utc, reverse=True)
        best = loaded[0]
        if len(loaded) > 1:
            other = loaded[1]
            if other.job.updated_utc != best.job.updated_utc:
                best.notes.append(
                    f"Duas copias encontradas; usando a mais recente "
                    f"({best.source_path}, {best.job.updated_utc})."
                )
        best.notes.extend(errors)
        return best

    @classmethod
    def open_for(
        cls,
        destination_root: str | Path,
        parent_name: str,
        job_id: str | None = None,
        local_root: Path | None = None,
    ) -> LoadOutcome:
        candidates = [remote_job_path(destination_root, parent_name)]
        if job_id:
            candidates.insert(0, local_job_path(job_id, local_root))
        return cls.open_newest(candidates, local_root=local_root)

    # -- paths ------------------------------------------------------------

    @property
    def local_path(self) -> Path:
        return local_job_path(self.job.job_id, self.local_root)

    @property
    def remote_path(self) -> Path:
        return remote_job_path(self.job.destination_root, self.job.parent_name)

    @property
    def log_path(self) -> Path:
        return self.job.parent_path / LOG_FILE_NAME

    # -- persistence ------------------------------------------------------

    def save(self) -> SaveOutcome:
        """Local first, remote best-effort. Never raises for the remote copy."""
        self.job.touch()
        self.job.app_version = __version__
        payload = self.job.to_dict()
        outcome = SaveOutcome()

        try:
            atomic_write_json(self.local_path, payload)
            outcome.local_ok = True
        except OSError as exc:
            outcome.local_error = str(exc)

        try:
            atomic_write_json(self.remote_path, payload)
            outcome.remote_ok = True
        except OSError as exc:
            # Expected whenever the share is unreachable - that is exactly the
            # situation the local copy exists to survive.
            outcome.remote_error = str(exc)

        if outcome.local_ok:
            try:
                self.record_recent()
            except OSError:
                pass
        return outcome

    # -- mutation (the only door entries come through) --------------------

    def add_entries(
        self, drafts: Sequence[EntryDraft], at_index: int | None = None
    ) -> list[DiscEntry]:
        created = self.job.add_entries(drafts, at_index=at_index)
        self.save()
        return created

    def remove_entry(self, entry_id: str) -> DiscEntry:
        removed = self.job.remove_entry(entry_id)
        self.save()
        return removed

    def move_entry(self, entry_id: str, offset: int) -> int:
        index = self.job.move_entry(entry_id, offset)
        self.save()
        return index

    # -- recent jobs ------------------------------------------------------

    @property
    def recent_path(self) -> Path:
        return self.local_root / RECENT_FILE_NAME

    def record_recent(self) -> None:
        entries = load_recent(self.local_root)
        entries = [item for item in entries if item.get("job_id") != self.job.job_id]
        entries.insert(
            0,
            {
                "job_id": self.job.job_id,
                "destination_root": self.job.destination_root,
                "parent_name": self.job.parent_name,
                "updated_utc": self.job.updated_utc,
                "entry_count": len(self.job.entries),
                "local_path": str(self.local_path),
                "remote_path": str(self.remote_path),
            },
        )
        atomic_write_json(
            self.recent_path, {"recent": entries[:RECENT_LIMIT]}
        )


def load_recent(local_root: Path | None = None) -> list[dict]:
    path = (local_root or default_local_root()) / RECENT_FILE_NAME
    try:
        data = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    recent = data.get("recent")
    return recent if isinstance(recent, list) else []


def control_files(parent_path: Path) -> list[Path]:
    """The control files that exist in a batch folder right now.

    Only the fixed set of names this app writes - never anything the user put
    there, and never a recursive sweep.
    """
    parent_path = Path(parent_path)
    found = []
    for name in CONTROL_FILE_NAMES:
        candidate = parent_path / name
        try:
            if candidate.is_file():
                found.append(candidate)
        except OSError:
            continue
    return found


def remove_control_files(parent_path: Path) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Delete the control files from a batch folder.

    Returns ``(removed, failures)``. Does not touch the local copy under
    %LOCALAPPDATA%, so a job cleaned this way can still be reopened from
    Recentes - only the folder's self-describing copy goes.
    """
    removed: list[Path] = []
    failures: list[tuple[Path, str]] = []
    for path in control_files(parent_path):
        try:
            path.unlink()
            removed.append(path)
        except OSError as exc:
            failures.append((path, str(exc)))
    return removed, failures
