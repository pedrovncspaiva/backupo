"""Reading folder names off a photographed delivery protocol, via Gemini.

The documents are scanned "PROTOCOLO DE ENTREGA DE MATERIAL DE MIDIA" sheets:
a box number, then one or more EG groups, each listing the discs it contains,
one per line, in the order they are stacked.

Three things about those sheets drive the design here:

* **Transcription is not a folder name.** Real lines contain characters Windows
  forbids, e.g. ``AGI-10-0451 R1 a AGI-10-0466 R1 (1/3)``. The raw text is kept
  verbatim and a separate, sanitised name is proposed alongside it.
* **Duplicates are real.** The same code legitimately appears twice on a sheet
  (two discs, same revision label). Folder names must be unique, so repeats are
  suffixed and flagged rather than silently dropped - dropping one would lose a
  disc.
* **Scans bleed through.** The back of the sheet shows faintly and mirrored on
  the front. The prompt calls this out explicitly, because transcribing ghost
  text would invent discs that do not exist.

Nothing here is trusted blindly: every item comes back with the raw text, any
issues found, and a needs-review flag, and the user confirms the list before a
single folder is created. The API key is read from the environment or a local
config file and is never written to a job file or a log.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .core import INVALID_CHARS, RESERVED_NAMES
from .jobmodel import EntryDraft

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "models/gemini-3.8-flash"
DEFAULT_TIMEOUT = 180.0
CONFIG_FILE_NAME = "config.json"

# Gemini accepts these inline. PDFs are included because a scanner often
# produces one multi-page PDF rather than a folder of images.
MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".pdf": "application/pdf",
}

# Inline request payloads are capped well below the API limit, since base64
# inflates by a third and several pages add up quickly.
MAX_INLINE_BYTES = 15 * 1024 * 1024

# Characters Windows forbids, mapped to the closest readable stand-in rather
# than deleted, so "(1/3)" stays legible as "(1-3)".
CHAR_REPLACEMENTS = {
    "/": "-",
    "\\": "-",
    ":": "-",
    "*": "+",
    "?": "",
    '"': "'",
    "<": "(",
    ">": ")",
    "|": "-",
}

PROMPT = """Voce esta lendo a FOTO de um documento em papel chamado
"PROTOCOLO DE ENTREGA DE MATERIAL DE MIDIA".

O documento lista CDs a serem copiados. Ele tem:
- um numero de caixa, por exemplo "CAIXA 02 (EG 1841 ao EG 1852)";
- um ou mais grupos, cada um comecando com uma linha como
  "- EG 1841 ( BENGUELA - ABG )";
- sob cada grupo, uma lista de codigos de CD, UM POR LINHA.

Transcreva TODOS os codigos de CD, na ordem exata em que aparecem, de cima para
baixo, pagina por pagina.

REGRAS IMPORTANTES:
1. Transcreva EXATAMENTE como esta escrito. Nao corrija, nao normalize, nao
   complete e nao reordene os codigos. Mantenha espacos, hifens, barras e
   parenteses como estao. Exemplos de codigos validos:
   "ABG-DI-8963-GI-19 R0", "AGI-10-0451 R1 a AGI-10-0466 R1 (1/3)",
   "ALU-3-DI-1852-DE-40 a 118", "PAC-DI-1885-GM-01".
2. Se a MESMA linha aparecer duas vezes, transcreva as DUAS vezes. Repeticoes
   sao reais: sao dois CDs diferentes. Nunca remova duplicatas.
3. IGNORE texto fantasma: em folhas finas o verso aparece de forma clara,
   espelhada ou de cabeca para baixo. Ignore completamente qualquer texto
   espelhado, invertido ou muito apagado. Transcreva apenas o texto nitido e
   legivel da frente da folha.
4. NAO inclua cabecalhos, datas, "A/C:", "Dept", o titulo, nem a linha
   "Encaminhamos os CDs...". Apenas os codigos dos CDs.
5. A linha do grupo ("- EG 1841 ( BENGUELA - ABG )") NAO e um codigo de CD.
   Separe-a em dois campos, SEM repetir nada:
     "label"    = apenas "EG 1841"        (sem o traco inicial, sem parenteses)
     "location" = apenas "BENGUELA - ABG" (sem parenteses)
   Cada EG deve aparecer UMA unica vez na lista de grupos, com todos os seus
   CDs juntos, mesmo que a lista continue na pagina seguinte. Escreva o
   "location" do mesmo jeito todas as vezes.
6. Se um codigo estiver ilegivel ou voce estiver em duvida, inclua-o mesmo
   assim, com a sua melhor leitura, e marque "uncertain": true nesse item.

