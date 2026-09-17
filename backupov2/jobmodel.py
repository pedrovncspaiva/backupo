"""The job: a parent folder plus an ordered list of disc entries.

Two rules carry most of the design weight, and between them are what make
skip, redirect, reorder and resume possible:

1. ``entry_id`` is identity; position in the list is merely order. Nothing is
   ever keyed on an index.
2. There is no "current disc" pointer anywhere. The next target is a *derived
   query* - the first entry still PENDING. Skip, redirect, reorder and mid-run
   insertion then need no bookkeeping at all, because there is no pointer that
   could disagree with the list.

Unknown keys are preserved on load and re-emitted on save, so an older build
opening a newer build's job file never destroys what it does not understand.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator, Sequence

SCHEMA_VERSION = 1


def utc_now() -> str:
    """Timestamps are stored as UTC ISO-8601 with a trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    return uuid.uuid4().hex


class EntryStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class DiscKind(str, Enum):
    UNKNOWN = "unknown"
    DATA = "data"
    AUDIO = "audio"


def _coerce(enum_cls, value, default):
    """Enum values from disk may be stale or misspelled; never crash on load."""
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except (ValueError, KeyError):
        return default


def _split_unknown(data: dict, known: Iterable[str]) -> dict:
    """Everything on disk that this build does not recognise, kept for re-emit."""
    known_set = set(known)
    return {key: value for key, value in data.items() if key not in known_set}


@dataclass
class EntryDraft:
    """The only way an entry enters a job.

    This is the seam the future photo/LLM naming step plugs into: it will produce
    drafts carrying ``name_confidence``, ``photo_ref`` and ``llm``, and nothing
    in the model, the store or the runner has to change to accept them.
    """

    folder_name: str
    group: str = ""  # optional subfolder between the parent and this disc
    source: str = "manual"  # manual, csv, llm_photo
    name_confidence: float | None = None
    needs_review: bool = False
    photo_ref: str | None = None
    llm: dict | None = None
    notes: str = ""


