"""API coerente sotto lock fisico comune per letture e transazioni."""
import hashlib
import json
import re
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from filelock import WindowsFileLock, Timeout

from config import Settings
from .errors import StoreError
from .git_ops import dirty_files, find_operation, head, run_git
from .transaction import transact
from .validation import (
    check_name, check_physical, decode, inspect_snapshot, load_snapshot,
    validate_added_lines, validate_snapshot,
)

HASH = re.compile(r"[a-f0-9]{64}\Z")
AGENTS = {"codex", "claude", "maintenance"}


class MemoryStore:
    """Store locale con CAS, lock condiviso e journal persistente."""

    def __init__(self, settings: Settings) -> None:
        if sys.platform != "win32":
            raise StoreError("Runtime non supportato: da WSL richiamare .venv/Scripts/python.exe Windows")
        self.settings = settings
        self.root = settings.root
        check_physical(self.root)
        self.root = self.root.resolve(strict=True)
        check_physical(self.root)
        check_physical(self.root / ".git")
        if not self.root.is_dir() or not (self.root / ".git").is_dir():
            raise StoreError("Richiesto repository con directory .git reale")
        self.state = self.root.parent / (self.root.name + ".shared-state")
        check_physical(self.state)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        check_physical(self.state)
        self.state.mkdir(exist_ok=True)
        check_physical(self.state / "store.windows.lock")
        try:
            with WindowsFileLock(
                self.state / "store.windows.lock", timeout=self.settings.lock_timeout,
                preserve_lock_file=True, fallback_to_soft=False,
            ):
                if (self.state / "journal.json").exists():
                    raise StoreError(f"Recupero pendente: {self.state / 'journal.json'}", 3)
                if Path(run_git(self.root, "rev-parse", "--show-toplevel")).resolve() != self.root.resolve():
                    raise StoreError("Root Git diversa dalla root memoria")
                yield
        except Timeout as exc:
            raise StoreError(f"Timeout lock condiviso: {self.state / 'store.windows.lock'}", 4) from exc

    def read(self, names: list[str]) -> dict:
        """Ritorna testo e hash dei byte esatti sotto un unico lock."""
        for name in names:
            check_name(name)
        if len(names) != len(set(names)):
            raise StoreError("File richiesto due volte")
        with self._locked():
            snapshot = load_snapshot(self.root)
            if any(name not in snapshot for name in names):
                raise StoreError("File richiesto non presente")
            return {"head": head(self.root), "files": {
                name: {"sha256": hashlib.sha256(snapshot[name]).hexdigest(),
                       "content": decode(snapshot[name])}
                for name in names
            }}

    def search(self, query: str) -> dict:
        """Ricerca case-insensitive in schede e alias, con output limitato."""
        if not query.strip():
            raise StoreError("Query vuota")
        with self._locked():
            snapshot = load_snapshot(self.root)
            matches = []
            for name, data in sorted(snapshot.items()):
                if name == "MEMORY.md":
                    continue
                content = decode(data)
                position = content.casefold().find(query.casefold())
                if position < 0 and query.casefold() not in name.casefold():
                    continue
                start = max(0, position)
                matches.append({"file": name, "excerpt": content[start:start + self.settings.excerpt_chars]})
                if len(matches) >= self.settings.search_limit:
                    break
            return {"head": head(self.root), "matches": matches}

    def doctor(self) -> dict:
        """Controlla indice, frontmatter e pulizia Git senza modificare memorie."""
        with self._locked():
            snapshot = load_snapshot(self.root)
            try:
                report = inspect_snapshot(snapshot, self.settings)
            except StoreError as exc:
                return {"ok": False, "files": len(snapshot), "index_links": 0,
                        "missing_targets": [], "unindexed_files": [],
                        "dirty_files": dirty_files(self.root), "head": head(self.root), "error": str(exc)}
            dirty = dirty_files(self.root)
            return {"ok": not (report["missing_targets"] or report["unindexed_files"] or dirty),
                    **report, "dirty_files": dirty, "head": head(self.root)}

    def apply(self, payload: dict, agent: str) -> dict:
        """Valida il candidato completo e lo committa con CAS e idempotenza."""
        _validate_request(payload, agent)
        digest = hashlib.sha256(json.dumps(
            {"agent": agent, "payload": payload}, sort_keys=True, ensure_ascii=True,
            separators=(",", ":"),
        ).encode()).hexdigest()
        with self._locked():
            if dirty_files(self.root):
                raise StoreError("Repository non pulito: preservate modifiche preesistenti", 3)
            replay = find_operation(self.root, payload["operation_id"])
            if replay:
                if replay.get("digest") != digest:
                    raise StoreError("UUID gia' utilizzato con payload diverso", 3)
                return {"ok": True, "head": replay["head"], "operation_id": payload["operation_id"],
                        "changed": replay["changed"], "replayed": True}
            if head(self.root) != payload["base_head"]:
                raise StoreError("Conflitto base_head", 3)
            snapshot = load_snapshot(self.root)
            candidate = dict(snapshot)
            changes = {}
            for change in payload["changes"]:
                name, content = change["file"], change["content"]
                actual = hashlib.sha256(snapshot[name]).hexdigest() if name in snapshot else "missing"
                if actual != change["expected_sha256"]:
                    raise StoreError("Conflitto hash file", 3)
                if content is None:
                    if name not in snapshot or name == "MEMORY.md":
                        raise StoreError("Cancellazione non valida")
                    del candidate[name]
                    changes[name] = None
                else:
                    data = content.encode("utf-8")
                    validate_added_lines(snapshot.get(name, b""), data)
                    candidate[name] = data
                    if snapshot.get(name) != data:
                        changes[name] = data
            validate_snapshot(candidate, self.settings, snapshot)
            if not changes:
                raise StoreError("Batch senza modifiche effettive")
            return transact(self.root, self.state, changes, snapshot, payload, digest, agent)


def _validate_request(payload: dict, agent: str) -> None:
    if agent not in AGENTS or not isinstance(payload, dict):
        raise StoreError("Agente o richiesta non validi")
    if set(payload) != {"operation_id", "base_head", "reason", "changes"}:
        raise StoreError("Campi richiesta non validi")
    try:
        operation_id = uuid.UUID(payload["operation_id"])
        if str(operation_id) != payload["operation_id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise StoreError("operation_id deve essere un UUID canonico") from exc
    if not isinstance(payload["base_head"], str) or not re.fullmatch(r"[a-f0-9]{40,64}", payload["base_head"]):
        raise StoreError("base_head non valido")
    if not isinstance(payload["reason"], str) or not payload["reason"].strip():
        raise StoreError("Motivazione richiesta")
    if not isinstance(payload["changes"], list) or not payload["changes"]:
        raise StoreError("Batch vuoto o non valido")
    names = set()
    for change in payload["changes"]:
        if not isinstance(change, dict) or set(change) != {"file", "expected_sha256", "content"}:
            raise StoreError("Campi modifica non validi")
        check_name(change["file"])
        expected = change["expected_sha256"]
        if not isinstance(expected, str) or (expected != "missing" and not HASH.fullmatch(expected)):
            raise StoreError("expected_sha256 non valido")
        if change["file"] in names:
            raise StoreError("File duplicato nel batch")
        names.add(change["file"])
        if change["content"] is not None and not isinstance(change["content"], str):
            raise StoreError("content deve essere testo oppure null")

