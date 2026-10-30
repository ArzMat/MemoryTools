"""Errori pubblici senza contenuti personali."""


class StoreError(Exception):
    """Errore del protocollo con exit code stabile."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code