@dataclass
class MediaRecord:
    """What was actually in the drive - which after a redirect is not the disc
    you would predict from the folder's position in the list."""

    drive: str = ""
    label: str = ""
    serial: int | None = None
    fs_name: str = ""
    toc_hash: str | None = None  # audio discs: their only reliable identity
    file_count: int = 0
    total_bytes: int = 0

    KNOWN = (
        "drive",
        "label",
        "serial",
        "fs_name",
        "toc_hash",
        "file_count",
        "total_bytes",
    )

    @property
    def display_name(self) -> str:
        name = self.label or "Disco sem rotulo"
        return f"{name} ({self.drive})" if self.drive else name

    def to_dict(self) -> dict:
        return {
            "drive": self.drive,
            "label": self.label,
            "serial": self.serial,
            "serial_hex": f"0x{self.serial:08X}" if self.serial else None,
            "fs_name": self.fs_name,
            "toc_hash": self.toc_hash,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "MediaRecord | None":
        if not data:
            return None
        return cls(
            drive=data.get("drive", ""),
            label=data.get("label", ""),
            serial=data.get("serial"),
            fs_name=data.get("fs_name", ""),
            toc_hash=data.get("toc_hash"),
            file_count=data.get("file_count", 0) or 0,
            total_bytes=data.get("total_bytes", 0) or 0,
        )


@dataclass
class EntryResult:
    files_copied: int = 0
    bytes_copied: int = 0
    files_failed: int = 0
    failed_files: list[dict] = field(default_factory=list)
    files_skipped_existing: int = 0
    # A name collision with different content on each side - never overwritten,
    # the incoming file was saved under a new name instead. See core.copy_tree.
    files_renamed: int = 0
    renamed_files: list[dict] = field(default_factory=list)
    tracks_ripped: int = 0
    duration_seconds: float = 0.0
    partial: bool = False

    def to_dict(self) -> dict:
        return {
            "files_copied": self.files_copied,
            "bytes_copied": self.bytes_copied,
            "files_failed": self.files_failed,
            "failed_files": list(self.failed_files),
            "files_skipped_existing": self.files_skipped_existing,
            "files_renamed": self.files_renamed,
            "renamed_files": list(self.renamed_files),
            "tracks_ripped": self.tracks_ripped,
            "duration_seconds": self.duration_seconds,
            "partial": self.partial,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "EntryResult | None":
        if not data:
            return None
        return cls(
            files_copied=data.get("files_copied", 0) or 0,
            bytes_copied=data.get("bytes_copied", 0) or 0,
            files_failed=data.get("files_failed", 0) or 0,
            failed_files=list(data.get("failed_files") or []),
            files_skipped_existing=data.get("files_skipped_existing", 0) or 0,
            files_renamed=data.get("files_renamed", 0) or 0,
            renamed_files=list(data.get("renamed_files") or []),
            tracks_ripped=data.get("tracks_ripped", 0) or 0,
            duration_seconds=data.get("duration_seconds", 0.0) or 0.0,
            partial=bool(data.get("partial", False)),
        )


@dataclass
class CollectedDisc:
    """One disc absorbed by a collecting folder.

    A folder that keeps receiving discs would otherwise have each disc's record
    overwritten by the next, leaving no trace of what actually went in. The
    entry's ``result`` holds the running total; this holds the individual discs.
    """

    media: MediaRecord | None = None
    finished_utc: str = ""
    files_copied: int = 0
    bytes_copied: int = 0
    files_failed: int = 0
    files_renamed: int = 0

    def to_dict(self) -> dict:
        return {
            "media": self.media.to_dict() if self.media else None,
            "finished_utc": self.finished_utc,
            "files_copied": self.files_copied,
            "bytes_copied": self.bytes_copied,
            "files_failed": self.files_failed,
            "files_renamed": self.files_renamed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CollectedDisc":
        return cls(
            media=MediaRecord.from_dict(data.get("media")),
            finished_utc=data.get("finished_utc", "") or "",
            files_copied=data.get("files_copied", 0) or 0,
            bytes_copied=data.get("bytes_copied", 0) or 0,
            files_failed=data.get("files_failed", 0) or 0,
            files_renamed=data.get("files_renamed", 0) or 0,
        )


@dataclass
class DiscEntry:
    folder_name: str
    # One optional level between the parent folder and this disc's folder, so a
    # delivery sheet's CAIXA > EG > disc hierarchy maps onto real directories.
    # Empty means the disc folder sits directly under the parent.
    group: str = ""
    entry_id: str = field(default_factory=new_id)
    status: EntryStatus = EntryStatus.PENDING
    disc_kind: DiscKind = DiscKind.UNKNOWN
    media: MediaRecord | None = None
    result: EntryResult | None = None
    attempts: int = 0
    started_utc: str | None = None
    finished_utc: str | None = None
    error: str | None = None
    needs_review: bool = False
    notes: str = ""
    # "Keep sending discs here until I turn it off." While this is set the
    # entry is the target for every disc regardless of its status, and the
    # discs pile up inside it instead of the list advancing. At most one entry
    # in a job may have it - see Job.set_collecting.
    collecting: bool = False
    collected: list[CollectedDisc] = field(default_factory=list)
    # Provenance - populated by whichever producer created the draft.
    source: str = "manual"
    name_confidence: float | None = None
    photo_ref: str | None = None
    llm: dict | None = None
    extra: dict = field(default_factory=dict)
    unknown: dict = field(default_factory=dict, repr=False)

    KNOWN = (
        "entry_id",
        "folder_name",
        "group",
        "status",
        "disc_kind",
        "media",
        "result",
        "attempts",
        "started_utc",
        "finished_utc",
        "error",
        "needs_review",
        "notes",
        "collecting",
        "collected",
        "source",
        "name_confidence",
        "photo_ref",
        "llm",
        "extra",
    )

    @classmethod
    def from_draft(cls, draft: EntryDraft) -> "DiscEntry":
        return cls(
            folder_name=draft.folder_name,
            group=draft.group,
            source=draft.source,
            name_confidence=draft.name_confidence,
            needs_review=draft.needs_review,
            photo_ref=draft.photo_ref,
            llm=draft.llm,
            notes=draft.notes,
        )

    @property
    def is_open(self) -> bool:
        """Still waiting for a disc - work remains for this entry.

        A collecting folder is open even once it is ``done``: it is done with
        the discs it has, and waiting for however many more you feed it. That
        is what keeps the job from declaring itself finished underneath you.
        """
        if self.collecting:
            return True
        return self.status in (EntryStatus.PENDING, EntryStatus.IN_PROGRESS)

    @property
    def failed_file_count(self) -> int:
        return self.result.files_failed if self.result else 0

    @property
    def disc_count(self) -> int:
        """Discs written into this folder. Only a collecting folder exceeds 1."""
        if self.collected:
            return len(self.collected)
        return 1 if self.status is EntryStatus.DONE else 0

    def absorb(self, media: MediaRecord | None, result: EntryResult) -> None:
        """Add one more disc's outcome to this folder instead of replacing it.

        ``result`` becomes the running total across every disc, while the disc
        itself is appended to ``collected`` so the individual contributions are
        still recoverable.
        """
        self.collected.append(
            CollectedDisc(
                media=media,
                finished_utc=utc_now(),
                files_copied=result.files_copied,
                bytes_copied=result.bytes_copied,
                files_failed=result.files_failed,
                files_renamed=result.files_renamed,
            )
        )
        if self.result is None:
            self.result = result
        else:
            total = self.result
            total.files_copied += result.files_copied
            total.bytes_copied += result.bytes_copied
            total.files_failed += result.files_failed
            total.failed_files.extend(result.failed_files)
            total.files_skipped_existing += result.files_skipped_existing
            total.files_renamed += result.files_renamed
            total.renamed_files.extend(result.renamed_files)
            total.tracks_ripped += result.tracks_ripped
            total.duration_seconds += result.duration_seconds
            total.partial = result.partial
        # The label column should name the disc that went in last, not the
        # first one ever - the latest is the one you just handled.
        self.media = media or self.media

    def to_dict(self) -> dict:
        data = {
            "entry_id": self.entry_id,
            "folder_name": self.folder_name,
            "group": self.group,
            "status": self.status.value,
            "disc_kind": self.disc_kind.value,
            "media": self.media.to_dict() if self.media else None,
            "result": self.result.to_dict() if self.result else None,
            "attempts": self.attempts,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "error": self.error,
            "needs_review": self.needs_review,
            "notes": self.notes,
            "collecting": self.collecting,
            "collected": [item.to_dict() for item in self.collected],
            "source": self.source,
            "name_confidence": self.name_confidence,
            "photo_ref": self.photo_ref,
            "llm": self.llm,
            "extra": dict(self.extra),
        }
        data.update(self.unknown)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "DiscEntry":
        return cls(
            entry_id=data.get("entry_id") or new_id(),
            folder_name=data.get("folder_name", ""),
            group=data.get("group", "") or "",
            status=_coerce(EntryStatus, data.get("status"), EntryStatus.PENDING),
            disc_kind=_coerce(DiscKind, data.get("disc_kind"), DiscKind.UNKNOWN),
            media=MediaRecord.from_dict(data.get("media")),
            result=EntryResult.from_dict(data.get("result")),
            attempts=data.get("attempts", 0) or 0,
            started_utc=data.get("started_utc"),
            finished_utc=data.get("finished_utc"),
            error=data.get("error"),
            needs_review=bool(data.get("needs_review", False)),
            notes=data.get("notes", "") or "",
            collecting=bool(data.get("collecting", False)),
            collected=[
                CollectedDisc.from_dict(item) for item in data.get("collected") or []
            ],
            source=data.get("source", "manual") or "manual",
            name_confidence=data.get("name_confidence"),
            photo_ref=data.get("photo_ref"),
            llm=data.get("llm"),
            extra=dict(data.get("extra") or {}),
            unknown=_split_unknown(data, cls.KNOWN),
        )


@dataclass
class JobSettings:
    auto_copy: bool = True
    grace_seconds: int = 10
    auto_eject: bool = True
    audio_format: str = "mp3"
    audio_bitrate: str = "320k"
    paranoia_mode: str = "full"
    on_file_error: str = "retry_then_skip"
    retry_attempts: int = 3
    resume_existing: bool = True
    ffmpeg_path: str | None = None
    unknown: dict = field(default_factory=dict, repr=False)

    KNOWN = (
        "auto_copy",
        "grace_seconds",
        "auto_eject",
        "audio_format",
        "audio_bitrate",
        "paranoia_mode",
        "on_file_error",
        "retry_attempts",
        "resume_existing",
        "ffmpeg_path",
    )

    def to_dict(self) -> dict:
        data = {name: getattr(self, name) for name in self.KNOWN}
        data.update(self.unknown)
        return data

    @classmethod
    def from_dict(cls, data: dict | None) -> "JobSettings":
        data = data or {}
        defaults = cls()
        values = {name: data.get(name, getattr(defaults, name)) for name in cls.KNOWN}
        return cls(unknown=_split_unknown(data, cls.KNOWN), **values)


@dataclass
class Job:
    destination_root: str
    parent_name: str
    job_id: str = field(default_factory=new_id)
    schema_version: int = SCHEMA_VERSION
    app_version: str = ""
    created_utc: str = field(default_factory=utc_now)
    updated_utc: str = field(default_factory=utc_now)
    settings: JobSettings = field(default_factory=JobSettings)
    entries: list[DiscEntry] = field(default_factory=list)
    unknown: dict = field(default_factory=dict, repr=False)

    KNOWN = (
        "schema_version",
        "app_version",
        "job_id",
        "created_utc",
        "updated_utc",
        "destination_root",
        "parent_name",
        "settings",
        "entries",
    )

    # -- paths ------------------------------------------------------------

    @property
    def parent_path(self) -> Path:
        return Path(self.destination_root) / self.parent_name

    def folder_for(self, entry: DiscEntry) -> Path:
        """Where this disc's files go.

        With a group set the layout is parent/group/disc, which is what a
        delivery sheet describes: CAIXA 02 > EG 1841 (BENGUELA - ABG) > the
        discs. Without one it stays parent/disc.
        """
        if entry.group:
            return self.parent_path / entry.group / entry.folder_name
        return self.parent_path / entry.folder_name

    # -- queries ----------------------------------------------------------

    def next_pending(self) -> DiscEntry | None:
        """The target for the next disc.

        Derived, never stored. Storing a "current disc" index instead is what
        makes skip and redirect hard; this way they need no extra state at all.

        A folder set to collect wins over the list order for as long as the
        flag is on - which is the whole of the "keep feeding this one folder"
        behaviour, with no second pointer to keep in step.
        """
        collecting = self.collecting_entry()
        if collecting is not None:
            return collecting
        for entry in self.entries:
            if entry.status is EntryStatus.PENDING:
                return entry
        return None

    def collecting_entry(self) -> DiscEntry | None:
        """The folder currently set to swallow every disc, if any."""
        for entry in self.entries:
            if entry.collecting:
                return entry
        return None

    def entry_by_id(self, entry_id: str) -> DiscEntry | None:
        for entry in self.entries:
            if entry.entry_id == entry_id:
                return entry
        return None

    def index_of(self, entry_id: str) -> int:
        for index, entry in enumerate(self.entries):
            if entry.entry_id == entry_id:
                return index
        raise KeyError(entry_id)

    def count_by_status(self) -> dict[EntryStatus, int]:
        counts = {status: 0 for status in EntryStatus}
        for entry in self.entries:
            counts[entry.status] += 1
        return counts

    @property
    def is_complete(self) -> bool:
        return bool(self.entries) and not any(entry.is_open for entry in self.entries)

    def find_by_serial(self, serial: int | None) -> DiscEntry | None:
        """Strong duplicate match: the same non-zero volume serial.

        Windows synthesises a CD serial from the ISO9660 creation timestamp, so
        two discs burned in the same second can collide and some report 0. That
        is why a hit warns rather than blocks, and why 0 never matches.
        """
        if not serial:
            return None
        for entry in self.entries:
            if entry.media and entry.media.serial == serial:
                return entry
            # A collecting folder holds several discs and ``media`` only names
            # the last one, so without this the earlier ones would silently
            # stop being recognised as already copied.
            for item in entry.collected:
                if item.media and item.media.serial == serial:
                    return entry
        return None

    def find_by_toc_hash(self, toc_hash: str | None) -> DiscEntry | None:
        if not toc_hash:
            return None
        for entry in self.entries:
            if entry.media and entry.media.toc_hash == toc_hash:
                return entry
            for item in entry.collected:
                if item.media and item.media.toc_hash == toc_hash:
                    return entry
        return None

    def has_folder_named(
        self, name: str, group: str = "", excluding: str | None = None
    ) -> bool:
        """True when *name* is already taken inside *group*.

        Scoped to the group because the same disc code under two different EGs
        lands in two different directories and so does not collide.
        """
        folded = name.casefold()
        folded_group = group.casefold()
        for entry in self.entries:
            if excluding is not None and entry.entry_id == excluding:
                continue
            if (
                entry.folder_name.casefold() == folded
                and entry.group.casefold() == folded_group
            ):
                return True
        return False

    def group_names(self) -> list[str]:
        """Every distinct group folder, in the order they first appear."""
        seen: list[str] = []
        for entry in self.entries:
            if entry.group and entry.group not in seen:
                seen.append(entry.group)
        return seen

    # -- mutation ---------------------------------------------------------

    def add_entries(
        self, drafts: Sequence[EntryDraft], at_index: int | None = None
    ) -> list[DiscEntry]:
        """The single door through which entries enter a job."""
        created = [DiscEntry.from_draft(draft) for draft in drafts]
        if at_index is None:
            self.entries.extend(created)
        else:
            position = max(0, min(at_index, len(self.entries)))
            self.entries[position:position] = created
        return created

    def set_collecting(self, entry_id: str, on: bool = True) -> DiscEntry | None:
        """Turn the "keep adding discs here" flag on or off for one folder.

        Turning it on turns it off everywhere else: two collecting folders
        would both claim every disc and the winner would come down to list
        order, which is not a choice anyone made.
        """
        entry = self.entry_by_id(entry_id)
        if entry is None:
            return None
        if on:
            for other in self.entries:
                other.collecting = other is entry
        else:
            entry.collecting = False
        return entry

    def remove_entry(self, entry_id: str) -> DiscEntry:
        return self.entries.pop(self.index_of(entry_id))

    def move_entry(self, entry_id: str, offset: int) -> int:
        """Move an entry up or down the list. Returns its new index."""
        index = self.index_of(entry_id)
        target = max(0, min(index + offset, len(self.entries) - 1))
        if target != index:
            entry = self.entries.pop(index)
            self.entries.insert(target, entry)
        return target

    def touch(self) -> None:
        self.updated_utc = utc_now()

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict:
        data = {
            "schema_version": self.schema_version,
            "app_version": self.app_version,
            "job_id": self.job_id,
            "created_utc": self.created_utc,
            "updated_utc": self.updated_utc,
            "destination_root": self.destination_root,
            "parent_name": self.parent_name,
            "settings": self.settings.to_dict(),
            "entries": [entry.to_dict() for entry in self.entries],
        }
        data.update(self.unknown)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            app_version=data.get("app_version", "") or "",
            job_id=data.get("job_id") or new_id(),
            created_utc=data.get("created_utc") or utc_now(),
            updated_utc=data.get("updated_utc") or utc_now(),
            destination_root=data.get("destination_root", ""),
            parent_name=data.get("parent_name", ""),
            settings=JobSettings.from_dict(data.get("settings")),
            entries=[DiscEntry.from_dict(item) for item in data.get("entries") or []],
            unknown=_split_unknown(data, cls.KNOWN),
        )

    def __iter__(self) -> Iterator[DiscEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)