Responda SOMENTE com JSON, no formato do schema."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "document_title": {"type": "string"},
        "box": {"type": "string"},
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "location": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "uncertain": {"type": "boolean"},
                            },
                            "required": ["text"],
                        },
                    },
                },
                "required": ["label", "items"],
            },
        },
    },
    "required": ["groups"],
}


# A heading may arrive split ("EG 1841" + "BENGUELA - ABG") or whole
# ("- EG 1841 ( BENGUELA - ABG )"). Both are handled; the model is not trusted
# to have split it correctly.
_LEADING_MARKS = "-–— 	"
_EG_RE = re.compile(r"\bEG\s*(\d+)\b", re.IGNORECASE)
_PARENS_RE = re.compile(r"\((.*?)\)")


def split_group_heading(label: str, location: str = "") -> tuple[str, str]:
    """Return (key, location) for a group, however the model phrased it.

    The key identifies the EG regardless of how the location was written, so
    every disc of one EG lands in one folder even if the model describes that
    EG inconsistently across pages.
    """
    text = " ".join((label or "").split()).strip()
    text = text.lstrip(_LEADING_MARKS).strip()

    # A location embedded in the label is used only if none was given
    # separately - and is always removed, so it is never appended twice.
    embedded = ""
    match = _PARENS_RE.search(text)
    if match:
        embedded = match.group(1).strip()
        text = (text[: match.start()] + text[match.end() :]).strip()

    found = _EG_RE.search(text)
    key = f"EG {found.group(1)}" if found else text.strip(_LEADING_MARKS).strip()

    place = " ".join((location or "").split()).strip().strip(_LEADING_MARKS).strip()
    return key, (place or embedded)


def group_folder_for(key: str, location: str) -> str:
    """The folder name for one group: "EG 1841 (BENGUELA - ABG)"."""
    if not key:
        return ""
    raw = f"{key} ({location})" if location else key
    name, _ = sanitize_name(raw)
    return name


class VisionError(RuntimeError):
    """Anything that stopped an extraction, phrased for the user."""


@dataclass
class VisionConfig:
    api_key: str = ""
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT
    api_base: str = API_BASE

    @property
    def endpoint(self) -> str:
        model = self.model if self.model.startswith("models/") else f"models/{self.model}"
        return f"{self.api_base}/{model}:generateContent"


@dataclass
class ExtractedItem:
    """One transcribed line, plus the folder name we propose for it."""

    raw_text: str
    folder_name: str
    group_label: str = ""  # canonical key, e.g. "EG 1841"
    group_location: str = ""
    group_folder: str = ""  # resolved subfolder, shared by every disc of this EG
    uncertain: bool = False
    issues: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return bool(self.issues) or self.uncertain

    @property
    def was_changed(self) -> bool:
        return self.folder_name != self.raw_text

    def group_folder_name(self) -> str:
        """The subfolder this disc belongs in - resolved once per EG."""
        return self.group_folder


@dataclass
class ExtractionResult:
    items: list[ExtractedItem] = field(default_factory=list)
    document_title: str = ""
    box: str = ""
    warnings: list[str] = field(default_factory=list)
    model: str = ""

    @property
    def review_count(self) -> int:
        return sum(1 for item in self.items if item.needs_review)

    def suggested_parent_name(self) -> str:
        """The parent folder name the sheet itself implies.

        The box line ("CAIXA 02 (EG 1841 ao EG 1852)") is exactly what the batch
        folder should be called, so there is no reason to make the user retype
        it. Falls back to the EG range when a sheet has no box line.
        """
        if self.box.strip():
            name, _ = sanitize_name(self.box)
            return name

        labels = self.group_labels()
        if not labels:
            return ""
        joined = labels[0] if len(labels) == 1 else f"{labels[0]} ao {labels[-1]}"
        name, _ = sanitize_name(joined)
        return name

    def group_labels(self) -> list[str]:
        seen: list[str] = []
        for item in self.items:
            if item.group_label and item.group_label not in seen:
                seen.append(item.group_label)
        return seen


# --------------------------------------------------------------------------
# API key
# --------------------------------------------------------------------------


def config_path(local_root: Path | None = None) -> Path:
    from .jobstore import default_local_root

    return (local_root or default_local_root()) / CONFIG_FILE_NAME


