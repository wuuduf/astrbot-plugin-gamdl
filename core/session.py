from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .config import PluginConfig
from .models import SelectionState, SessionSettings

try:
    from astrbot.core.utils.session_waiter import SessionFilter
except Exception:  # pragma: no cover
    class SessionFilter:  # type: ignore[no-redef]
        def filter(self, event):
            return getattr(event, "unified_msg_origin", "")


class UnifiedMsgOriginFilter(SessionFilter):
    def filter(self, event):
        return event.unified_msg_origin


class SessionStore:
    def __init__(self, config: PluginConfig):
        self.cfg = config
        self._settings: dict[str, SessionSettings] = {}
        self._pending: dict[str, SelectionState] = {}

    async def initialize(self) -> None:
        self._settings = self._load_settings(self.cfg.session_settings_path)

    async def close(self) -> None:
        self._save_settings(self.cfg.session_settings_path, self._settings)
        self._pending.clear()

    def get_settings(self, session_key: str) -> SessionSettings:
        settings = self._settings.get(session_key)
        if settings:
            return settings
        default = SessionSettings(transfer_mode=self.cfg.default_transfer_mode)
        self._settings[session_key] = default
        return default

    def update_settings(self, session_key: str, patch: dict[str, Any]) -> SessionSettings:
        settings = self.get_settings(session_key)
        data = settings.to_dict()
        data.update(patch)
        merged = SessionSettings.from_dict(data)
        self._settings[session_key] = merged
        self._save_settings(self.cfg.session_settings_path, self._settings)
        return merged

    def set_pending(self, session_key: str, state: SelectionState) -> None:
        self._pending[session_key] = state

    def get_pending(self, session_key: str) -> SelectionState | None:
        return self._pending.get(session_key)

    def clear_pending(self, session_key: str) -> None:
        self._pending.pop(session_key, None)

    def clear_expired_pending(self, timeout_sec: int) -> None:
        if timeout_sec <= 0:
            self._pending.clear()
            return
        now = time.time()
        keys = [
            key
            for key, state in self._pending.items()
            if now - state.created_ts >= timeout_sec
        ]
        for key in keys:
            self._pending.pop(key, None)

    @staticmethod
    def _load_settings(path: Path) -> dict[str, SessionSettings]:
        if not path.exists():
            return {}
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"读取会话设置失败: {exc}")
            return {}
        if not isinstance(obj, dict):
            return {}
        out: dict[str, SessionSettings] = {}
        for key, value in obj.items():
            if isinstance(value, dict):
                out[str(key)] = SessionSettings.from_dict(value)
        return out

    @staticmethod
    def _save_settings(path: Path, data: dict[str, SessionSettings]) -> None:
        try:
            payload = {key: asdict(value) for key, value in data.items()}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"保存会话设置失败: {exc}")
