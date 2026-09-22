"""The auto-copy state machine.

Insert a disc and it copies into the next pending folder on its own, then
ejects and waits for the next one - while leaving a grace countdown in which the
user can skip the disc or send it somewhere else.

This module must never import ``tkinter``. Every dependency it needs from the
outside world - the scanner, the copier, the ripper, the ejector, the clock - is
injected, so a whole session of discs can be replayed deterministically in a
unit test with no hardware. That is the single design constraint that keeps
skip/redirect/duplicate/resume behaviour honest.

The UI drives it with two pumps:

* ``poll()``   about once a second - looks at the drive
* ``tick()``   about ten times a second - countdown and worker results

and receives everything back as events on a queue.
"""

from __future__ import annotations

import queue
import shutil
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from .core import (
    DEFECT_REPORT_NAME,
    CopyOptions,
    CopyProgress,
    CopyResult,
    ScanResult,
    copy_tree,
    scan_tree,
    write_defect_report,
)
from .errors import BackupCancelled, ErrorClass, SourceLost, classify_os_error
from .jobmodel import DiscEntry, DiscKind, EntryResult, EntryStatus, utc_now
from .jobstore import JobStore
from .media import KindProbe, MatchStrength, MediaInfo, detect_disc_kind, identity_match

# Two consecutive identical sightings before we act. Windows takes 1-3 s to
# mount a disc, and acting on the first sighting yields phantom empty discs.
SETTLE_POLLS = 2

# How long a just-finished disc stays "recently completed" so its lingering
# presence is not mistaken for a new disc. Time-bounded so it cannot wedge.
COMPLETED_GRACE_SECONDS = 60.0

# Headroom required on the destination before starting a copy.
FREE_SPACE_MARGIN = 1.02
FREE_SPACE_SLACK_BYTES = 64 * 1024 * 1024

# When to stop treating read failures as bad luck and start treating them as a
# bad disc. Below this the per-file retry-then-skip policy handles it silently,
# which is right for the odd scratch; at or above it the disc itself is the
# problem and the user is offered a way out.
TROUBLE_FILE_THRESHOLD = 3

# No progress of any kind for this long means the drive has stopped answering.
# Generous, because a genuine retry cycle on a bad sector already costs ~7.5 s
# and a slow share can be quiet for a while without being stuck.
STALL_SECONDS = 45.0

# How long to let a copy that was told to stop actually unwind before giving up
# on the thread entirely. A read wedged in the kernel never returns, and the
# app must not be held hostage to it.
ABANDON_SECONDS = 10.0


class RunnerState(str, Enum):
    IDLE = "idle"
    READY_NO_DISC = "ready_no_disc"
    DISC_SETTLING = "disc_settling"
    IDENTIFYING = "identifying"
    DUPLICATE_WARNING = "duplicate_warning"
    NO_TARGET = "no_target"
    GRACE_COUNTDOWN = "grace_countdown"
    WORKING = "working"
    EJECTING = "ejecting"
    WAIT_DISC_REMOVED = "wait_disc_removed"
    PAUSED = "paused"
    ERROR_HOLD = "error_hold"
    JOB_COMPLETE = "job_complete"


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


@dataclass
class Event:
    # Which drive this happened in, as "D:". Stamped by JobRunner.emit rather
    # than by each construction site, so no event can be raised untagged. It
    # stays "" for a runner that was not bound to one particular drive, which
    # is every single-drive job.
    drive: str = field(default="", kw_only=True)


@dataclass
class StateChanged(Event):
    state: RunnerState
    detail: str = ""


@dataclass
class DiscDetected(Event):
    media: MediaInfo
    kind: DiscKind
    reason: str = ""


@dataclass
class DiscSized(Event):
    file_count: int
    total_bytes: int


@dataclass
class CountdownTick(Event):
    remaining: float
    total: float
    target_name: str


@dataclass
class Progress(Event):
    copied_bytes: int
    total_bytes: int
    current_file: str
    bytes_per_second: float = 0.0
    seconds_remaining: float | None = None

    @property
    def percent(self) -> float:
        if not self.total_bytes:
            return 100.0
        return min(100.0, self.copied_bytes / self.total_bytes * 100.0)


@dataclass
class EntryUpdated(Event):
    entry_id: str


@dataclass
class DiscFinished(Event):
    entry_id: str
    files_copied: int
    bytes_copied: int
    files_failed: int


@dataclass
class DiscTrouble(Event):
    """This disc is going badly enough to be worth offering a way out.

    Emitted once per disc, either because several files have failed to read or
    because the drive has gone quiet. The UI turns it into a button; nothing
    happens automatically, since a slow disc that eventually finishes is still
    better than one abandoned on a guess.
    """

    entry_id: str
    files_failed: int
    stalled: bool
    message: str


@dataclass
class DiscFailed(Event):
    entry_id: str | None
    message: str
    error_class: str = ErrorClass.UNKNOWN.value


@dataclass
class DuplicateWarning(Event):
    media: MediaInfo
    entry_id: str
    entry_name: str
    strength: str
    finished_utc: str | None


@dataclass
class NoTarget(Event):
    media: MediaInfo


@dataclass
class Ejected(Event):
    ok: bool
    message: str = ""


@dataclass
class LogLine(Event):
    message: str
    level: str = "info"


@dataclass
class JobComplete(Event):
    pass


# --------------------------------------------------------------------------
# Injected collaborators
# --------------------------------------------------------------------------


class Executor(Protocol):
    def submit(self, work: Callable[[], None]) -> None:
        ...


class ThreadExecutor:
    """Real execution: one daemon thread per unit of work."""

    def submit(self, work: Callable[[], None]) -> None:
        threading.Thread(target=work, daemon=True).start()


class InlineExecutor:
    """Runs work synchronously - used by tests to keep sessions deterministic."""

    def submit(self, work: Callable[[], None]) -> None:
        work()


class Ejector(Protocol):
    def eject(self, drive: str) -> bool:
        ...


class NullEjector:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def eject(self, drive: str) -> bool:
        self.calls.append(drive)
        return True


class Win32Ejector:
    def eject(self, drive: str) -> bool:
        from . import winapi

        return winapi.eject(drive).ok


class Copier(Protocol):
    def scan(self, source: Path) -> ScanResult:
        ...

    def copy(
        self,
        source: Path,
        destination: Path,
        cancel: threading.Event,
        on_progress: Callable[[CopyProgress], None],
        options: CopyOptions,
        scan: ScanResult | None,
        pause: threading.Event | None,
    ) -> CopyResult:
        ...


class RealCopier:
    def scan(self, source: Path) -> ScanResult:
        return scan_tree(source)

    def copy(
        self,
        source: Path,
        destination: Path,
        cancel: threading.Event,
        on_progress: Callable[[CopyProgress], None],
        options: CopyOptions,
        scan: ScanResult | None = None,
        pause: threading.Event | None = None,
    ) -> CopyResult:
        return copy_tree(
            source, destination, cancel, on_progress, options, scan, pause=pause
        )


