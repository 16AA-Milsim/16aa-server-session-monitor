import json
import os
from typing import Optional


class StateStore:
    def __init__(self, path: str):
        self.path = path

    def _read(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def get_panel_message_id(self) -> Optional[int]:
        raw = self._read().get("panel_message_id")
        try:
            return int(raw) if raw is not None else None
        except Exception:
            return None

    def set_panel_message_id(self, message_id: Optional[int]) -> None:
        data = self._read()
        data["panel_message_id"] = message_id
        self._write(data)

