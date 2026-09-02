"""Persistent dedup state so an email is never posted to Teams twice."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

MAX_TRACKED_IDS = 5000


class ProcessedStore:
    """Tracks processed email IDs and the last successful poll time in a JSON file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._ids: List[str] = []
        self._id_set: set[str] = set()
        self.last_poll_utc: Optional[str] = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self._ids = list(data.get("processed_ids", []))[-MAX_TRACKED_IDS:]
        self._id_set = set(self._ids)
        self.last_poll_utc = data.get("last_poll_utc")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "processed_ids": self._ids[-MAX_TRACKED_IDS:],
            "last_poll_utc": self.last_poll_utc,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._id_set

    def mark_processed(self, message_id: str) -> None:
        if message_id in self._id_set:
            return
        self._ids.append(message_id)
        self._id_set.add(message_id)
        if len(self._ids) > MAX_TRACKED_IDS:
            dropped = self._ids[: len(self._ids) - MAX_TRACKED_IDS]
            self._ids = self._ids[-MAX_TRACKED_IDS:]
            self._id_set.difference_update(dropped)
