"""CLI JSON dello store di memoria condivisa."""
import argparse
import json
import logging
import sys
from pathlib import Path

from config import load_settings
from memory_store import MemoryStore
from memory_store.errors import StoreError


class JsonParser(argparse.ArgumentParser):
    """Converte gli errori di argparse nel protocollo JSON."""

    def error(self, message: str) -> None:
        """Rifiuta argomenti invalidi senza stampare contenuti utente."""
        raise StoreError("Argomenti CLI non validi", 2)


def main() -> int:
    """Esegue un comando ed emette una sola risposta JSON UTF-8."""
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        parser = JsonParser(add_help=False)
        parser.add_argument("--root")
        parser.add_argument("--lock-timeout", type=float)
        commands = parser.add_subparsers(dest="command", required=True)
        for name in ("index", "doctor"):
            commands.add_parser(name, add_help=False)
        read_parser = commands.add_parser("read", add_help=False)
        read_parser.add_argument("files", nargs="+")
        search_parser = commands.add_parser("search", add_help=False)
        search_parser.add_argument("query")
        apply_parser = commands.add_parser("apply", add_help=False)
        apply_parser.add_argument("--agent", choices=("codex", "claude", "maintenance"), required=True)
        apply_parser.add_argument("--request")
        args = parser.parse_args()
        store = MemoryStore(load_settings(args.root, args.lock_timeout))
        code = 0
        if args.command == "index":
            result = store.read(["MEMORY.md"])
        elif args.command == "read":
            result = store.read(args.files)
        elif args.command == "search":
            result = store.search(args.query)
        elif args.command == "doctor":
            result = store.doctor()
            code = 0 if result["ok"] else 3
        else:
            raw = Path(args.request).read_text(encoding="utf-8-sig") if args.request else sys.stdin.read()
            result = store.apply(json.loads(raw), args.agent)
    except StoreError as exc:
        result, code = {"error": str(exc)}, exc.code
    except (ValueError, UnicodeError):
        result, code = {"error": "Richiesta JSON, testo o configurazione non validi"}, 2
    except Exception:
        logging.error("Errore operativo dello store; nessun contenuto personale nei log")
        result, code = {"error": "Errore operativo dello store"}, 1
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

