"""Optical media: what is in the drive, and what kind of disc it is.

The ``OpticalScanner`` protocol is the seam that makes the whole state machine
testable. Calling ctypes directly from the media lookup would leave no way to
exercise any of this without physically feeding discs into a drive. Here the
real implementation is one class, ``FakeScanner`` is another, and ``JobRunner``
never knows which it has.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, Sequence

from . import winapi
from .jobmodel import DiscKind, MediaRecord


@dataclass(frozen=True)
class MediaInfo:
    """A disc currently sitting in a drive."""

    root: Path
    label: str = ""
    serial: int | None = None
    fs_name: str = ""

    @property
    def drive(self) -> str:
        """'D:' - the form used in job files and messages."""
        return str(self.root)[:2].upper()

    @property
    def display_name(self) -> str:
        name = self.label or "Disco sem rotulo"
        return f"{name} ({self.drive})"

    def to_record(
        self,
        file_count: int = 0,
        total_bytes: int = 0,
        toc_hash: str | None = None,
    ) -> MediaRecord:
        return MediaRecord(
            drive=self.drive,
            label=self.label,
            serial=self.serial,
            fs_name=self.fs_name,
            toc_hash=toc_hash,
            file_count=file_count,
            total_bytes=total_bytes,
        )


class MatchStrength(str, Enum):
    NONE = "none"
    WEAK = "weak"
    STRONG = "strong"


def identity_match(first: MediaInfo | None, second: MediaInfo | None) -> MatchStrength:
    """How confident we are that two sightings are the same physical disc.

    STRONG means equal, non-zero volume serials. It is still only "strong" and
    not "certain" because Windows synthesises a CD serial from the ISO9660
    creation timestamp: two discs burned in the same second by the same tool can
    genuinely collide. That is why callers warn rather than block.
    """
    if first is None or second is None:
        return MatchStrength.NONE
    if first.serial and second.serial:
        return (
            MatchStrength.STRONG if first.serial == second.serial else MatchStrength.NONE
        )
    if first.label and first.label == second.label:
        return MatchStrength.WEAK
    if first.root == second.root and not first.label and not second.label:
        return MatchStrength.WEAK
    return MatchStrength.NONE


def same_media(first: MediaInfo | None, second: MediaInfo | None) -> bool:
    """Coarse comparison, for answering "has the disc been swapped yet?"."""
    return identity_match(first, second) is not MatchStrength.NONE


class OpticalScanner(Protocol):
    """Anything that can report which discs are currently loaded."""

    def scan(self) -> list[MediaInfo]:
        ...

    def drives(self) -> list[str]:
        """Every optical drive, as "D:", loaded or not.

        Separate from ``scan`` because a pool of one runner per drive has to
        know the drives exist before any disc is in them - otherwise the app
        cannot say "put the next one in E:".
        """
        ...


class Win32Scanner:
    """The real scanner: enumerate optical drives and read any loaded disc."""

    def drives(self) -> list[str]:
        return [str(root)[:2].upper() for root in winapi.optical_drive_roots()]

    def scan(self) -> list[MediaInfo]:
        found: list[MediaInfo] = []
        for root in winapi.optical_drive_roots():
            if not winapi.media_present(root):
                continue
            info = winapi.volume_information(root)
            if info is None:
                # Present but not yet mounted - it will be picked up next poll.
                # DISC_SETTLING requires two consecutive matching sightings
                # anyway, so skipping here costs nothing.
                continue
            found.append(
                MediaInfo(
                    root=Path(root),
                    label=info.label,
                    serial=info.serial,
                    fs_name=info.fs_name,
                )
            )
        return found


class FakeScanner:
    """Scripted scanner for tests: one list of MediaInfo per poll.

    Once the script runs out, the last state repeats forever, so a test only has
    to describe the interesting transitions.
    """

    def __init__(
        self,
        script: Sequence[Sequence[MediaInfo]] | None = None,
        drives: Sequence[str] | None = None,
    ) -> None:
        self.script = [list(step) for step in (script or [])]
        self.calls = 0
        self._drives = [d.upper() for d in drives] if drives else None

    def drives(self) -> list[str]:
        """The drives named at construction, or every drive the script ever
        mentions - so a test that only cares about discs need not list them."""
        if self._drives is not None:
            return list(self._drives)
        seen = {media.drive for step in self.script for media in step}
        return sorted(seen)

    def scan(self) -> list[MediaInfo]:
        if not self.script:
            return []
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return list(self.script[index])

    def feed(self, *states: Sequence[MediaInfo]) -> "FakeScanner":
        self.script.extend(list(state) for state in states)
        return self


# --------------------------------------------------------------------------
# Disc kind
# --------------------------------------------------------------------------

AUDIO_TRACK_SUFFIX = ".cda"


@dataclass(frozen=True)
class KindProbe:
    kind: DiscKind
    confident: bool
    reason: str


def detect_disc_kind(root: Path, fs_name: str = "") -> KindProbe:
    """Decide whether a mounted disc holds files or audio tracks.

    Cheapest signals first; only an inconclusive result should cost an ffprobe
    call (which the caller makes, not us - this function stays subprocess-free
    and therefore instant and testable).

    A mixed-mode disc (data track plus audio tracks) is reported as DATA:
    copying its files is the useful behaviour, and ripping the audio half is out
    of scope for v2.
    """
    try:
        names = os.listdir(root)
    except OSError as exc:
        return KindProbe(DiscKind.UNKNOWN, False, f"disco ilegivel: {exc}")

    if not names:
        if fs_name.upper() == "CDFS":
            return KindProbe(DiscKind.AUDIO, False, "CDFS sem arquivos visiveis")
        return KindProbe(DiscKind.UNKNOWN, False, "disco vazio")

    cda = [name for name in names if name.lower().endswith(AUDIO_TRACK_SUFFIX)]
    if cda and len(cda) == len(names):
        return KindProbe(DiscKind.AUDIO, True, f"{len(cda)} faixa(s) .cda")

    if cda:
        return KindProbe(DiscKind.DATA, True, "disco misto: copiando os arquivos")

    return KindProbe(DiscKind.DATA, True, f"{len(names)} item(ns) na raiz")
