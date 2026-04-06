from __future__ import annotations

from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from astrbot.core.message.components import File, Image, Video
except Exception:  # pragma: no cover
    from astrbot.api.message_components import File, Image, Video  # type: ignore

from .models import OutputFile
from .config import PluginConfig


class Sender:
    def __init__(self, config: PluginConfig | None = None):
        self.cfg = config

    @staticmethod
    def _humanize_send_error(exc: Exception) -> str:
        raw = str(exc)
        lower = raw.lower()
        if "eacces" in lower or "permission denied" in lower:
            return "发送失败：平台进程无权读取该文件，请检查共享目录权限。"
        if "enoent" in lower or "no such file" in lower:
            return "发送失败：平台进程找不到该文件，请检查宿主机/容器路径映射。"
        return f"发送失败: {raw}"

    async def _check_file(self, event: AstrMessageEvent, path: str, kind: str) -> Path | None:
        raw = str(path or "").strip()
        mapped = self.cfg.remap_path(raw) if self.cfg else raw
        candidates: list[str] = [mapped]
        if raw and mapped != raw:
            candidates.append(raw)

        for candidate in candidates:
            if not candidate:
                continue
            p = Path(candidate)
            try:
                rp = p.resolve(strict=True)
            except Exception:
                continue
            if rp.is_file():
                if candidate != raw:
                    logger.info(f"{kind}路径映射: {raw} -> {candidate}")
                return rp

        if raw and mapped != raw:
            await self.send_plain(
                event,
                f"{kind}不存在: {mapped}\n原始路径: {raw}\n请检查 path_map 和容器挂载路径。",
            )
        else:
            await self.send_plain(event, f"{kind}不存在: {raw}")
        return None

    async def send_plain(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(event.plain_result(text))

    async def send_file(
        self,
        event: AstrMessageEvent,
        path: str,
        name: str | None = None,
        caption: str | None = None,
    ) -> bool:
        p = await self._check_file(event, path, "文件")
        if p is None:
            return False
        if caption:
            await self.send_plain(event, caption)
        file_name = name or p.name
        seg = File(file=str(p), name=file_name)
        try:
            await event.send(event.chain_result([seg]))
            return True
        except Exception as exc:
            logger.warning(f"文件发送失败: {exc}")
            await self.send_plain(event, self._humanize_send_error(exc))
            return False

    async def send_image(
        self,
        event: AstrMessageEvent,
        path: str,
        caption: str | None = None,
    ) -> bool:
        p = await self._check_file(event, path, "图片")
        if p is None:
            return False
        if caption:
            await self.send_plain(event, caption)
        try:
            if hasattr(Image, "fromFileSystem"):
                seg = Image.fromFileSystem(str(p))
            else:
                seg = Image(file=str(p))
            await event.send(event.chain_result([seg]))
            return True
        except Exception as exc:
            logger.warning(f"图片发送失败，回退文件发送: {exc}")
            return await self.send_file(event, str(p), p.name)

    async def send_video_or_file(
        self,
        event: AstrMessageEvent,
        path: str,
        caption: str | None = None,
    ) -> bool:
        p = await self._check_file(event, path, "视频")
        if p is None:
            return False
        if caption:
            await self.send_plain(event, caption)
        try:
            if hasattr(Video, "fromFileSystem"):
                seg = Video.fromFileSystem(str(p))
            else:
                seg = Video(file=str(p))
            await event.send(event.chain_result([seg]))
            return True
        except Exception as exc:
            logger.warning(f"视频发送失败，回退文件发送: {exc}")
            return await self.send_file(event, str(p), p.name)

    async def send_output_file(self, event: AstrMessageEvent, item: OutputFile) -> bool:
        ext = Path(item.path).suffix.lower()
        if item.kind == "image" and ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return await self.send_image(event, item.path)
        if item.kind == "video" or ext in {".mp4", ".m4v", ".mov"}:
            return await self.send_video_or_file(event, item.path)
        return await self.send_file(event, item.path, item.name)
