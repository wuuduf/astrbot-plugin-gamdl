from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from .core.client import AppleMusicClient
from .core.config import PluginConfig
from .core.models import OutputFile, SearchItem, SelectionState, ServiceError
from .core.renderer import Renderer
from .core.sender import Sender
from .core.service import AppleMusicService
from .core.session import SessionStore, UnifiedMsgOriginFilter
from .core.utils import (
    apply_setting_token,
    extract_first_apple_music_url,
    normalize_transfer_mode,
    parse_am_payload,
    parse_selection_action,
)


class AppleMusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        self.client = AppleMusicClient(self.cfg)
        self.service = AppleMusicService(self.client, self.cfg)
        self.sessions = SessionStore(self.cfg)
        self.renderer = Renderer()
        self.sender = Sender(self.cfg)
        self._tasks: set[asyncio.Task] = set()

    async def initialize(self):
        self.cfg.maybe_clean_temp()
        await self.sessions.initialize()
        await self.client.initialize()
        try:
            await self.service.check_health()
            logger.info("Apple Music backend ready.")
        except Exception as exc:
            logger.warning(f"Apple Music backend health check failed: {exc}")

    async def terminate(self):
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        await self.sessions.close()
        await self.client.close()

    @filter.command("am")
    async def am_entry(self, event: AstrMessageEvent):
        """Apple Music 主命令"""
        try:
            self.sessions.clear_expired_pending(self.cfg.selection_timeout)
            cmd, arg = parse_am_payload(event.message_str)
            if not cmd:
                await self.sender.send_plain(event, self.renderer.help_text())
                event.stop_event()
                return

            if cmd in {"搜歌", "song", "search_song"}:
                await self._handle_search(event, "song", arg)
            elif cmd in {"搜专", "专辑", "album", "search_album"}:
                await self._handle_search(event, "album", arg)
            elif cmd in {"搜人", "艺人", "artist", "search_artist"}:
                await self._handle_search(event, "artist", arg)
            elif cmd in {"help", "帮助", "h", "?"}:
                await self.sender.send_plain(event, self.renderer.help_text())
            elif cmd in {"链接", "url"}:
                await self._handle_link(event, arg)
            elif cmd in {"歌词", "lyrics", "lyric"}:
                await self._handle_lyrics_cmd(event, arg)
            elif cmd in {"封面", "cover"}:
                await self._handle_artwork_cmd(event, arg, animated=False)
            elif cmd in {"动态封面", "animatedcover", "motioncover"}:
                await self._handle_artwork_cmd(event, arg, animated=True)
            elif cmd in {"设置", "settings", "set"}:
                await self._handle_settings(event, arg)
            else:
                # 允许 /am 直接跟链接
                url = extract_first_apple_music_url(event.message_str)
                if url:
                    await self._handle_url_target(event, url)
                else:
                    await self.sender.send_plain(event, self.renderer.help_text())
        except ServiceError as exc:
            await self.sender.send_plain(event, str(exc))
        except Exception:
            logger.error(traceback.format_exc())
            await self.sender.send_plain(event, "命令处理失败，请稍后再试。")
        finally:
            event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_auto_parse_url(self, event: AstrMessageEvent):
        """自动识别 Apple Music 链接"""
        if not self.cfg.auto_parse_url:
            return
        if not event.is_at_or_wake_command:
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        if text.startswith("/") and text[1:].lower().startswith("am"):
            return
        url = extract_first_apple_music_url(text)
        if not url:
            return
        try:
            await self._handle_url_target(event, url)
            event.stop_event()
        except ServiceError as exc:
            await self.sender.send_plain(event, str(exc))
        except Exception:
            logger.error(traceback.format_exc())
            await self.sender.send_plain(event, "链接解析失败，请确认链接可访问。")

    async def _handle_search(self, event: AstrMessageEvent, media_type: str, query: str):
        query = query.strip()
        if not query:
            await self.sender.send_plain(event, f"用法: am {'搜歌' if media_type == 'song' else '搜专' if media_type == 'album' else '搜人'} <关键词>")
            return
        items, _has_next, storefront = await self.service.search(
            media_type=media_type,
            query=query,
            storefront=self.cfg.default_storefront,
            limit=self.cfg.search_limit,
        )
        if not items:
            await self.sender.send_plain(event, "没有找到结果。")
            return

        session_key = self._session_key(event)
        state = SelectionState(
            kind=media_type,
            title=query,
            items=items,
            storefront=storefront,
            created_ts=time.time(),
        )
        self.sessions.set_pending(session_key, state)
        await self.sender.send_plain(event, self.renderer.render_search(media_type, query, items))
        await self._wait_for_selection(event, session_key)

    async def _wait_for_selection(self, event: AstrMessageEvent, session_key: str):
        @session_waiter(timeout=self.cfg.selection_timeout, record_history_chains=False)
        async def picker(controller: SessionController, incoming: AstrMessageEvent):
            state = self.sessions.get_pending(session_key)
            if not state:
                controller.stop()
                return

            action = parse_selection_action(incoming.message_str)
            if not action:
                return

            idx = action.index
            if idx == 0:
                if len(state.items) != 1:
                    await self.sender.send_plain(
                        incoming,
                        "请使用“序号 专辑”或“序号 mv”，例如: 1 专辑",
                    )
                    return
                idx = 1

            if idx < 1 or idx > len(state.items):
                await self.sender.send_plain(incoming, "序号超出范围，请重新输入。")
                return

            item = state.items[idx - 1]
            done = await self._handle_selection_action(
                incoming,
                session_key,
                state,
                item,
                action.op,
            )
            if done:
                controller.stop()

        try:
            await picker(event, session_filter=UnifiedMsgOriginFilter())
        except TimeoutError:
            self.sessions.clear_pending(session_key)
            await self.sender.send_plain(event, "选择超时，已自动取消。")
        except Exception:
            self.sessions.clear_pending(session_key)
            logger.error(traceback.format_exc())
            await self.sender.send_plain(event, "处理选择时出错，请重新搜索。")

    async def _handle_selection_action(
        self,
        event: AstrMessageEvent,
        session_key: str,
        state: SelectionState,
        item: SearchItem,
        op: str,
    ) -> bool:
        target = {
            "media_type": item.media_type or self._fallback_media_type(state.kind),
            "id": item.item_id,
            "storefront": state.storefront or self.cfg.default_storefront,
            "url": item.url,
        }
        media_type = target["media_type"]

        if state.kind == "artist":
            if op in {"cover", "animated_cover", "lyrics", "download", "zip"}:
                relationship = "albums"
            elif op == "artist_mvs":
                relationship = "music-videos"
            else:
                relationship = "albums"
            if op == "cover":
                await self._send_artwork(event, target, animated=False)
                self.sessions.clear_pending(session_key)
                return True
            if op == "animated_cover":
                await self._send_artwork(event, target, animated=True)
                self.sessions.clear_pending(session_key)
                return True
            await self._expand_artist_children(
                event,
                session_key,
                artist_item=item,
                storefront=target["storefront"],
                relationship=relationship,
            )
            return False

        if op == "cover":
            await self._send_artwork(event, target, animated=False)
            self.sessions.clear_pending(session_key)
            return True
        if op == "animated_cover":
            await self._send_artwork(event, target, animated=True)
            self.sessions.clear_pending(session_key)
            return True
        if op == "lyrics":
            await self._send_lyrics(event, target, transfer_mode="zip" if op == "zip" else "one")
            self.sessions.clear_pending(session_key)
            return True

        transfer_mode = "one"
        if op == "zip":
            transfer_mode = "zip"
        elif media_type in {"song", "album", "playlist", "station"}:
            transfer_mode = self.sessions.get_settings(session_key).transfer_mode

        await self._queue_download(event, target, transfer_mode=transfer_mode)
        self.sessions.clear_pending(session_key)
        return True

    async def _expand_artist_children(
        self,
        event: AstrMessageEvent,
        session_key: str,
        artist_item: SearchItem,
        storefront: str,
        relationship: str,
    ) -> None:
        rel = "artist_album" if relationship == "albums" else "artist_mv"
        items, _has_next = await self.service.artist_children(
            artist_id=artist_item.item_id,
            relationship=relationship,
            storefront=storefront,
            limit=self.cfg.search_limit,
        )
        if not items:
            await self.sender.send_plain(event, "该艺人暂无可浏览内容。")
            return
        state = SelectionState(
            kind=rel,
            title=artist_item.name,
            artist_name=artist_item.name,
            items=items,
            storefront=storefront,
            created_ts=time.time(),
        )
        self.sessions.set_pending(session_key, state)
        await self.sender.send_plain(event, self.renderer.render_search(rel, artist_item.name, items))

    async def _handle_link(self, event: AstrMessageEvent, arg: str):
        url = extract_first_apple_music_url(arg)
        if not url:
            await self.sender.send_plain(event, "用法: am 链接 <apple music url>")
            return
        await self._handle_url_target(event, url)

    async def _handle_url_target(self, event: AstrMessageEvent, raw: str):
        target = await self.service.resolve_url(raw)
        media_type = str(target.get("media_type", "")).strip()
        if not media_type:
            raise ServiceError("链接类型未知")

        if media_type == "artist":
            artist_item = SearchItem(
                media_type="artist",
                item_id=str(target.get("id", "") or ""),
                name="艺人",
                url=str(target.get("raw_url", "") or ""),
            )
            session_key = self._session_key(event)
            state = SelectionState(
                kind="artist",
                title="artist-url",
                items=[artist_item],
                storefront=str(target.get("storefront", self.cfg.default_storefront)),
                created_ts=time.time(),
            )
            self.sessions.set_pending(session_key, state)
            await self.sender.send_plain(event, "识别到艺人链接，回复“专辑”或“mv”继续，或回复“1 专辑”。")
            await self._wait_for_selection(event, session_key)
            return

        session_key = self._session_key(event)
        transfer_mode = "one"
        if media_type in {"song", "album", "playlist", "station"}:
            transfer_mode = self.sessions.get_settings(session_key).transfer_mode
        await self._queue_download(event, target, transfer_mode=transfer_mode)

    async def _handle_lyrics_cmd(self, event: AstrMessageEvent, arg: str):
        arg = arg.strip()
        if not arg:
            await self.sender.send_plain(event, "用法: am 歌词 <song-url|song-id|album-url|album-id>")
            return
        target = await self._resolve_target_from_text(arg, default_media_type="song")
        media_type = str(target.get("media_type", ""))
        if media_type not in {"song", "album"}:
            await self.sender.send_plain(event, "歌词仅支持 song/album。")
            return
        settings = self.sessions.get_settings(self._session_key(event))
        transfer_mode = "zip" if media_type == "album" else "one"
        await self._send_lyrics(event, target, transfer_mode=transfer_mode, output_format=settings.lyrics_format)

    async def _handle_artwork_cmd(self, event: AstrMessageEvent, arg: str, animated: bool):
        arg = arg.strip()
        if not arg:
            usage = "am 动态封面 <url|type id>" if animated else "am 封面 <url|type id>"
            await self.sender.send_plain(event, f"用法: {usage}")
            return
        target = await self._resolve_target_from_text(arg, default_media_type="song")
        await self._send_artwork(event, target, animated=animated)

    async def _handle_settings(self, event: AstrMessageEvent, arg: str):
        session_key = self._session_key(event)
        settings = self.sessions.get_settings(session_key)
        if not arg.strip():
            await self.sender.send_plain(event, self.renderer.render_settings(settings))
            return

        tokens = [t for t in arg.strip().split() if t]
        data = settings.to_dict()
        msgs: list[str] = []
        for token in tokens:
            ok, msg = apply_setting_token(data, token)
            if ok and msg:
                msgs.append(msg)

        if not msgs:
            await self.sender.send_plain(
                event,
                "可设置项: alac/flac/aac/atmos, aac-lc/aac-binaural/aac-downmix, mv-atmos/mv-ac3/mv-aac, lrc/ttml, zip/逐个, 歌词开关/封面开关/动态封面开关",
            )
            return

        merged = self.sessions.update_settings(session_key, data)
        await self.sender.send_plain(event, "\n".join(msgs + [self.renderer.render_settings(merged)]))

    async def _queue_download(self, event: AstrMessageEvent, target: dict[str, Any], transfer_mode: str):
        session_key = self._session_key(event)
        settings = self.sessions.get_settings(session_key)
        transfer_mode = normalize_transfer_mode(transfer_mode)
        job_id = await self.service.create_download_job(target, settings, transfer_mode=transfer_mode)
        await self.sender.send_plain(event, self.renderer.render_job_queued(job_id))
        self._spawn_task(self._watch_download_job(event, job_id, prefer_zip=(transfer_mode == "zip")))

    async def _watch_download_job(self, event: AstrMessageEvent, job_id: str, prefer_zip: bool):
        start_ts = time.monotonic()
        next_notify_ts = start_ts + float(self.cfg.job_progress_interval)
        try:
            while True:
                status = await self.service.get_job(job_id)
                current = (status.status or "").strip().lower()
                if current in {"completed", "failed"}:
                    break

                if self.cfg.job_progress_notify and time.monotonic() >= next_notify_ts:
                    elapsed = int(time.monotonic() - start_ts)
                    await self.sender.send_plain(
                        event,
                        self.renderer.render_job_progress(job_id, current or "running", elapsed),
                    )
                    next_notify_ts = time.monotonic() + float(self.cfg.job_progress_interval)

                await asyncio.sleep(2.0)
        except Exception as exc:
            await self.sender.send_plain(event, self.renderer.render_job_failed(job_id, str(exc)))
            return

        if status.status == "failed":
            await self.sender.send_plain(event, self.renderer.render_job_failed(job_id, status.error or "未知错误"))
            return

        if not status.result:
            await self.sender.send_plain(event, self.renderer.render_job_failed(job_id, "服务未返回结果"))
            return

        sent = 0
        zip_file = status.result.zip_file
        if prefer_zip and zip_file:
            try:
                ok = await self.sender.send_file(event, zip_file.path, zip_file.name)
            except Exception:
                logger.error(traceback.format_exc())
                ok = False
            if ok:
                sent += 1
            else:
                # ZIP 发送失败时回退逐个发送
                for item in status.result.files:
                    try:
                        ok_item = await self.sender.send_output_file(event, item)
                    except Exception:
                        logger.error(traceback.format_exc())
                        ok_item = False
                    if ok_item:
                        sent += 1
        else:
            for item in status.result.files:
                try:
                    ok_item = await self.sender.send_output_file(event, item)
                except Exception:
                    logger.error(traceback.format_exc())
                    ok_item = False
                if ok_item:
                    sent += 1

        if sent == 0:
            await self.sender.send_plain(
                event,
                "任务已完成但未发送成功。请检查文件路径映射(path_map)、挂载目录可见性和读取权限(EACCES/ENOENT)。",
            )
        await self.sender.send_plain(event, self.renderer.render_job_done(job_id, sent))

    async def _send_artwork(self, event: AstrMessageEvent, target: dict[str, Any], animated: bool):
        try:
            payload = await self.service.artwork(target, animated=animated)
            file_data = payload.get("file") if isinstance(payload, dict) else None
            if not isinstance(file_data, dict):
                raise ServiceError("服务未返回文件")
            item = OutputFile.from_dict(file_data)
            if animated:
                await self.sender.send_video_or_file(event, item.path)
            else:
                await self.sender.send_output_file(event, item)
        except Exception as exc:
            await self.sender.send_plain(event, f"{'动态封面' if animated else '封面'}获取失败: {exc}")

    async def _send_lyrics(
        self,
        event: AstrMessageEvent,
        target: dict[str, Any],
        transfer_mode: str,
        output_format: str | None = None,
    ):
        try:
            settings = self.sessions.get_settings(self._session_key(event))
            payload = await self.service.lyrics(
                target=target,
                output_format=output_format or settings.lyrics_format,
                transfer_mode=normalize_transfer_mode(transfer_mode),
            )
            sent = 0
            zip_obj = payload.get("zip_file") if isinstance(payload, dict) else None
            if normalize_transfer_mode(transfer_mode) == "zip" and isinstance(zip_obj, dict):
                zf = OutputFile.from_dict(zip_obj)
                if await self.sender.send_file(event, zf.path, zf.name):
                    sent += 1
            else:
                for obj in payload.get("files", []) if isinstance(payload, dict) else []:
                    if isinstance(obj, dict):
                        item = OutputFile.from_dict(obj)
                        if await self.sender.send_file(event, item.path, item.name):
                            sent += 1
            await self.sender.send_plain(event, f"歌词导出完成，已发送 {sent} 个文件。")
        except Exception as exc:
            await self.sender.send_plain(event, f"歌词导出失败: {exc}")

    async def _resolve_target_from_text(self, text: str, default_media_type: str) -> dict[str, Any]:
        raw = text.strip()
        url = extract_first_apple_music_url(raw)
        if url:
            return await self.service.resolve_url(url)

        parts = raw.split()
        if len(parts) >= 2:
            mt = self._map_media_type(parts[0])
            if mt:
                return {
                    "media_type": mt,
                    "id": parts[1],
                    "storefront": self.cfg.default_storefront,
                }

        if raw:
            return {
                "media_type": default_media_type,
                "id": raw,
                "storefront": self.cfg.default_storefront,
            }
        raise ServiceError("无法解析目标")

    @staticmethod
    def _map_media_type(raw: str) -> str:
        t = raw.strip().lower()
        mapping = {
            "song": "song",
            "songs": "song",
            "album": "album",
            "albums": "album",
            "playlist": "playlist",
            "playlists": "playlist",
            "station": "station",
            "stations": "station",
            "mv": "music-video",
            "mvs": "music-video",
            "music-video": "music-video",
            "musicvideo": "music-video",
            "artist": "artist",
            "artists": "artist",
        }
        return mapping.get(t, "")

    @staticmethod
    def _fallback_media_type(kind: str) -> str:
        if kind == "artist_mv":
            return "music-video"
        if kind in {"artist_album", "album"}:
            return "album"
        if kind == "artist":
            return "artist"
        return "song"

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        return event.unified_msg_origin

    def _spawn_task(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        
        def _on_done(done: asyncio.Task):
            self._tasks.discard(done)
            if done.cancelled():
                return
            try:
                exc = done.exception()
            except Exception:
                logger.error(traceback.format_exc())
                return
            if exc:
                logger.error(f"后台任务异常: {exc}")

        task.add_done_callback(_on_done)
