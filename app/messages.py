from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel


class MessageInput(BaseModel):
    recipient: Literal["monsieur_mounier", "madame_astride"]
    caller_name: str
    message: str
    callback_number: str | None
    language: Literal["fr"]
    confirmed_by_caller: bool


class MessageStore:
    def __init__(self, path: str | Path = "var/messages.json"):
        self.path = Path(path)
        self._lock = Lock()

    def list(self) -> list[dict]:
        with self._lock:
            return self._read()

    def save(self, message: MessageInput) -> dict:
        record = {
            "message_id": uuid4().hex,
            **message.model_dump(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            messages = self._read()
            messages.append(record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(messages, ensure_ascii=False, indent=2) + "\n")
            temp.replace(self.path)
        return {"ok": True, "message_id": record["message_id"]}

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text())
        if not isinstance(data, list):
            raise ValueError("message store must contain a JSON list")
        return data


def runtime_message_store(runtime) -> MessageStore:
    if not hasattr(runtime, "message_store"):
        config_path = getattr(getattr(runtime, "store", None), "path", Path("var/config.json"))
        runtime.message_store = MessageStore(Path(config_path).parent / "messages.json")
    return runtime.message_store