class Ripper(Protocol):
    def available(self) -> bool:
        ...

    def rip(
        self,
        source: Path,
        destination: Path,
        cancel: threading.Event,
        on_progress: Callable[[CopyProgress], None],
        settings,
    ) -> EntryResult:
        ...


class UnavailableRipper:
    """Stands in until audio support lands; audio discs are skipped, not fatal."""

    def available(self) -> bool:
        return False

    def rip(self, source, destination, cancel, on_progress, settings) -> EntryResult:
        raise RuntimeError("audio_unsupported")


# --------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------


class ClaimRegistry:
    """Which drive is currently aiming at which folder.

    ``Job.next_pending`` is derived from the list rather than stored, which is
    what makes skip and redirect free - but it also means two drives asked at
    the same moment both get the same answer, and would copy two different
    discs into one folder. A claim is the missing piece: held from the moment
    a drive starts working towards a folder until it is finished with it.

    Deliberately in memory only. A claim describes what is happening in a
    drive right now, so it cannot outlive the session that made it - a job
    file carrying stale claims would strand folders no drive was holding.
    """

    def __init__(self) -> None:
        self._by_owner: dict[str, str] = {}

    def claim(self, owner: str, entry_id: str) -> None:
        self._by_owner[owner] = entry_id

    def release(self, owner: str) -> None:
        self._by_owner.pop(owner, None)

    def held_by_others(self, owner: str) -> set[str]:
        return {
            entry_id
            for holder, entry_id in self._by_owner.items()
            if holder != owner
        }

    def owner_of(self, entry_id: str) -> str | None:
        for holder, held in self._by_owner.items():
            if held == entry_id:
                return holder
        return None

    def as_dict(self) -> dict[str, str]:
        """entry_id -> drive, for painting the folder list."""
        return {entry_id: owner for owner, entry_id in self._by_owner.items()}


