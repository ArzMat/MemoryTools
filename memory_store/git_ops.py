"""Operazioni Git limitate al repository e ai path del batch."""
import json
import os
import subprocess
from pathlib import Path

from .errors import StoreError


def run_git(root: Path, *args: str) -> str:
    """Invoca Git senza shell e senza restituire stderr potenzialmente privato."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root), *args],
        capture_output=True, env=environment,
    )
    if result.returncode:
        raise StoreError("Operazione Git fallita", 1)
    return result.stdout.decode("utf-8", errors="replace").rstrip("\r\n")


def head(root: Path) -> str:
    """Legge l'identificatore HEAD corrente."""
    return run_git(root, "rev-parse", "--verify", "HEAD")


def dirty_files(root: Path) -> list[str]:
    """Include staged, unstaged e file untracked."""
    output = run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    return [entry[3:] for entry in output.split("\0") if entry]


def find_operation(root: Path, operation_id: str) -> dict | None:
    """Cerca la ricevuta strutturata nei commit raggiungibili da HEAD."""
    history = run_git(root, "log", "--format=%H%x00%B%x00", "--fixed-strings", f"--grep=Shared-Memory-Operation: {operation_id}")
    pieces = history.split("\0")
    for position in range(0, len(pieces) - 1, 2):
        commit_head, body = pieces[position].strip(), pieces[position + 1]
        lines = body.splitlines()
        if f"Shared-Memory-Operation: {operation_id}" not in lines:
            continue
        for line in lines:
            if line.startswith("Shared-Memory-Receipt: "):
                try:
                    receipt = json.loads(line.removeprefix("Shared-Memory-Receipt: "))
                    receipt["head"] = commit_head
                    return receipt
                except (ValueError, TypeError):
                    raise StoreError("Ricevuta Git non valida", 3) from None
    return None

