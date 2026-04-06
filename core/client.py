from __future__ import annotations

from typing import Any

from .backend import LocalAppleMusicBackend
from .config import PluginConfig


class AppleMusicClient:
    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.backend = LocalAppleMusicBackend(config)

    async def initialize(self) -> None:
        await self.backend.initialize()

    async def close(self) -> None:
        await self.backend.close()

    async def health(self) -> dict[str, Any]:
        return await self.backend.health()

    async def search(
        self,
        media_type: str,
        query: str,
        storefront: str,
        limit: int,
        offset: int = 0,
    ) -> dict[str, Any]:
        return await self.backend.search(
            media_type=media_type,
            query=query,
            storefront=storefront,
            limit=limit,
            offset=offset,
        )

    async def resolve_url(self, text_or_url: str) -> dict[str, Any]:
        return await self.backend.resolve_url(text_or_url)

    async def artist_children(
        self,
        artist_id: str,
        relationship: str,
        storefront: str,
        limit: int,
        offset: int = 0,
    ) -> dict[str, Any]:
        return await self.backend.artist_children(
            artist_id=artist_id,
            relationship=relationship,
            storefront=storefront,
            limit=limit,
            offset=offset,
        )

    async def download(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.backend.download(payload)

    async def job(self, job_id: str) -> dict[str, Any]:
        return await self.backend.job(job_id)

    async def artwork(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.backend.artwork(payload)

    async def lyrics(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.backend.lyrics(payload)