@dataclass
class _Work:
    """Everything the worker thread needs, and what it hands back."""

    entry_id: str
    media: MediaInfo
    kind: DiscKind
    destination: Path
    scan: ScanResult | None = None
    started: float = 0.0
    result: CopyResult | EntryResult | None = None
    error: BaseException | None = None
    # Set when the drive stopped answering and we gave up waiting for this
    # thread. Whatever it eventually reports is ignored.
    abandoned: bool = False


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        scanner,
        copier: Copier | None = None,
        ripper: Ripper | None = None,
        ejector: Ejector | None = None,
        executor: Executor | None = None,
        clock: Callable[[], float] = time.monotonic,
        emit: Callable[[Event], None] | None = None,
        kind_probe: Callable[[Path, str], KindProbe] = detect_disc_kind,
        drive: str = "",
        claims: "ClaimRegistry | None" = None,
    ) -> None:
        self.store = store
        self.scanner = scanner
        # Bound to one drive ("D:") this runner ignores discs in any other,
        # which is what lets several of them share a scanner and a job. Empty
        # means "whatever is loaded", the single-drive behaviour.
        self.drive = drive.upper()
        # Shared with the other drives' runners, so two of them never aim at
        # the same folder. A registry of its own when there is only one.
        self.claims = claims if claims is not None else ClaimRegistry()
        self.copier = copier or RealCopier()
        self.ripper = ripper or UnavailableRipper()
        self.ejector = ejector or NullEjector()
        self.executor = executor or ThreadExecutor()
        self.clock = clock
        self.kind_probe = kind_probe

        self.events: "queue.Queue[Event]" = queue.Queue()
        self._emit_hook = emit

        self.state = RunnerState.IDLE
        self.current_media: MediaInfo | None = None
        self.current_kind: DiscKind = DiscKind.UNKNOWN
        self.current_entry_id: str | None = None

        self._settling: MediaInfo | None = None
        self._settle_count = 0
        self._countdown_ends: float | None = None
        self._countdown_total: float = 0.0
        self._completed: MediaInfo | None = None
        self._completed_at: float = 0.0
        self._absent_polls = 0
        self._paused_from: RunnerState | None = None

        self.cancel_event = threading.Event()
        # Set while the user holds a copy. The worker consults it between files
        # and between chunks, so nothing is lost and nothing is re-read.
        self.pause_event = threading.Event()
        self._paused_at: float | None = None
        # Distinguishes the two ways a copy can be stopped: cancel leaves the
        # disc in the drive, skip hands it back.
        self._skip_requested = False
        self._corrupt_requested = False
        self._corrupt_at: float | None = None
        self._trouble_announced = False
        self._work: _Work | None = None
        self._results: "queue.Queue[_Work]" = queue.Queue()
        self._progress: "queue.Queue[CopyProgress]" = queue.Queue()
        self._last_progress_at = 0.0
        self._last_progress: CopyProgress | None = None

    # -- plumbing ---------------------------------------------------------

    @property
    def job(self):
        return self.store.job

    def emit(self, event: Event) -> None:
        # One place to stamp the drive, so every event the UI receives can be
        # routed to the right panel without each call site remembering to say
        # where it came from.
        if self.drive and not event.drive:
            event.drive = self.drive
        self.events.put(event)
        if self._emit_hook is not None:
            self._emit_hook(event)

    def drain(self) -> list[Event]:
        """Everything emitted since the last call. The UI pump uses this."""
        collected: list[Event] = []
        while True:
            try:
                collected.append(self.events.get_nowait())
            except queue.Empty:
                return collected

    def _set_state(self, state: RunnerState, detail: str = "") -> None:
        if self.state is state and not detail:
            return
        self.state = state
        self.emit(StateChanged(state, detail))

    def log(self, message: str, level: str = "info") -> None:
        self.emit(LogLine(message, level))

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self.job.is_complete:
            self._set_state(RunnerState.JOB_COMPLETE)
            self.emit(JobComplete())
            return
        self._set_state(RunnerState.READY_NO_DISC, "Aguardando disco")

    def pause(self) -> None:
        """Hold whatever is happening - a countdown or a transfer in progress.

        A running copy stays in WORKING rather than moving to PAUSED: the
        entry is still mid-write, and anything asking "is this folder busy?"
        - the row protected from renaming, the poller that must not go looking
        for a new disc - has to keep getting yes.
        """
        if self.state is RunnerState.WORKING:
            if self.pause_event.is_set():
                return
            self.pause_event.set()
            self._paused_at = self.clock()
            self._set_state(RunnerState.WORKING, "Copia pausada")
            self.log("Copia pausada.")
            return
        if self.state is RunnerState.PAUSED:
            return
        self._paused_from = self.state
        self._countdown_ends = None
        self._set_state(RunnerState.PAUSED, "Pausado")

    @property
    def is_paused(self) -> bool:
        """Held either way - idle-paused, or mid-copy paused."""
        return self.state is RunnerState.PAUSED or (
            self.state is RunnerState.WORKING and self.pause_event.is_set()
        )

    def resume(self) -> None:
        if self.state is RunnerState.WORKING:
            if not self.pause_event.is_set():
                return
            self.pause_event.clear()
            # Speed and ETA come from elapsed time, so without this a coffee
            # break would be reported as the network having got slower.
            if self._paused_at is not None and self._work is not None:
                self._work.started += self.clock() - self._paused_at
            self._paused_at = None
            entry = self._current_entry()
            self._set_state(
                RunnerState.WORKING,
                f"Copiando para '{entry.folder_name}'" if entry else "Copiando",
            )
            self.log("Copia retomada.")
            return

        if self.state is not RunnerState.PAUSED:
            return
        previous = self._paused_from
        self._paused_from = None

        if self.current_media is None:
            # The disc was taken out while paused; nothing to go back to.
            self._set_state(RunnerState.READY_NO_DISC, "Aguardando disco")
            return

        if previous in (
            RunnerState.GRACE_COUNTDOWN,
            RunnerState.DUPLICATE_WARNING,
            RunnerState.NO_TARGET,
        ):
            # Re-running identification is cheap (no subprocess, just the kind
            # probe and a lookup) and naturally lands back in whichever of the
            # three states applies, instead of the three needing to be
            # reconstructed and re-announced by hand here.
            self._identify(self.current_media, announce=False)
        else:
            self._set_state(RunnerState.READY_NO_DISC, "Aguardando disco")

    @property
    def is_busy(self) -> bool:
        return self.state is RunnerState.WORKING

    # -- polling ----------------------------------------------------------

    def poll(self) -> None:
        """Look at the drive. Called about once a second."""
        if self.state in (
            RunnerState.IDLE,
            RunnerState.PAUSED,
            RunnerState.WORKING,
            RunnerState.IDENTIFYING,
            RunnerState.EJECTING,
            RunnerState.JOB_COMPLETE,
        ):
            return

        loaded = self._mine(self.scanner.scan())

        if self.state is RunnerState.WAIT_DISC_REMOVED:
            self._poll_removal(loaded)
            return

        if self.state in (
            RunnerState.DUPLICATE_WARNING,
            RunnerState.NO_TARGET,
            RunnerState.ERROR_HOLD,
        ):
            # Waiting on the user, but notice if they simply took the disc out.
            if not loaded:
                self._forget_disc()
                self._set_state(RunnerState.READY_NO_DISC, "Aguardando disco")
            return

        if self.state is RunnerState.GRACE_COUNTDOWN:
            if not self._still_present(loaded):
                self.log("Disco removido antes do inicio da copia.", "warn")
                self._forget_disc()
                self._set_state(RunnerState.READY_NO_DISC, "Aguardando disco")
            return

        self._poll_for_new_disc(loaded)

    def _poll_for_new_disc(self, loaded: list[MediaInfo]) -> None:
        if not loaded:
            self._settling = None
            self._settle_count = 0
            self._set_state(RunnerState.READY_NO_DISC, "Aguardando disco")
            return

        candidate = self._pick_candidate(loaded)
        if candidate is None:
            return

        if identity_match(candidate, self._completed) is not MatchStrength.NONE:
            if self.clock() - self._completed_at < COMPLETED_GRACE_SECONDS:
                self._set_state(
                    RunnerState.WAIT_DISC_REMOVED, "Remova o disco concluido"
                )
                return
            self._completed = None

        if identity_match(candidate, self._settling) is MatchStrength.NONE:
            self._settling = candidate
            self._settle_count = 1
            self._set_state(RunnerState.DISC_SETTLING, "Lendo disco...")
            return

        self._settle_count += 1
        if self._settle_count < SETTLE_POLLS:
            return

        self._settling = None
        self._settle_count = 0
        self._identify(candidate)

    def _mine(self, loaded: list[MediaInfo]) -> list[MediaInfo]:
        """Only the discs in the drive this runner was bound to.

        Unbound (drive="") it sees everything, which is the single-drive
        behaviour: one runner, every drive, one disc at a time.
        """
        if not self.drive:
            return loaded
        return [media for media in loaded if media.drive == self.drive]

    def _pick_candidate(self, loaded: list[MediaInfo]) -> MediaInfo | None:
        """Take drives in letter order, so an unbound runner is deterministic.

        Bound to a drive there is only ever one candidate; it is a pool of
        runners, one per drive, that copies two discs at once - see DrivePool.
        """
        return sorted(loaded, key=lambda media: media.drive)[0] if loaded else None

    def _still_present(self, loaded: list[MediaInfo]) -> bool:
        return any(
            identity_match(media, self.current_media) is not MatchStrength.NONE
            for media in loaded
        )

    def _poll_removal(self, loaded: list[MediaInfo]) -> None:
        if self._still_present_identity(loaded, self._completed):
            self._absent_polls = 0
            return
        self._absent_polls += 1
        if self._absent_polls < SETTLE_POLLS:
            return
        self._absent_polls = 0
        self._completed = None
        self._forget_disc()
        if self.job.is_complete:
            self._set_state(RunnerState.JOB_COMPLETE, "Backup concluido")
            self.emit(JobComplete())
        else:
            self._set_state(RunnerState.READY_NO_DISC, "Aguardando disco")

    @staticmethod
    def _still_present_identity(loaded, reference) -> bool:
        if reference is None:
            return False
        return any(
            identity_match(media, reference) is not MatchStrength.NONE
            for media in loaded
        )

    def _forget_disc(self) -> None:
        self.current_media = None
        self.current_kind = DiscKind.UNKNOWN
        self.current_entry_id = None
        self._countdown_ends = None
        # Whatever folder this drive was holding goes back to the pool. This
        # is already the one place current_entry_id is cleared, so the claim
        # cannot outlive the aim.
        self.claims.release(self._owner)

    # -- claiming a folder ------------------------------------------------

    @property
    def _owner(self) -> str:
        """This drive's key in the shared claim registry."""
        return self.drive or "*"

    def _take_target(self) -> DiscEntry | None:
        """The next folder this drive may work towards, claimed as it is taken.

        Skipping what other drives hold is the whole difference between one
        drive and several: without it both would be handed the same answer by
        next_pending() and copy two discs into one folder.
        """
        target = self.job.next_pending(ignoring=self.claims.held_by_others(self._owner))
        if target is not None:
            self.claims.claim(self._owner, target.entry_id)
        return target

    # -- identification ---------------------------------------------------

    def _identify(self, media: MediaInfo, announce: bool = True) -> None:
        """Work out what this disc is and where it should go.

        ``announce=False`` is used by ``resume()`` to recompute the same
        DUPLICATE_WARNING/NO_TARGET/GRACE_COUNTDOWN state after a pause
        without re-logging a "disc detected" line for a disc that was never
        actually re-inserted.
        """
        self.current_media = media
        self._set_state(RunnerState.IDENTIFYING, "Identificando disco...")

        probe = self.kind_probe(media.root, media.fs_name)
        self.current_kind = probe.kind
        self.emit(DiscDetected(media, probe.kind, probe.reason))
        if announce:
            self.log(f"Disco detectado: {media.display_name} ({probe.reason})")

        duplicate = self._find_duplicate(media)
        if duplicate is not None:
            entry, strength = duplicate
            self.current_entry_id = None
            self._set_state(RunnerState.DUPLICATE_WARNING, "Disco ja copiado")
            self.emit(
                DuplicateWarning(
                    media=media,
                    entry_id=entry.entry_id,
                    entry_name=entry.folder_name,
                    strength=strength.value,
                    finished_utc=entry.finished_utc,
                )
            )
            return

        target = self._take_target()
        if target is None:
            self._set_state(RunnerState.NO_TARGET, "Nenhuma pasta pendente")
            self.emit(NoTarget(media))
            return

        self.current_entry_id = target.entry_id
        self._begin_countdown()

    def _find_duplicate(self, media: MediaInfo):
        """Warn, never block: CD serials can legitimately collide."""
        entry = self.job.find_by_serial(media.serial)
        if entry is not None and entry.status is EntryStatus.DONE:
            return entry, MatchStrength.STRONG
        if media.serial:
            return None
        for candidate in self.job.entries:
            if candidate.status is not EntryStatus.DONE or not candidate.media:
                continue
            if candidate.media.label and candidate.media.label == media.label:
                return candidate, MatchStrength.WEAK
        return None

    # -- countdown --------------------------------------------------------

    def _begin_countdown(self, seconds: float | None = None) -> None:
        entry = self._current_entry()
        if entry is None:
            self._set_state(RunnerState.NO_TARGET, "Nenhuma pasta pendente")
            return

        if seconds is None:
            seconds = float(self.job.settings.grace_seconds)
        if not self.job.settings.auto_copy or seconds <= 0:
            # grace_seconds = 0 means "never start on its own".
            self._countdown_ends = None
            self._set_state(RunnerState.GRACE_COUNTDOWN, "Aguardando confirmacao")
            self.emit(CountdownTick(0.0, 0.0, entry.folder_name))
            return

        self._countdown_total = seconds
        self._countdown_ends = self.clock() + seconds
        self._set_state(RunnerState.GRACE_COUNTDOWN, f"Iniciando em {int(seconds)} s")
        self.emit(CountdownTick(seconds, seconds, entry.folder_name))

    def _current_entry(self):
        if self.current_entry_id is None:
            return None
        return self.job.entry_by_id(self.current_entry_id)

    # -- ticking ----------------------------------------------------------

    def tick(self) -> None:
        """Countdown and worker results. Called about ten times a second."""
        self._drain_progress()
        self._drain_results()

        if self.state is RunnerState.WORKING:
            self._check_stall()
            self._check_abandon()

        if self.state is not RunnerState.GRACE_COUNTDOWN or self._countdown_ends is None:
            return

        remaining = self._countdown_ends - self.clock()
        entry = self._current_entry()
        name = entry.folder_name if entry else ""
        if remaining > 0:
            self.emit(CountdownTick(remaining, self._countdown_total, name))
            return

        self._countdown_ends = None
        self.start_now()
        # Work submitted synchronously (or already finished on its thread) has
        # its result waiting now; picking it up here rather than next tick keeps
        # the UI a beat tighter and makes inline execution behave identically.
        self._drain_progress()
        self._drain_results()

    def _drain_progress(self) -> None:
        latest: CopyProgress | None = None
        while True:
            try:
                latest = self._progress.get_nowait()
            except queue.Empty:
                break
        if latest is None or self._work is None:
            return

        self._last_progress_at = self.clock()
        self._last_progress = latest
        if latest.files_failed >= TROUBLE_FILE_THRESHOLD:
            self._announce_trouble(latest.files_failed, stalled=False)

        elapsed = max(1e-6, self.clock() - self._work.started)
        speed = latest.copied_bytes / elapsed
        remaining_bytes = max(0, latest.total_bytes - latest.copied_bytes)
        eta = remaining_bytes / speed if speed > 0 else None
        self.emit(
            Progress(
                copied_bytes=latest.copied_bytes,
                total_bytes=latest.total_bytes,
                current_file=latest.current_file,
                bytes_per_second=speed,
                seconds_remaining=eta,
            )
        )

    def _check_stall(self) -> None:
        """A drive that has gone silent is the other face of a bad disc."""
        if self._work is None or self.pause_event.is_set() or self._trouble_announced:
            return
        since = self.clock() - (self._last_progress_at or self._work.started)
        if since >= STALL_SECONDS:
            failed = self._last_progress.files_failed if self._last_progress else 0
            self._announce_trouble(failed, stalled=True)

    def _check_abandon(self) -> None:
        """Stop waiting for a worker that was told to stop and has not.

        Its cancel flag is already set and it still holds a reference to it, so
        it will unwind on its own if the read ever comes back. From here on its
        result is ignored - the alternative is the whole app waiting on a drive
        that may never answer again.
        """
        if not self._corrupt_requested or self._corrupt_at is None:
            return
        if self.clock() - self._corrupt_at < ABANDON_SECONDS:
            return
        work = self._work
        if work is None:
            return
        work.abandoned = True
        self.log(
            "A unidade nao respondeu ao pedido de parada; a copia foi "
            "abandonada e o disco marcado como defeituoso.",
            "warn",
        )
        self._finish_corrupted(work, None)

    def _announce_trouble(self, files_failed: int, stalled: bool) -> None:
        if self._trouble_announced or self._work is None:
            return
        self._trouble_announced = True
        if stalled:
            message = (
                "A unidade parou de responder. O disco pode estar danificado."
            )
        else:
            message = (
                f"{files_failed} arquivo(s) nao puderam ser lidos ate agora."
            )
        self.emit(
            DiscTrouble(
                entry_id=self._work.entry_id,
                files_failed=files_failed,
                stalled=stalled,
                message=message,
            )
        )
        self.log(
            f"{message} Use 'Disco defeituoso' para marcar este disco e "
            "seguir para o proximo.",
            "warn",
        )

    def _drain_results(self) -> None:
        while True:
            try:
                work = self._results.get_nowait()
            except queue.Empty:
                return
            self._finish_work(work)

    # -- commands ---------------------------------------------------------

    def start_now(self) -> None:
        """Begin copying the loaded disc into its target immediately."""
        if self.state not in (
            RunnerState.GRACE_COUNTDOWN,
            RunnerState.DUPLICATE_WARNING,
            RunnerState.NO_TARGET,
        ):
            return
        entry = self._current_entry()
        media = self.current_media
        if entry is None or media is None:
            return
        self._countdown_ends = None
        self._begin_work(entry, media)

    def skip_disc(self) -> None:
        """Dismiss this disc without consuming an entry.

        Note the asymmetry: skipping a *disc* leaves every folder untouched,
        whereas marking an *entry* skipped is a separate action on the list.

        Mid-copy this stops the transfer first. The tray is not opened here -
        the worker still has the drive open, so the eject waits until it has
        actually unwound, in ``_work_failed``.
        """
        media = self.current_media
        if media is None:
            return

        if self.state is RunnerState.WORKING:
            self._skip_requested = True
            self.pause_event.clear()  # a held worker must wake to see the stop
            self.cancel_event.set()
            self.log(f"Interrompendo a copia para pular {media.display_name}...")
            self._set_state(RunnerState.WORKING, "Interrompendo para ejetar...")
            return

        self.log(f"Disco {media.display_name} pulado pelo usuario.")
        # Remember it as handled, or the very next poll would see the disc still
        # sitting in the tray and offer it again.
        self._completed = media
        self._completed_at = self.clock()
        self._forget_disc()
        self._eject_and_wait(expect=media)

    def send_to(self, entry_id: str) -> None:
        """Redirect the loaded disc straight to any entry, no grace period.

        Picking a destination through the dialog is itself the confirmation -
        unlike the auto-detected next-pending disc there is no misclick this
        would be recovering from, so it starts immediately rather than
        stacking a countdown on top of a choice that was already deliberate.
        """
        entry = self.job.entry_by_id(entry_id)
        if entry is None or self.current_media is None:
            return
        if self.state not in (
            RunnerState.GRACE_COUNTDOWN,
            RunnerState.DUPLICATE_WARNING,
            RunnerState.NO_TARGET,
            RunnerState.PAUSED,  # the UI pauses while its picker dialog is open
        ):
            return
        self._paused_from = None
        self.current_entry_id = entry_id
        self.claims.claim(self._owner, entry_id)
        self._countdown_ends = None
        self.log(f"Disco redirecionado para '{entry.folder_name}'.")
        self._begin_work(entry, self.current_media)
        # Mirrors tick()'s handling of an expired countdown: with a
        # synchronous executor the result is already waiting.
        self._drain_progress()
        self._drain_results()

    def set_collecting(self, entry_id: str, on: bool = True) -> None:
        """Make one folder keep receiving every disc, or stop it doing so.

        Takes effect for the disc already in the drive as well as the next
        ones: a target chosen while the countdown is running is re-aimed rather
        than applying only from the following disc, which is the opposite of
        what flipping the switch right now looks like it should do.
        """
        entry = self.job.entry_by_id(entry_id)
        if entry is None or bool(entry.collecting) == bool(on):
            return

        self.job.set_collecting(entry_id, on)
        self.store.save()
        self.emit(EntryUpdated(entry_id))
        if on:
            self.log(
                f"Acumulando em '{entry.folder_name}': todos os discos vao para "
                "esta pasta ate ser desligado."
            )
        else:
            self.log(
                f"'{entry.folder_name}' nao esta mais acumulando "
                f"({entry.disc_count} disco(s) recebidos)."
            )

        if self.state is RunnerState.WORKING:
            return  # the copy under way keeps its target; this is for the next disc
        if self.current_media is not None and self.state in (
            RunnerState.GRACE_COUNTDOWN,
            RunnerState.DUPLICATE_WARNING,
            RunnerState.NO_TARGET,
        ):
            self._identify(self.current_media, announce=False)
        elif self.state is RunnerState.JOB_COMPLETE and not self.job.is_complete:
            self._set_state(RunnerState.READY_NO_DISC, "Aguardando disco")

    def copy_anyway(self) -> None:
        """Answer to a duplicate warning: copy into the next pending folder."""
        if self.state is not RunnerState.DUPLICATE_WARNING:
            return
        target = self._take_target()
        if target is None:
            self._set_state(RunnerState.NO_TARGET, "Nenhuma pasta pendente")
            self.emit(NoTarget(self.current_media))
            return
        self.current_entry_id = target.entry_id
        self._begin_countdown()

    def cancel(self) -> None:
        """Stop the transfer and leave the disc where it is.

        The difference from ``skip_disc`` is only what happens to the disc:
        both keep the files already written and put the folder back to
        pending, but this one expects you to try again with the same disc.
        """
        if self.state is not RunnerState.WORKING:
            return
        self.pause_event.clear()  # a held worker must wake to see the cancel
        self.cancel_event.set()
        self._set_state(RunnerState.WORKING, "Interrompendo...")

    def mark_corrupted(self) -> None:
        """Give up on this disc, record why in its folder, and move on.

        Unlike skip and cancel, this one is a verdict: the folder is left
        FAILED with a report of what could not be read, so an incomplete
        folder can never be mistaken later for a finished one. The disc is
        ejected and the next pending folder becomes the target.
        """
        if self.state is not RunnerState.WORKING or self._work is None:
            return
        if self._corrupt_requested:
            return
        self._corrupt_requested = True
        self._corrupt_at = self.clock()
        self.pause_event.clear()  # a held worker must wake to see the stop
        self.cancel_event.set()
        self.log("Disco marcado como defeituoso. Interrompendo a copia...", "warn")
        self._set_state(RunnerState.WORKING, "Marcando disco como defeituoso...")

    def eject_now(self) -> None:
        self._eject_and_wait(expect=self.current_media)

    def retry_entry(self, entry_id: str) -> None:
        entry = self.job.entry_by_id(entry_id)
        if entry is None:
            return
        entry.status = EntryStatus.PENDING
        entry.error = None
        self.store.save()
        self.emit(EntryUpdated(entry_id))
        if self.state in (RunnerState.JOB_COMPLETE, RunnerState.ERROR_HOLD):
            self._set_state(RunnerState.READY_NO_DISC, "Aguardando disco")

    def mark_entry_defective(self, entry_id: str, note: str = "") -> bool:
        """Declare a disc unreadable without attempting it at all.

        The sibling of ``mark_corrupted``, for when there is nothing to
        interrupt: the disc is visibly broken, or the drive will not mount it,
        so it never gets as far as a copy. The folder is marked skipped rather
        than failed - it was not tried and found wanting, it was written off -
        and the same report is left inside saying so.

        Returns False when a copy of this very folder is running, since then
        there *is* something to stop first and ``mark_corrupted`` is the
        command that does it.
        """
        entry = self.job.entry_by_id(entry_id)
        if entry is None:
            return False
        if entry.status is EntryStatus.IN_PROGRESS:
            return False

        entry.status = EntryStatus.SKIPPED
        entry.error = "disc_corrupted"
        entry.needs_review = True
        entry.finished_utc = utc_now()
        if note.strip():
            entry.notes = note.strip()

        folder = self.job.folder_for(entry)
        failed = list(entry.result.failed_files) if entry.result else []
        copied = entry.result.files_copied if entry.result else 0
        try:
            report = write_defect_report(
                folder=folder,
                disc_name=entry.media.display_name if entry.media else "(não lido)",
                reason="marcado manualmente como disco defeituoso",
                files_copied=copied,
                failed_files=failed,
                when=utc_now().replace("T", " ").rstrip("Z"),
                serial=entry.media.serial if entry.media else None,
                note=note,
                skipped=True,
                bytes_copied=entry.result.bytes_copied if entry.result else 0,
                # Only set if this disc was read on an earlier attempt; a
                # folder written off sight-unseen has no totals at all, and
                # the report says so rather than printing a measured-looking 0%.
                total_bytes=entry.media.total_bytes if entry.media else 0,
                total_files=entry.media.file_count if entry.media else 0,
            )
            self.log(f"Relatorio do defeito gravado em '{report.name}'.")
        except OSError as exc:
            # The mark still stands; only the note on disk failed.
            self.log(f"Nao foi possivel gravar o relatorio do defeito: {exc}", "error")

        self.store.save()
        self.emit(EntryUpdated(entry_id))
        self.log(
            f"'{entry.folder_name}' pulada: disco defeituoso"
            + (f" ({note.strip()})" if note.strip() else "")
            + ".",
            "warn",
        )
        if self.state is RunnerState.JOB_COMPLETE and not self.job.is_complete:
            self._set_state(RunnerState.READY_NO_DISC, "Aguardando disco")
        return True

    def dismiss_error(self) -> None:
        if self.state is RunnerState.ERROR_HOLD:
            self._set_state(RunnerState.READY_NO_DISC, "Aguardando disco")

    # -- work -------------------------------------------------------------

    def _begin_work(self, entry, media: MediaInfo) -> None:
        destination = self.job.folder_for(entry)
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._hold_error(entry.entry_id, f"Nao foi possivel criar a pasta: {exc}", exc)
            return

        work = _Work(
            entry_id=entry.entry_id,
            media=media,
            kind=self.current_kind,
            destination=destination,
            started=self.clock(),
        )
        self._work = work
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self._paused_at = None
        self._skip_requested = False
        self._corrupt_requested = False
        self._corrupt_at = None
        self._trouble_announced = False
        self._last_progress = None
        self._last_progress_at = work.started

        entry.status = EntryStatus.IN_PROGRESS
        entry.attempts += 1
        entry.started_utc = utc_now()
        entry.error = None
        self.store.save()
        self.emit(EntryUpdated(entry.entry_id))

        self._set_state(
            RunnerState.WORKING,
            f"Copiando para '{entry.folder_name}'",
        )
        self.log(
            f"Iniciando {media.display_name} -> '{entry.folder_name}' "
            f"({work.kind.value})."
        )
        self.executor.submit(lambda: self._run_work(work))

    def _run_work(self, work: _Work) -> None:
        """Runs on the worker thread. Never touches state directly."""
        try:
            if work.kind is DiscKind.AUDIO:
                work.result = self.ripper.rip(
                    work.media.root,
                    work.destination,
                    self.cancel_event,
                    self._progress.put,
                    self.job.settings,
                )
            else:
                scan = work.scan or self.copier.scan(work.media.root)
                work.scan = scan
                self.emit(DiscSized(scan.file_count, scan.total_bytes))
                self._check_free_space(work.destination, scan.total_bytes)
                work.result = self.copier.copy(
                    work.media.root,
                    work.destination,
                    self.cancel_event,
                    self._progress.put,
                    self._copy_options(),
                    scan,
                    self.pause_event,
                )
        except BaseException as exc:  # reported, never raised out of the thread
            work.error = exc
        finally:
            self._results.put(work)

    def _copy_options(self) -> CopyOptions:
        settings = self.job.settings
        return CopyOptions(
            on_file_error=settings.on_file_error,
            resume_existing=settings.resume_existing,
        )

    def _check_free_space(self, destination: Path, needed: int) -> None:
        """Warn rather than block: on a share this figure is often wrong."""
        try:
            usage = shutil.disk_usage(destination)
        except OSError:
            return
        required = needed * FREE_SPACE_MARGIN + FREE_SPACE_SLACK_BYTES
        if usage.free < required:
            self.log(
                f"Espaco livre no destino pode ser insuficiente: "
                f"{usage.free} disponivel, ~{int(required)} necessario.",
                "warn",
            )

    def _finish_work(self, work: _Work) -> None:
        if work.abandoned:
            # We stopped waiting for this one and already wrote it off; a late
            # answer from a drive that had wedged must not reopen a folder the
            # user has moved on from.
            return
        self._work = None
        entry = self.job.entry_by_id(work.entry_id)
        if entry is None:
            return

        duration = self.clock() - work.started
        if work.error is not None:
            self._work_failed(entry, work, duration)
            return

        record = work.media.to_record(
            file_count=work.scan.file_count if work.scan else 0,
            total_bytes=work.scan.total_bytes if work.scan else 0,
        )
        outcome = _to_entry_result(work.result, duration)

        entry.status = EntryStatus.DONE
        entry.disc_kind = work.kind
        entry.finished_utc = utc_now()
        entry.error = None
        if entry.collecting:
            entry.absorb(record, outcome)
            # Discs pooled into one folder share filenames as a matter of
            # course (AUTORUN.INF, INDEX.HTM), so a rename here is the
            # mechanism doing its job rather than something to flag. An
            # unreadable file still is.
            entry.needs_review = entry.needs_review or outcome.files_failed > 0
        else:
            entry.media = record
            entry.result = outcome
            # A rename is not a failure, but it means the folder now holds files
            # from more than one disc under the same name - worth a look.
            entry.needs_review = outcome.files_failed > 0 or outcome.files_renamed > 0

        # A folder that has just been copied successfully is not defective any
        # more; leaving the old note behind would have it contradict itself.
        self._clear_defect_report(work.destination)

        self.store.save()
        self.emit(EntryUpdated(entry.entry_id))
        # This disc's own numbers, not the folder's running total, so the
        # message still describes what just happened.
        self.emit(
            DiscFinished(
                entry_id=entry.entry_id,
                files_copied=outcome.files_copied,
                bytes_copied=outcome.bytes_copied,
                files_failed=outcome.files_failed,
            )
        )
        notes = []
        if outcome.files_failed:
            notes.append(f"{outcome.files_failed} arquivo(s) com falha")
        if outcome.files_renamed:
            notes.append(
                f"{outcome.files_renamed} arquivo(s) ja existiam com "
                "conteudo diferente e foram salvos com outro nome"
            )
        warn = f" ({'; '.join(notes)})" if notes else ""
        if entry.collecting:
            self.log(
                f"'{entry.folder_name}' recebeu o disco {entry.disc_count} "
                f"- acumulando, continue inserindo{warn}."
            )
        else:
            self.log(f"'{entry.folder_name}' concluida{warn}.")

        self._completed = work.media
        self._completed_at = self.clock()
        self._forget_disc()
        self._eject_and_wait(expect=work.media)

    def _clear_defect_report(self, folder: Path) -> None:
        report = folder / DEFECT_REPORT_NAME
        try:
            if report.is_file():
                report.unlink()
                self.log(f"'{report.name}' removido: a copia foi refeita com sucesso.")
        except OSError:
            pass  # a stale note is not worth failing a good copy over

    def _work_failed(self, entry, work: _Work, duration: float) -> None:
        exc = work.error
        if isinstance(exc, BackupCancelled) and self._corrupt_requested:
            # The copy stopped on its own before the abandon watchdog fired,
            # so we have the real list of files that failed.
            self._finish_corrupted(work, getattr(exc, "result", None))
            return

        if isinstance(exc, BackupCancelled):
            entry.status = EntryStatus.PENDING
            if entry.result is None:
                entry.result = EntryResult()
            entry.result.partial = True
            self.store.save()
            self.emit(EntryUpdated(entry.entry_id))

            if self._skip_requested:
                # The worker has let go of the drive by now, so the tray can
                # open. The folder keeps what was written and goes back to
                # pending: nothing was consumed by the disc being handed back.
                self._skip_requested = False
                self.log(
                    f"Copia de '{entry.folder_name}' interrompida e disco pulado. "
                    "Os arquivos ja gravados foram mantidos e a pasta continua "
                    "pendente.",
                    "warn",
                )
                self._completed = work.media
                self._completed_at = self.clock()
                self._forget_disc()
                self._eject_and_wait(expect=work.media)
                return

            self.log(
                f"Copia de '{entry.folder_name}' cancelada. Os arquivos ja "
                "gravados foram mantidos; reinserir o disco continua de onde parou.",
                "warn",
            )
            self._set_state(RunnerState.READY_NO_DISC, "Aguardando disco")
            return

        if isinstance(exc, RuntimeError) and str(exc) == "audio_unsupported":
            entry.status = EntryStatus.SKIPPED
            entry.disc_kind = DiscKind.AUDIO
            entry.error = "audio_unsupported"
            entry.needs_review = True
            entry.finished_utc = utc_now()
            self.store.save()
            self.emit(EntryUpdated(entry.entry_id))
            self.log(
                f"'{entry.folder_name}': CD de audio detectado, mas o ffmpeg com "
                "libcdio nao esta disponivel.",
                "warn",
            )
            self._completed = work.media
            self._completed_at = self.clock()
            self._forget_disc()
            self._eject_and_wait(expect=work.media)
            return

        error_class = classify_os_error(exc) if isinstance(exc, OSError) else ErrorClass.UNKNOWN
        entry.status = EntryStatus.FAILED
        entry.finished_utc = utc_now()
        entry.error = str(exc)
        if entry.result is None:
            entry.result = EntryResult()
        entry.result.partial = True
        entry.result.duration_seconds = duration
        self.store.save()
        self.emit(EntryUpdated(entry.entry_id))
        self.emit(DiscFailed(entry.entry_id, str(exc), error_class.value))
        self.log(f"Falha em '{entry.folder_name}': {exc}", "error")
        self._hold_error(entry.entry_id, str(exc), exc)

    def _finish_corrupted(self, work: _Work, result) -> None:
        """Write the disc off: folder FAILED, report on disk, tray open.

        *result* is the partial CopyResult when the copy managed to unwind, and
        None when the drive never answered; the report says which of the two
        happened rather than inventing numbers for the second case.
        """
        self._corrupt_requested = False
        self._corrupt_at = None
        self._work = None
        entry = self.job.entry_by_id(work.entry_id)
        if entry is None:
            return

        failed = list(getattr(result, "failed_files", None) or [])
        if result is not None:
            copied = result.files_copied
            copied_bytes = result.bytes_copied
            reason = f"{len(failed)} arquivo(s) não puderam ser lidos"
        else:
            # The drive never answered, so the last heartbeat is all there is.
            copied = self._last_progress.files_copied if self._last_progress else 0
            copied_bytes = self._last_progress.copied_bytes if self._last_progress else 0
            reason = "a unidade parou de responder durante a cópia"

        entry.status = EntryStatus.FAILED
        entry.disc_kind = work.kind
        entry.finished_utc = utc_now()
        entry.error = "disc_corrupted"
        entry.needs_review = True
        entry.media = work.media.to_record(
            file_count=work.scan.file_count if work.scan else 0,
            total_bytes=work.scan.total_bytes if work.scan else 0,
        )
        if entry.result is None:
            entry.result = EntryResult()
        entry.result.partial = True
        entry.result.files_copied = copied
        entry.result.files_failed = len(failed)
        entry.result.failed_files = [failure.to_dict() for failure in failed]
        if result is not None:
            entry.result.bytes_copied = result.bytes_copied
        entry.result.duration_seconds = self.clock() - work.started

        try:
            report = write_defect_report(
                folder=work.destination,
                disc_name=work.media.display_name,
                reason=reason,
                files_copied=copied,
                failed_files=failed,
                when=utc_now().replace("T", " ").rstrip("Z"),
                serial=work.media.serial,
                bytes_copied=copied_bytes,
                # The scan ran before the copy, so the disc's real size is
                # known even when the drive died mid-transfer.
                total_bytes=work.scan.total_bytes if work.scan else 0,
                total_files=work.scan.file_count if work.scan else 0,
            )
            self.log(f"Relatorio do defeito gravado em '{report.name}'.")
        except OSError as exc:
            # Never let the note-taking be what stops the batch moving on.
            self.log(f"Nao foi possivel gravar o relatorio do defeito: {exc}", "error")

        self.store.save()
        self.emit(EntryUpdated(entry.entry_id))
        self.emit(
            DiscFailed(entry.entry_id, "disc_corrupted", ErrorClass.SOURCE_MEDIA.value)
        )
        self.log(
            f"'{entry.folder_name}' marcada como disco defeituoso ({reason}). "
            f"{copied} arquivo(s) copiados foram mantidos.",
            "warn",
        )

        self._completed = work.media
        self._completed_at = self.clock()
        self._forget_disc()
        self._eject_and_wait(expect=work.media)

    def _hold_error(self, entry_id: str | None, message: str, exc: BaseException) -> None:
        self._forget_disc()
        self._set_state(RunnerState.ERROR_HOLD, message)

    # -- eject ------------------------------------------------------------

    def _eject_and_wait(self, expect: MediaInfo | None) -> None:
        """Eject is advisory. We always fall through to waiting for removal.

        External USB drives can report a successful IOCTL while the tray never
        moves, so nothing is ever allowed to block on it.
        """
        drive = (expect or self._completed or self.current_media)
        drive_letter = drive.drive if drive else ""

        if self.job.settings.auto_eject and drive_letter:
            self._set_state(RunnerState.EJECTING, "Ejetando disco")
            try:
                ok = self.ejector.eject(drive_letter)
            except Exception as exc:  # an eject failure must never end a batch
                ok = False
                self.log(f"Falha ao ejetar {drive_letter}: {exc}", "warn")
            self.emit(Ejected(ok, drive_letter))
            if not ok:
                self.log(f"Nao foi possivel ejetar {drive_letter}.", "warn")

        if expect is None:
            self._completed = None
            if self.job.is_complete:
                self._set_state(RunnerState.JOB_COMPLETE, "Backup concluido")
                self.emit(JobComplete())
            else:
                self._set_state(RunnerState.READY_NO_DISC, "Aguardando disco")
            return

        self._absent_polls = 0
        self._set_state(RunnerState.WAIT_DISC_REMOVED, "Remova o disco")


