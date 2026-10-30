"""Validazione nomi, snapshot Markdown e righe aggiunte."""
import difflib
import re
import stat
from collections import Counter
from pathlib import Path

import yaml

from config import Settings
from .errors import StoreError

NAME = re.compile(r"(?:MEMORY|(?:user|project|feedback|person|reference)_[a-z0-9_-]+)\.md\Z")
INLINE_LINK = re.compile(r"\[[^\]\n]*\]\(([^)\n]+)\)")
REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
FORBIDDEN_DASHES = frozenset(chr(code) for code in (0x2012, 0x2013, 0x2014, 0x2015, 0x2212))
TYPES = {"user", "feedback", "project", "reference"}


def check_name(name: str) -> None:
    """Ammette esclusivamente i nomi canonici senza sottocartelle."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise StoreError("Nome file memoria non consentito")


def check_physical(path: Path) -> None:
    """Rifiuta symlink, junction e reparse point anche negli antenati."""
    for item in (path, *path.parents):
        if not item.exists() and not item.is_symlink():
            continue
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise StoreError("Percorso simbolico o reparse non consentito")


def load_snapshot(root: Path) -> dict[str, bytes]:
    """Legge i byte dei Markdown diretti, senza normalizzare le schede."""
    result = {}
    seen = set()
    for path in root.iterdir():
        if path.suffix.lower() != ".md":
            continue
        check_name(path.name)
        check_physical(path)
        if not path.is_file() or path.name.casefold() in seen:
            raise StoreError("File memoria non regolare o collisione di nomi")
        seen.add(path.name.casefold())
        result[path.name] = path.read_bytes()
    return result


def decode(content: bytes) -> str:
    """Decodifica UTF-8 strettamente."""
    try:
        return content.decode("utf-8")
    except UnicodeError as exc:
        raise StoreError("File memoria non UTF-8") from exc


def index_targets(content: str) -> list[str]:
    """Estrae e valida target Markdown inline e reference-style."""
    targets = []
    for raw in INLINE_LINK.findall(content) + REFERENCE_LINK.findall(content):
        target = raw.strip().split()[0].strip("<>")
        if ".md" not in target.lower():
            continue
        check_name(target)
        if target == "MEMORY.md":
            raise StoreError("Indice autoreferenziale non consentito")
        targets.append(target)
    return targets


def inspect_snapshot(snapshot: dict[str, bytes], settings: Settings) -> dict:
    """Ritorna diagnostica strutturale senza esporre i fatti delle schede."""
    if "MEMORY.md" not in snapshot:
        raise StoreError("MEMORY.md mancante")
    index = decode(snapshot["MEMORY.md"])
    if len(index.splitlines()) > settings.index_max_lines or len(snapshot["MEMORY.md"]) > settings.index_max_bytes:
        raise StoreError("Indice oltre il limite di righe o byte")
    targets = index_targets(index)
    subjects = set(snapshot) - {"MEMORY.md"}
    counts = Counter(targets)
    duplicates = sorted(name for name, count in counts.items() if count != 1)
    if duplicates:
        raise StoreError("Link duplicati nell'indice")
    for name in subjects:
        text = decode(snapshot[name]).lstrip("\ufeff")
        parts = re.split(r"^---\s*$", text, maxsplit=2, flags=re.MULTILINE)
        if len(parts) != 3 or parts[0].strip():
            raise StoreError(f"{name}: frontmatter YAML mancante o non valido")
        try:
            metadata = yaml.safe_load(parts[1])
        except yaml.YAMLError as exc:
            raise StoreError(f"{name}: frontmatter YAML non valido") from exc
        if not isinstance(metadata, dict):
            raise StoreError(f"{name}: frontmatter deve essere una mappa")
        if any(not isinstance(metadata.get(key), str) or not metadata[key].strip() for key in ("name", "description")):
            raise StoreError(f"{name}: frontmatter richiede name e description")
        detail = metadata.get("metadata")
        kind = detail.get("type") if isinstance(detail, dict) else None
        legacy_person = kind == "person" and name.startswith("person_")
        if not isinstance(kind, str) or (kind not in TYPES and not legacy_person):
            raise StoreError(f"{name}: frontmatter metadata.type non valido")
    missing = sorted(set(targets) - subjects)
    unindexed = sorted(subjects - set(targets))
    return {"files": len(snapshot), "index_links": len(targets), "missing_targets": missing, "unindexed_files": unindexed}


def validate_snapshot(snapshot: dict[str, bytes], settings: Settings,
                      previous: dict[str, bytes] | None = None) -> None:
    """Rifiuta un candidato con collegamenti mancanti o schede non indicizzate."""
    report = inspect_snapshot(snapshot, settings)
    for name, data in snapshot.items():
        if name == "MEMORY.md":
            continue
        metadata = yaml.safe_load(re.split(r"^---\s*$", decode(data).lstrip("\ufeff"), maxsplit=2, flags=re.MULTILINE)[1])
        if metadata["metadata"]["type"] == "person":
            old = (previous or {}).get(name)
            if old is None or yaml.safe_load(re.split(r"^---\s*$", decode(old).lstrip("\ufeff"), maxsplit=2, flags=re.MULTILINE)[1]).get("metadata", {}).get("type") != "person":
                raise StoreError(f"{name}: tipo person consentito solo nel legacy esistente")
    if report["missing_targets"] or report["unindexed_files"]:
        raise StoreError("Snapshot incoerente: aggiornare schede e indice nello stesso batch")


def validate_added_lines(before: bytes, after: bytes) -> None:
    """Vieta i trattini lunghi nelle righe nuove, preservando il testo legacy."""
    old_lines, new_lines = decode(before).splitlines(), decode(after).splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, _, _, start, end in matcher.get_opcodes():
        if tag not in {"insert", "replace"}:
            continue
        if any(character in FORBIDDEN_DASHES for line in new_lines[start:end] for character in line):
            raise StoreError("Trattino Unicode lungo in una riga nuova")

