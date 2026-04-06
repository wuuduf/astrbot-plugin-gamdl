from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


class PluginConfig:
    plugin_name = "astrbot_plugin_gamdl"

    def __init__(self, config: AstrBotConfig, context: Context):
        self._raw = config
        self.context = context

        self.search_limit = self._get_int("search_limit", 8)
        self.selection_timeout = self._get_int("selection_timeout", 90)
        self.auto_parse_url = self._get_bool("auto_parse_url", True)
        self.default_transfer_mode = self._normalize_transfer_mode(
            self._get_str("default_transfer_mode", "one(逐个)")
        )
        self.job_progress_notify = self._get_bool("job_progress_notify", True)
        self.job_progress_interval = max(5, self._get_int("job_progress_interval", 20))
        self.max_concurrency = min(4, max(1, self._get_int("max_concurrency", 1)))
        self.job_timeout_seconds = max(60, self._get_int("job_timeout_seconds", 7200))
        self.clean_cache_on_reload = self._get_bool("clean_cache_on_reload", False)
        self.default_storefront = self._get_str("default_storefront", "us").lower().strip() or "us"
        self.path_map_raw = self._get_str("path_map", "").strip()
        self.path_mappings = self._parse_path_mappings(self.path_map_raw)

        self.use_wrapper = self._get_bool("use_wrapper", True)
        self.wrapper_account_url = self._get_str("wrapper_account_url", "http://127.0.0.1:30020").strip()
        self.language = self._get_str("language", "zh-Hans-CN").strip() or "zh-Hans-CN"
        self.song_codec_priority = self._parse_csv(self._get_str("song_codec_priority", "alac,aac-legacy"))
        if not self.song_codec_priority:
            self.song_codec_priority = ["alac", "aac-legacy"]
        self.artist_auto_select = self._get_str("artist_auto_select", "all-albums").strip().lower() or "all-albums"
        self.allow_music_video = self._get_bool("allow_music_video", True)
        self.allow_post_video = self._get_bool("allow_post_video", False)
        self.allow_large_file_zip = self._get_bool("allow_large_file_zip", True)
        self.gamdl_invoke_mode = self._normalize_invoke_mode(self._get_str("gamdl_invoke_mode", "python"))
        self.gamdl_executable = self._get_str("gamdl_executable", "gamdl").strip() or "gamdl"
        self.gamdl_python_path = self._get_str("gamdl_python_path", "").strip() or None
        self.cover_size = self._get_str("cover_size", "3000x3000").strip() or "3000x3000"
        self.cover_format = self._normalize_cover_format(self._get_str("cover_format", "jpg"))

        self.data_dir = Path(get_astrbot_plugin_data_path()) / self.plugin_name
        self.data_dir.mkdir(parents=True, exist_ok=True)

        temp_raw = self._get_str("temp_dir", "").strip()
        self.temp_dir = Path(temp_raw).expanduser().resolve() if temp_raw else (self.data_dir / "temp")
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        download_raw = self._get_str("download_dir", "").strip()
        self.download_dir = (
            Path(download_raw).expanduser().resolve()
            if download_raw
            else (self.data_dir / "downloads")
        )
        self.download_dir.mkdir(parents=True, exist_ok=True)

        cookies_raw = self._get_str("cookies_path", "").strip()
        self.cookies_path = (
            Path(cookies_raw).expanduser().resolve()
            if cookies_raw
            else (self.data_dir / "cookies.txt")
        )

        self.cache_db_path = self.data_dir / "cache.sqlite3"
        self.session_settings_path = self.data_dir / "session_settings.json"

    def maybe_clean_temp(self) -> None:
        if not self.clean_cache_on_reload:
            return
        try:
            if self.temp_dir.exists():
                shutil.rmtree(self.temp_dir)
            self.temp_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning(f"清理临时目录失败: {exc}")

    def _get(self, key: str, default: Any) -> Any:
        raw = self._raw
        if hasattr(raw, "get"):
            try:
                return raw.get(key, default)
            except Exception:
                pass
        if hasattr(raw, key):
            try:
                return getattr(raw, key)
            except Exception:
                pass
        return default

    def _get_str(self, key: str, default: str) -> str:
        val = self._get(key, default)
        if val is None:
            return default
        return str(val)

    def _get_int(self, key: str, default: int) -> int:
        val = self._get(key, default)
        try:
            return int(val)
        except Exception:
            return default

    def _get_bool(self, key: str, default: bool) -> bool:
        val = self._get(key, default)
        if isinstance(val, bool):
            return val
        text = str(val).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default

    @staticmethod
    def _normalize_transfer_mode(raw: str) -> str:
        text = (raw or "").lower()
        if "zip" in text:
            return "zip"
        return "one"

    @staticmethod
    def _normalize_invoke_mode(raw: str) -> str:
        text = (raw or "").strip().lower()
        if text in {"python", "subprocess"}:
            return text
        return "python"

    @staticmethod
    def _normalize_cover_format(raw: str) -> str:
        text = (raw or "").strip().lower()
        if text in {"jpg", "jpeg"}:
            return "jpg"
        if text == "png":
            return "png"
        return "jpg"

    @staticmethod
    def _parse_csv(raw: str) -> list[str]:
        text = (raw or "").strip()
        if not text:
            return []
        return [x.strip().lower() for x in text.split(",") if x.strip()]

    @staticmethod
    def _parse_path_mappings(raw: str) -> list[tuple[str, str]]:
        text = (raw or "").strip()
        if not text:
            return []
        out: list[tuple[str, str]] = []
        for chunk in re.split(r"[;\n]+", text):
            part = chunk.strip()
            if not part or "=>" not in part:
                continue
            src, dst = part.split("=>", 1)
            src = src.strip().strip('"').strip("'")
            dst = dst.strip().strip('"').strip("'")
            if src and dst:
                out.append((src, dst))
        return out

    def remap_path(self, path: str) -> str:
        raw = (path or "").strip()
        if not raw or not self.path_mappings:
            return raw

        def _join(dst: str, suffix: str) -> str:
            if not suffix:
                return dst
            if dst.endswith(("/", "\\")) and suffix.startswith(("/", "\\")):
                return dst.rstrip("/\\") + suffix
            if (not dst.endswith(("/", "\\"))) and (not suffix.startswith(("/", "\\"))):
                return dst + "/" + suffix
            return dst + suffix

        for src, dst in sorted(self.path_mappings, key=lambda x: len(x[0]), reverse=True):
            if raw == src:
                return dst
            src_norm = src.rstrip("/\\")
            if src_norm and (raw.startswith(src_norm + "/") or raw.startswith(src_norm + "\\")):
                suffix = raw[len(src_norm):]
                return _join(dst, suffix)
        return raw
