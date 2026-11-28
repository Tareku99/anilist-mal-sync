"""Confirmed common values used to resolve bidirectional changes."""

import json
from pathlib import Path
from tempfile import NamedTemporaryFile


class SyncHistory:
    def __init__(self, path: Path, accounts: tuple[str, str]):
        self.path = path
        self.accounts = list(accounts)
        self.entries: dict[str, dict[str, object]] = {}
        if path.exists():
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("version") != 1:
                raise ValueError("Unsupported sync history format")
            if document.get("accounts") == self.accounts:
                self.entries = document["entries"]

    def values(self, mal_id: int) -> dict[str, object] | None:
        return self.entries.get(str(mal_id))

    def remember(self, mal_id: int, agreed: dict[str, object]) -> None:
        self.entries.setdefault(str(mal_id), {}).update(agreed)

    def clear_values(self, mal_id: int) -> None:
        """Forget potentially private values while preserving that both lists saw the title."""
        if str(mal_id) in self.entries:
            self.entries[str(mal_id)] = {}

    def save(self) -> None:
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({"version": 1, "accounts": self.accounts, "entries": self.entries}, handle)
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
