"""Read folder names from protocol photos, from the command line.

    python -m backupov2.tools.extract foto1.jpg foto2.jpg
    python -m backupov2.tools.extract --model models/gemini-3.8-flash scan.pdf
    python -m backupov2.tools.extract --names-only *.jpg > nomes.txt
    python -m backupov2.tools.extract --sem-grupos scan.pdf   # sem subpasta por EG

Useful for checking a model or a batch of scans without opening the GUI, and
for producing a plain list you can paste into "Adicionar pastas".

The API key comes from GEMINI_API_KEY, or from the config file the GUI writes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..vision import (
    VisionConfig,
    VisionError,
    extract_names,
    load_api_key,
    load_model,
)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or "--help" in argv or "-h" in argv:
        print(__doc__.strip())
        return 0 if argv else 2

    model = load_model()
    if "--model" in argv:
        index = argv.index("--model")
        model = argv[index + 1]
        del argv[index : index + 2]

    names_only = "--names-only" in argv
    if names_only:
        argv.remove("--names-only")
    flat = "--sem-grupos" in argv
    if flat:
        argv.remove("--sem-grupos")

    paths = [Path(arg) for arg in argv]
    missing = [path for path in paths if not path.is_file()]
    if missing:
        print("Arquivo(s) nao encontrado(s): " + ", ".join(str(p) for p in missing))
        return 2

    key = load_api_key()
    if not key:
        print(
            "Nenhuma chave de API. Defina GEMINI_API_KEY ou configure a chave "
            "pela interface (Importar de fotos... -> Chave de API...)."
        )
        return 2

    try:
        result = extract_names(paths, VisionConfig(api_key=key, model=model))
    except VisionError as exc:
        print(f"Falhou: {exc}")
        return 1

    if names_only:
        for item in result.items:
            group = "" if flat else item.group_folder_name()
            print(f"{group}/{item.folder_name}" if group else item.folder_name)
        return 0

    if result.box:
        print(result.box)
    print(f"modelo: {result.model}   paginas: {len(paths)}   CDs: {len(result.items)}")
    for warning in result.warnings:
        print(f"AVISO: {warning}")
    print()

    header = f"{'#':>3}  {'nome da pasta':<40} {'subpasta (EG)':<26} situacao"
    print(header)
    print("-" * len(header))
    for index, item in enumerate(result.items, start=1):
        note = "; ".join(item.issues) or ("leitura incerta" if item.uncertain else "")
        mark = "!" if item.needs_review else " "
        group = "" if flat else item.group_folder_name()
        print(f"{mark}{index:>2}  {item.folder_name[:40]:<40} {group[:26]:<26} {note}")
        if item.was_changed:
            print(f"{'':5}no papel: {item.raw_text}")

    print()
    print(f"{result.review_count} linha(s) para conferir antes de usar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
