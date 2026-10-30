"""Journal persistente, replace atomici e rollback conservativo."""
import base64
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .errors import StoreError
from .git_ops import dirty_files, find_operation, head, run_git

LOGGER = logging.getLogger(__name__)


def atomic_write(path: Path, content: bytes) -> None:
    """Scrive e sincronizza un temporaneo sullo stesso volume, poi os.replace."""
    descriptor, temporary = tempfile.mkstemp(prefix=".shared-memory-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reason_line(reason: str) -> str:
    """Motivo su una riga stampabile: nel commit non puo' imitare le righe della ricevuta."""
    return "".join(char for char in " ".join(reason.split()) if char.isprintable())


def _restore(root: Path, before: dict[str, bytes | None], base_head: str) -> None:
    for name, content in before.items():
        if name != "MEMORY.md" and content is not None:
            atomic_write(root / name, content)
    if "MEMORY.md" in before and before["MEMORY.md"] is not None:
        atomic_write(root / "MEMORY.md", before["MEMORY.md"])
    for name, content in before.items():
        if content is None:
            (root / name).unlink(missing_ok=True)
    run_git(root, "reset", "--quiet", base_head, "--", *before)
    if dirty_files(root) or any(
        ((root / name).read_bytes() != data) if data is not None else (root / name).exists()
        for name, data in before.items()
    ):
        raise StoreError("Rollback incompleto", 1)



def _commit_matches(root: Path, receipt: dict | None, digest: str,
                    changes: dict[str, bytes | None], base_head: str) -> bool:
    if not receipt or receipt.get("digest") != digest or dirty_files(root):
        return False
    if head(root) != receipt["head"] or run_git(root, "rev-parse", "HEAD^") != base_head:
        return False
    committed_paths = run_git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
    if set(committed_paths) != set(changes):
        return False
    return all(
        ((root / name).read_bytes() == data) if data is not None else not (root / name).exists()
        for name, data in changes.items()
    )

def transact(root: Path, state: Path, changes: dict[str, bytes | None],
             snapshot: dict[str, bytes], payload: dict, digest: str, agent: str) -> dict:
    """Applica un batch validato e mantiene il journal se l'esito resta ambiguo."""
    journal_path = state / "journal.json"
    before = {name: snapshot.get(name) for name in changes}
    journal = {
        "operation_id": payload["operation_id"], "digest": digest,
        "base_head": payload["base_head"],
        "preimages": {name: base64.b64encode(data).decode("ascii") if data is not None else None
                      for name, data in before.items()},
    }
    atomic_write(journal_path, json.dumps(journal, ensure_ascii=True).encode("utf-8"))
    receipt = {"operation_id": payload["operation_id"], "digest": digest,
               "changed": list(changes), "agent": agent}
    timestamp = datetime.now(UTC).isoformat()
    reason = reason_line(payload["reason"])
    message = (
        f"Memoria condivisa: {agent} {timestamp}\n\n"
        + (f"Motivo: {reason}\n\n" if reason else "")
        + f"Shared-Memory-Operation: {payload['operation_id']}\n"
        "Shared-Memory-Receipt: " + json.dumps(receipt, separators=(",", ":"))
    )
    try:
        for name, content in changes.items():
            if name != "MEMORY.md" and content is not None:
                atomic_write(root / name, content)
        if "MEMORY.md" in changes:
            atomic_write(root / "MEMORY.md", changes["MEMORY.md"])
        for name, content in changes.items():
            if content is None:
                (root / name).unlink()
        run_git(root, "add", "--", *changes)
        run_git(root, "commit", "-m", message)
        committed = find_operation(root, payload["operation_id"])
        if not _commit_matches(root, committed, digest, changes, payload["base_head"]):
            raise StoreError("Verifica del commit non riuscita", 1)
    except Exception as failure:
        LOGGER.warning("Transazione interrotta, controllo HEAD e journal")
        try:
            committed = find_operation(root, payload["operation_id"])
            if _commit_matches(root, committed, digest, changes, payload["base_head"]):
                journal_path.unlink()
                return {"ok": True, "head": committed["head"], "operation_id": payload["operation_id"],
                        "changed": list(changes), "replayed": False}
            if head(root) != payload["base_head"]:
                raise StoreError("HEAD cambiato durante la transazione", 1)
            _restore(root, before, payload["base_head"])
            journal_path.unlink()
        except Exception as recovery_error:
            raise StoreError(f"Recupero manuale richiesto: {journal_path}", 1) from recovery_error
        raise StoreError("Commit fallito; preimage ripristinato e repository pulito", 1) from failure
    journal_path.unlink()
    return {"ok": True, "head": committed["head"], "operation_id": payload["operation_id"],
            "changed": list(changes), "replayed": False}