def load_api_key(local_root: Path | None = None) -> str:
    """Environment first, then the local config file.

    Deliberately never the job file: that gets copied into the backup folder and
    handed to other people.
    """
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    try:
        data = json.loads(config_path(local_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("gemini_api_key", "") or "").strip()


def save_api_key(key: str, local_root: Path | None = None) -> None:
    path = config_path(local_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data["gemini_api_key"] = key.strip()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_model(local_root: Path | None = None) -> str:
    try:
        data = json.loads(config_path(local_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_MODEL
    return str(data.get("gemini_model") or DEFAULT_MODEL)


# --------------------------------------------------------------------------
# Name sanitising
# --------------------------------------------------------------------------


def sanitize_name(raw: str) -> tuple[str, list[str]]:
    """Turn a transcribed line into a legal Windows folder name.

    Returns the name and a list of what had to change, so the review dialog can
    show the user exactly why a name is not a verbatim copy of the paper.
    """
    issues: list[str] = []
    name = " ".join(raw.split())  # collapse runs of whitespace, strip ends
    if name != raw.strip():
        issues.append("espacos ajustados")

    replaced = []
    for bad, good in CHAR_REPLACEMENTS.items():
        if bad in name:
            name = name.replace(bad, good)
            replaced.append(bad)
    if replaced:
        issues.append(f"caractere invalido no Windows: {' '.join(replaced)}")

    # Anything else illegal (control characters) goes.
    if INVALID_CHARS.search(name):
        name = INVALID_CHARS.sub("-", name)
        issues.append("caractere de controle removido")

    name = name.strip().rstrip(".")
    if not name:
        return "SEM NOME", issues + ["linha vazia"]

    if name.split(".", 1)[0].upper() in RESERVED_NAMES:
        name = name + "_"
        issues.append("nome reservado do Windows")

    if len(name) > 120:
        name = name[:120].strip()
        issues.append("nome encurtado")

    return name, issues


def resolve_duplicates(items: Sequence[ExtractedItem]) -> list[ExtractedItem]:
    """Suffix repeated names so every folder is distinct.

    A repeated code on these sheets means two physical discs, so the second one
    must get its own folder. Dropping it would silently lose a disc; both are
    flagged so the user can name them properly.
    """
    seen: dict[tuple[str, str], int] = {}
    for item in items:
        # Two discs with the same code under different EGs land in different
        # folders and do not collide; only a repeat within one group does.
        key = (item.group_folder.casefold(), item.folder_name.casefold())
        count = seen.get(key, 0) + 1
        seen[key] = count
        if count > 1:
            item.folder_name = f"{item.folder_name} ({count})"
            item.issues = item.issues + [f"nome repetido na folha ({count}o)"]
    return list(items)


def to_drafts(
    items: Iterable[ExtractedItem],
    model: str = "",
    use_groups: bool = True,
) -> list[EntryDraft]:
    """Convert to the drafts the job store already accepts.

    With ``use_groups`` each disc keeps its EG as a real subfolder, reproducing
    the sheet's CAIXA > EG > disc layout. Turning it off flattens everything
    directly under the parent.
    """
    drafts = []
    for item in items:
        drafts.append(
            EntryDraft(
                folder_name=item.folder_name,
                group=item.group_folder_name() if use_groups else "",
                source="llm_photo",
                name_confidence=0.5 if item.uncertain else 0.95,
                needs_review=item.needs_review,
                llm={
                    "model": model,
                    "raw_text": item.raw_text,
                    "group": item.group_label,
                    "location": item.group_location,
                    "issues": list(item.issues),
                },
                notes="; ".join(item.issues),
            )
        )
    return drafts


# --------------------------------------------------------------------------
# Request / response
# --------------------------------------------------------------------------


def mime_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in MIME_TYPES:
        raise VisionError(
            f"Formato nao suportado: {path.name}. "
            f"Use {', '.join(sorted(set(MIME_TYPES)))}."
        )
    return MIME_TYPES[suffix]


def build_payload(image_paths: Sequence[Path], prompt: str = PROMPT) -> dict:
    """Assemble the generateContent body: the prompt, then every page in order."""
    if not image_paths:
        raise VisionError("Selecione ao menos uma imagem.")

    parts: list[dict] = [{"text": prompt}]
    total = 0
    for path in image_paths:
        path = Path(path)
        try:
            blob = path.read_bytes()
        except OSError as exc:
            raise VisionError(f"Nao foi possivel ler {path.name}: {exc}") from exc
        total += len(blob)
        if total > MAX_INLINE_BYTES:
            raise VisionError(
                "As imagens somam mais que o limite por requisicao "
                f"({MAX_INLINE_BYTES // (1024 * 1024)} MB). Envie menos paginas por vez."
            )
        parts.append(
            {
                "inline_data": {
                    "mime_type": mime_type_for(path),
                    "data": base64.b64encode(blob).decode("ascii"),
                }
            }
        )

    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            # Transcription, not composition: keep it as literal as possible.
            "temperature": 0.0,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }


def parse_response(payload: dict) -> ExtractionResult:
    """Pull the transcription out of a generateContent response."""
    result = ExtractionResult()

    feedback = payload.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise VisionError(f"A requisicao foi bloqueada: {feedback['blockReason']}")

    candidates = payload.get("candidates") or []
    if not candidates:
        raise VisionError("A resposta nao trouxe nenhum resultado.")

    candidate = candidates[0]
    finish = candidate.get("finishReason")
    if finish and finish not in ("STOP", "MAX_TOKENS"):
        raise VisionError(f"A leitura terminou de forma inesperada: {finish}")

    text = "".join(
        part.get("text", "")
        for part in (candidate.get("content") or {}).get("parts") or []
    ).strip()
    if not text:
        raise VisionError("A resposta veio vazia.")

    try:
        data = json.loads(text)
    except ValueError as exc:
        raise VisionError(f"A resposta nao era JSON valido: {exc}") from exc

    result.document_title = str(data.get("document_title") or "")
    result.box = str(data.get("box") or "")

    if finish == "MAX_TOKENS":
        result.warnings.append(
            "A leitura pode ter sido cortada por tamanho - confira o fim da lista."
        )

    for group in data.get("groups") or []:
        label, location = split_group_heading(
            str(group.get("label") or ""), str(group.get("location") or "")
        )
        for raw_item in group.get("items") or []:
            if isinstance(raw_item, str):
                text_value, uncertain = raw_item, False
            else:
                text_value = str(raw_item.get("text") or "")
                uncertain = bool(raw_item.get("uncertain"))
            if not text_value.strip():
                continue
            folder_name, issues = sanitize_name(text_value)
            result.items.append(
                ExtractedItem(
                    raw_text=text_value.strip(),
                    folder_name=folder_name,
                    group_label=label,
                    group_location=location,
                    uncertain=uncertain,
                    issues=issues,
                )
            )

    if not result.items:
        raise VisionError("Nenhum codigo de CD foi encontrado nas imagens.")

    _assign_group_folders(result.items)
    resolve_duplicates(result.items)
    return result


def _assign_group_folders(items: Sequence[ExtractedItem]) -> None:
    """Give every disc of one EG the same subfolder.

    A sheet spanning several pages can come back with the same EG described
    more than once and not always identically ("BENGUELA - ABG" on one page,
    "BENGUELA" on another). Picking one location per EG - the most detailed -
    keeps that EG to a single folder instead of splitting its discs across two.
    """
    locations: dict[str, str] = {}
    for item in items:
        best = locations.get(item.group_label, "")
        if len(item.group_location) > len(best):
            locations[item.group_label] = item.group_location

    for item in items:
        item.group_location = locations.get(item.group_label, "")
        item.group_folder = group_folder_for(item.group_label, item.group_location)


def _post(url: str, body: dict, api_key: str, timeout: float) -> dict:
    """POST the request. The key travels in a header, never in the URL."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            problem = json.loads(exc.read().decode("utf-8"))
            detail = problem.get("error", {}).get("message", "")
        except Exception:
            pass
        if exc.code in (401, 403):
            raise VisionError(f"Chave de API recusada ({exc.code}). {detail}") from exc
        if exc.code == 404:
            raise VisionError(
                f"Modelo nao encontrado ({exc.code}). Verifique o nome do modelo "
                f"nas Opcoes. {detail}"
            ) from exc
        if exc.code == 429:
            raise VisionError(f"Limite de uso atingido (429). {detail}") from exc
        raise VisionError(f"Erro HTTP {exc.code}. {detail}") from exc
    except urllib.error.URLError as exc:
        raise VisionError(f"Falha de rede: {exc.reason}") from exc
    except TimeoutError as exc:
        raise VisionError("A requisicao demorou demais e foi cancelada.") from exc


def extract_names(
    image_paths: Sequence[Path],
    config: VisionConfig,
    post: Callable[[str, dict, str, float], dict] = _post,
) -> ExtractionResult:
    """Read folder names from photographed protocol sheets.

    ``post`` is injected so the whole pipeline - payload, parsing, sanitising,
    duplicate handling - is testable without a network call or an API key.
    """
    if not config.api_key:
        raise VisionError(
            "Nenhuma chave de API configurada. Defina GEMINI_API_KEY ou informe "
            "a chave nas Opcoes."
        )
    body = build_payload([Path(path) for path in image_paths])
    payload = post(config.endpoint, body, config.api_key, config.timeout)
    result = parse_response(payload)
    result.model = config.model
    return result
