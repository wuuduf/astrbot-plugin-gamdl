from __future__ import annotations

import asyncio
from typing import Any

from .client import AppleMusicClient
from .config import PluginConfig
from .models import JobStatus, SearchItem, ServiceError, SessionSettings


class AppleMusicService:
    def __init__(self, client: AppleMusicClient, config: PluginConfig):
        self.client = client
        self.cfg = config

    async def check_health(self) -> None:
        await self.client.health()

    async def search(
        self,
        media_type: str,
        query: str,
        storefront: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[SearchItem], bool, str]:
        payload = await self.client.search(
            media_type=media_type,
            query=query,
            storefront=storefront or self.cfg.default_storefront,
            limit=limit or self.cfg.search_limit,
            offset=offset,
        )
        items = [SearchItem.from_dict(it) for it in payload.get("items", []) or []]
        has_next = bool(payload.get("has_next", False))
        sf = str(payload.get("storefront", storefront or self.cfg.default_storefront) or self.cfg.default_storefront)
        return items, has_next, sf

    async def resolve_url(self, text_or_url: str) -> dict[str, Any]:
        payload = await self.client.resolve_url(text_or_url)
        target = payload.get("target")
        if not isinstance(target, dict):
            raise ServiceError("服务未返回可用目标")
        normalized = self._normalize_target(target)
        if not normalized.get("media_type"):
            raise ServiceError("服务未返回 media_type")
        if not normalized.get("id"):
            raise ServiceError("服务未返回 id")
        return normalized

    async def artist_children(
        self,
        artist_id: str,
        relationship: str,
        storefront: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[SearchItem], bool]:
        payload = await self.client.artist_children(
            artist_id=artist_id,
            relationship=relationship,
            storefront=storefront,
            limit=limit or self.cfg.search_limit,
            offset=offset,
        )
        items = [SearchItem.from_dict(it) for it in payload.get("items", []) or []]
        has_next = bool(payload.get("has_next", False))
        return items, has_next

    async def create_download_job(
        self,
        target: dict[str, Any],
        settings: SessionSettings,
        transfer_mode: str | None = None,
    ) -> str:
        payload = {
            "media_type": target.get("media_type"),
            "id": target.get("id"),
            "url": target.get("raw_url") or target.get("url") or "",
            "storefront": target.get("storefront") or self.cfg.default_storefront,
            "quality": settings.quality,
            "aac_type": settings.aac_type,
            "mv_audio_type": settings.mv_audio_type,
            "lyrics_format": settings.lyrics_format,
            "include_lyrics": settings.include_lyrics,
            "include_cover": settings.include_cover,
            "include_animated_cover": settings.include_animated_cover,
            "transfer_mode": transfer_mode or settings.transfer_mode,
        }
        result = await self.client.download(payload)
        job_id = str(result.get("job_id", "")).strip()
        if not job_id:
            raise ServiceError("服务未返回 job_id")
        return job_id

    async def get_job(self, job_id: str) -> JobStatus:
        payload = await self.client.job(job_id)
        return JobStatus.from_dict(payload)

    async def wait_job(
        self,
        job_id: str,
        poll_interval: float = 2.0,
        timeout: float = 7200.0,
    ) -> JobStatus:
        start = asyncio.get_running_loop().time()
        while True:
            status = await self.get_job(job_id)
            if status.status in {"completed", "failed"}:
                return status
            now = asyncio.get_running_loop().time()
            if now - start >= timeout:
                raise ServiceError("下载任务等待超时")
            await asyncio.sleep(poll_interval)

    async def artwork(
        self,
        target: dict[str, Any],
        animated: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "media_type": target.get("media_type"),
            "id": target.get("id"),
            "url": target.get("raw_url") or target.get("url") or "",
            "storefront": target.get("storefront") or self.cfg.default_storefront,
            "animated": animated,
        }
        return await self.client.artwork(payload)

    async def lyrics(
        self,
        target: dict[str, Any],
        output_format: str,
        transfer_mode: str = "one",
    ) -> dict[str, Any]:
        payload = {
            "media_type": target.get("media_type"),
            "id": target.get("id"),
            "url": target.get("raw_url") or target.get("url") or "",
            "storefront": target.get("storefront") or self.cfg.default_storefront,
            "output_format": output_format,
            "transfer_mode": transfer_mode,
        }
        return await self.client.lyrics(payload)

    @staticmethod
    def _normalize_target(target: dict[str, Any]) -> dict[str, Any]:
        def pick(*keys: str) -> str:
            for key in keys:
                val = target.get(key)
                if val is None:
                    continue
                text = str(val).strip()
                if text:
                    return text
            return ""

        media_type = pick("media_type", "MediaType", "mediaType", "type")
        media_id = pick("id", "ID", "media_id", "MediaID")
        storefront = pick("storefront", "Storefront")
        raw_url = pick("raw_url", "rawUrl", "RawURL")
        url = pick("url", "URL")
        if not raw_url:
            raw_url = url
        if not url:
            url = raw_url
        return {
            "media_type": media_type,
            "id": media_id,
            "storefront": storefront or "us",
            "raw_url": raw_url,
            "url": url,
        }
