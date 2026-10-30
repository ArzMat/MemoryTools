"""Configurazione unica del tooling locale."""
from dataclasses import dataclass
from pathlib import Path
import math
import tomllib


@dataclass(frozen=True)
class Settings:
    """Parametri non segreti della memoria condivisa."""
    root: Path
    lock_timeout: float
    search_limit: int
    excerpt_chars: int
    index_max_lines: int
    index_max_bytes: int


def load_settings(root: str | None = None, lock_timeout: float | None = None) -> Settings:
    """Carica config.toml, poi config.local.toml se c'e', e gli override CLI espliciti.

    config.local.toml (non versionato) contiene i valori propri della macchina,
    per esempio la root dell'archivio; vince su config.toml chiave per chiave.
    """
    base = Path(__file__).with_name("config.toml")
    with base.open("rb") as stream:
        values = tomllib.load(stream)
    local = base.with_name("config.local.toml")
    if local.exists():
        with local.open("rb") as stream:
            values.update(tomllib.load(stream))
    values["root"] = Path(root or values["root"]).expanduser().absolute()
    if lock_timeout is not None:
        values["lock_timeout"] = lock_timeout
    if not math.isfinite(values["lock_timeout"]) or values["lock_timeout"] < 0:
        raise ValueError("Timeout lock non valido")
    return Settings(**values)

