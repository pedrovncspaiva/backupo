"""Print a job file as a readable table.

    python -m backupov2.tools.jobdump <path to _backupov2-job.json>

Small on purpose - it exists so a job's real state can be checked from a
terminal while the UI is still being built, and afterwards whenever a batch
looks wrong on disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..core import format_bytes
from ..jobmodel import EntryStatus
from ..jobstore import JobStore

MARKS = {
    EntryStatus.PENDING: " ",
    EntryStatus.IN_PROGRESS: ">",
    EntryStatus.DONE: "+",
    EntryStatus.SKIPPED: "-",
    EntryStatus.FAILED: "!",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__.strip())
        return 2

    outcome = JobStore.load(Path(argv[0]))
    job = outcome.job

    print(f"job      {job.job_id}  (schema {job.schema_version}, app {job.app_version})")
    print(f"destino  {job.parent_path}")
    print(f"criado   {job.created_utc}    atualizado {job.updated_utc}")
    if outcome.used_backup:
        print("AVISO: carregado a partir da copia de seguranca (.bak)")
    for note in outcome.notes:
        print(f"nota: {note}")

    counts = job.count_by_status()
    summary = "  ".join(
        f"{status.value}={counts[status]}" for status in EntryStatus if counts[status]
    )
    print(f"entradas {len(job)}   {summary}")
    print()

    header = f"{'':2}{'#':>3}  {'pasta':<34} {'situacao':<12} {'tipo':<7} {'arquivos':>9} {'tamanho':>10}"
    print(header)
    print("-" * len(header))
    for index, entry in enumerate(job.entries, start=1):
        result = entry.result
        files = str(result.files_copied) if result else ""
        size = format_bytes(result.bytes_copied) if result else ""
        warn = f" !{entry.failed_file_count}" if entry.failed_file_count else ""
        print(
            f"{MARKS.get(entry.status, '?'):2}{index:>3}  "
            f"{entry.folder_name[:34]:<34} "
            f"{entry.status.value:<12} {entry.disc_kind.value:<7} "
            f"{files:>9} {size:>10}{warn}"
        )
        if entry.error:
            print(f"{'':7}erro: {entry.error}")

    pending = job.next_pending()
    print()
    print(f"proximo disco -> {pending.folder_name if pending else '(nenhum pendente)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
