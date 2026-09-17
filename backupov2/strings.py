"""User-facing text that more than one module needs.

Labels used in exactly one place stay next to the widget that shows them; this
module exists for the vocabulary the whole app shares, so a status never reads
"concluida" in one panel and "concluido" in another.
"""

from __future__ import annotations

from .jobmodel import DiscKind, EntryStatus
from .runner import RunnerState

APP_TITLE = "Assistente de Backup de Discos"

STATUS_LABELS = {
    EntryStatus.PENDING: "pendente",
    EntryStatus.IN_PROGRESS: "copiando",
    EntryStatus.DONE: "concluida",
    EntryStatus.SKIPPED: "pulada",
    EntryStatus.FAILED: "falhou",
}

KIND_LABELS = {
    DiscKind.UNKNOWN: "-",
    DiscKind.DATA: "dados",
    DiscKind.AUDIO: "audio",
}

STATE_LABELS = {
    RunnerState.IDLE: "Parado",
    RunnerState.READY_NO_DISC: "Aguardando disco",
    RunnerState.DISC_SETTLING: "Lendo disco...",
    RunnerState.IDENTIFYING: "Identificando disco...",
    RunnerState.DUPLICATE_WARNING: "Disco ja copiado",
    RunnerState.NO_TARGET: "Nenhuma pasta pendente",
    RunnerState.GRACE_COUNTDOWN: "Pronto para copiar",
    RunnerState.WORKING: "Copiando",
    RunnerState.EJECTING: "Ejetando",
    RunnerState.WAIT_DISC_REMOVED: "Remova o disco",
    RunnerState.PAUSED: "Pausado",
    RunnerState.ERROR_HOLD: "Erro",
    RunnerState.JOB_COMPLETE: "Backup concluido",
}


def status_label(status: EntryStatus) -> str:
    return STATUS_LABELS.get(status, status.value)


def kind_label(kind: DiscKind) -> str:
    return KIND_LABELS.get(kind, kind.value)


def state_label(state: RunnerState) -> str:
    return STATE_LABELS.get(state, state.value)
