"""Hardware smoke test for the Win32 layer.

    python -m backupov2.tools.drivecheck            # report only
    python -m backupov2.tools.drivecheck --eject    # also open the tray

Exists because the eject caveats are drive-specific: the target machine has an
external USB unit, and those can report a successful IOCTL while the tray never
moves. Better to find that out here than 40 minutes into a real batch.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .. import winapi
from ..core import format_bytes, scan_tree
from ..media import Win32Scanner, detect_disc_kind

DRIVE_TYPE_NAMES = {
    winapi.DRIVE_UNKNOWN: "desconhecido",
    winapi.DRIVE_NO_ROOT_DIR: "sem raiz",
    winapi.DRIVE_REMOVABLE: "removivel",
    winapi.DRIVE_FIXED: "fixo",
    winapi.DRIVE_REMOTE: "rede",
    winapi.DRIVE_CDROM: "optico",
    winapi.DRIVE_RAMDISK: "ramdisk",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    do_eject = "--eject" in argv
    do_size = "--size" in argv

    if not winapi.is_windows():
        print("Este verificador so funciona no Windows.")
        return 2

    winapi.silence_missing_media_dialogs()
    print("SetErrorMode aplicado (sem dialogos de 'disco ausente').\n")

    print("Unidades logicas:")
    for root in winapi.logical_drive_roots():
        kind = DRIVE_TYPE_NAMES.get(winapi.drive_type(root), "?")
        print(f"  {root:<5} {kind}")

    optical = winapi.optical_drive_roots()
    print(f"\nUnidades opticas: {optical or 'nenhuma'}")
    if not optical:
        return 1

    for root in optical:
        present = winapi.media_present(root)
        print(f"\n--- {root} ---")
        print(f"  midia presente: {'sim' if present else 'nao'}")
        info = winapi.volume_information(root)
        if info:
            serial = f"0x{info.serial:08X}" if info.serial else "(nenhum)"
            print(f"  rotulo        : {info.label or '(sem rotulo)'}")
            print(f"  numero de serie: {serial}")
            print(f"  sistema de arq.: {info.fs_name}")
        elif present:
            print("  volume ainda nao montado")

        if present:
            probe = detect_disc_kind(Path(root), info.fs_name if info else "")
            confident = "confirmado" if probe.confident else "incerto"
            print(f"  tipo de disco : {probe.kind.value} ({confident}: {probe.reason})")
            if do_size:
                scan = scan_tree(Path(root))
                print(
                    f"  conteudo      : {scan.file_count} arquivo(s), "
                    f"{format_bytes(scan.total_bytes)}"
                )

    print("\nScanner (o que o runner enxerga):")
    seen = Win32Scanner().scan()
    for media in seen:
        print(f"  {media.display_name}  serial={media.serial}  fs={media.fs_name}")
    if not seen:
        print("  (nenhum disco carregado)")

    if do_eject:
        target = optical[0]
        print(f"\nEjetando {target} ...")
        result = winapi.eject(target)
        for name, ok, detail in result.steps:
            mark = "ok " if ok else "FALHOU"
            print(f"  {name:<14} {mark} {detail}")
        print(f"  resultado: {'ejetado' if result.ok else result.error}")
        print("  (a bandeja pode levar alguns segundos; unidades USB variam)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
