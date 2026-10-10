"""Состояние плагина — файл state.json в служебной папке (PULSE_PLUGIN_DATA): аккаунты с токенами, подключения,
незавершённый вход. Запись атомарная (временный файл и rename), права 0600; токены наружу не отдаются.

    {"device_id": "…",
     "accounts": {"<uid>": {"uid", "login", "name", "access_token", "refresh_token", "expires_at", "expired"}},
     "mounts": [{"name", "kind": "account"|"public", "account", "path", "url", "title", "target_type"}],
     "login": {"user_code", "verification_url", "device_code", "expires_at", "interval", "status", "error", "account"}}
"""

import json
import os
import tempfile
import threading
from pathlib import Path


class Store:
    def __init__(self, directory: Path):
        self.path = Path(directory) / "state.json"
        self._lock = threading.RLock()

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def change(self, fn):
        """Прочитать, поменять функцией fn(data) и записать — под замком. Возвращает результат fn."""
        with self._lock:
            data = self.load()
            result = fn(data)
            self._write(data)
            return result

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