def _to_entry_result(result, duration: float) -> EntryResult:
    if isinstance(result, EntryResult):
        result.duration_seconds = duration
        return result
    entry_result = EntryResult(
        files_copied=getattr(result, "files_copied", 0),
        bytes_copied=getattr(result, "bytes_copied", 0),
        files_failed=getattr(result, "files_failed", 0),
        failed_files=[
            failure.to_dict() for failure in getattr(result, "failed_files", [])
        ],
        files_skipped_existing=getattr(result, "files_skipped_existing", 0),
        files_renamed=getattr(result, "files_renamed", 0),
        renamed_files=[
            item.to_dict() for item in getattr(result, "renamed_files", [])
        ],
        duration_seconds=duration,
        partial=getattr(result, "partial", False),
    )
    return entry_result


# --------------------------------------------------------------------------
# Several drives at once
# --------------------------------------------------------------------------


class DrivePool:
    """One JobRunner per optical drive, over one job.

    Two drives roughly double throughput on this workload, because the
    bottleneck is the drive's read head and not the share it writes to: an
    optical drive manages single-digit MB/s, and less on a disc that has been
    sitting in a box for fifteen years, while the destination is a LAN share
    an order of magnitude faster than that.

    Nothing here is threaded. The runners are stepped from the same ``poll``
    and ``tick`` the single-drive app already used, which run on the UI's
    pump; only the copies themselves are on worker threads, exactly as
    before. That is what keeps the job model free of locks - two runners can
    never be inside it at the same moment.

    The one thing they genuinely share is *which folder is whose*, and that
    lives in the ClaimRegistry every runner is handed.
    """

    def __init__(
        self,
        store: JobStore,
        scanner,
        drives: "list[str] | None" = None,
        **runner_kwargs,
    ) -> None:
        self.store = store
        self.scanner = scanner
        self.claims = ClaimRegistry()
        found = drives if drives is not None else _scanner_drives(scanner)
        # Sorted, so "the first drive" means the same thing every session and
        # the panels do not reshuffle between runs.
        self.drives: list[str] = sorted({d.upper() for d in found})
        self.runners: dict[str, JobRunner] = {
            drive: JobRunner(
                store=store,
                scanner=scanner,
                drive=drive,
                claims=self.claims,
                **runner_kwargs,
            )
            for drive in self.drives
        }

    # -- plumbing ---------------------------------------------------------

    @property
    def job(self):
        return self.store.job

    def runner_for(self, drive: str) -> "JobRunner | None":
        return self.runners.get(drive.upper())

    def start(self) -> None:
        for runner in self.runners.values():
            runner.start()

    def poll(self) -> None:
        for runner in self.runners.values():
            runner.poll()

    def tick(self) -> None:
        for runner in self.runners.values():
            runner.tick()

    def drain(self) -> list[Event]:
        """Every drive's events, already stamped with which drive they are from."""
        collected: list[Event] = []
        for runner in self.runners.values():
            collected.extend(runner.drain())
        return collected

    # -- aggregate state --------------------------------------------------

    @property
    def is_busy(self) -> bool:
        """True while *any* drive is copying - the question every caller that
        asks is really asking ("is it safe to close / clean up now?")."""
        return any(runner.is_busy for runner in self.runners.values())

    @property
    def is_paused(self) -> bool:
        return any(runner.is_paused for runner in self.runners.values())

    def busy_drives(self) -> list[str]:
        return [drive for drive, r in self.runners.items() if r.is_busy]

    def working_entry_ids(self) -> set[str]:
        """Folders being written right now, which the list must not let the
        user rename or delete out from under a copy."""
        return {
            r.current_entry_id
            for r in self.runners.values()
            if r.is_busy and r.current_entry_id
        }

    def claimed_by(self) -> dict[str, str]:
        """entry_id -> drive, so the folder list can show whose is whose."""
        return self.claims.as_dict()

    def cancel_all(self) -> None:
        for runner in self.runners.values():
            if runner.is_busy:
                runner.cancel()

    # -- the advisory bit -------------------------------------------------

    def expected(self) -> dict[str, "DiscEntry | None"]:
        """What each idle drive should be fed next.

        Advisory only, and deliberately so. Nothing is reserved by asking, and
        a disc put in the "wrong" drive still lands in the right folder -
        identification happens after insertion either way. This exists so the
        operator can keep two drives straight, not to make a rule they can
        break.

        Drives already holding a folder report that one; the rest are dealt
        the queue in drive order.
        """
        answer: dict[str, DiscEntry | None] = {}
        spoken_for = set()
        for drive in self.drives:
            runner = self.runners[drive]
            entry = runner._current_entry()
            answer[drive] = entry
            if entry is not None:
                spoken_for.add(entry.entry_id)

        free = [drive for drive in self.drives if answer[drive] is None]
        if not free:
            return answer
        queue_ahead = [
            entry
            for entry in self.job.upcoming_pending(len(free) + len(spoken_for))
            if entry.entry_id not in spoken_for
        ]
        for drive, entry in zip(free, queue_ahead):
            answer[drive] = entry
        return answer


def _scanner_drives(scanner) -> list[str]:
    """Ask the scanner which drives exist, tolerating one that cannot say.

    A scanner predating the pool has only ``scan()``, which reports drives
    with a disc already in them - enough to keep working, and the pool picks
    up the rest as soon as they are used.
    """
    asker = getattr(scanner, "drives", None)
    if callable(asker):
        return list(asker())
    return sorted({media.drive for media in scanner.scan()})
