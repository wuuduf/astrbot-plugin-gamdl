from __future__ import annotations

import asyncio
import importlib
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from astrbot.api import logger

from .config import PluginConfig
from .models import ServiceError

URL_EXTRACT_RE = re.compile(
    r"https?://(?:music|beta\.music|classical\.music)\.apple\.com/[^\s]+",
    re.IGNORECASE,
)

CATALOG_URL_RE = re.compile(
    r"^https?://(?:music|beta\.music|classical\.music)\.apple\.com/"
    r"(?:(?P<storefront>[a-z]{2})/)?"
    r"(?P<type>artist|album|playlist|song|music-video|post|station)"
    r"(?:/[^/?#\s]+)?/"
    r"(?P<id>[^/?#\s]+)",
    re.IGNORECASE,
)

LIBRARY_URL_RE = re.compile(
    r"^https?://(?:music|beta\.music|classical\.music)\.apple\.com/"
    r"(?:(?P<storefront>[a-z]{2})/)?library/"
    r"(?P<type>playlist|albums)/(?P<id>[^/?#\s]+)",
    re.IGNORECASE,
)

MEDIA_EXT_KIND = {
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    ".gif": "image",
    ".mp4": "video",
    ".m4v": "video",
    ".mov": "video",
}


@dataclass(slots=True)
class _Job:
    job_id: str
    status: str
    request: dict[str, Any]
    created_at: float
    updated_at: float
    error: str = ""
    result: dict[str, Any] | None = None


class LocalAppleMusicBackend:
    def __init__(self, config: PluginConfig):
        self.cfg = config
        self._jobs: dict[str, _Job] = {}
        self._order: list[str] = []
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._seq = 0
        self._lock = asyncio.Lock()
        self._modules: dict[str, Any] | None = None
        self._module_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._workers:
            return
        for idx in range(max(1, self.cfg.max_concurrency)):
            task = asyncio.create_task(self._worker_loop(), name=f"amdl-worker-{idx}")
            self._workers.append(task)

    async def close(self) -> None:
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def health(self) -> dict[str, Any]:
        api = None
        itunes = None
        try:
            api, itunes, _modules = await self._create_api_clients(
                storefront=self.cfg.default_storefront
            )
            if not getattr(api, "active_subscription", False):
                raise ServiceError("Apple Music 账号未检测到有效订阅。")
            return {
                "ok": True,
                "storefront": getattr(api, "storefront", self.cfg.default_storefront),
            }
        finally:
            await self._close_clients(api, itunes)

    async def search(
        self,
        media_type: str,
        query: str,
        storefront: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        media_type = self._normalize_media_type(media_type)
        type_map = {
            "song": "songs",
            "album": "albums",
            "artist": "artists",
        }
        result_key = type_map.get(media_type)
        if not result_key:
            raise ServiceError("搜索类型仅支持 song/album/artist")

        api = None
        itunes = None
        try:
            api, itunes, _modules = await self._create_api_clients(storefront=storefront)
            payload = await api.get_search_results(
                term=query,
                types=result_key,
                limit=max(1, limit),
                offset=max(0, offset),
            )
            bucket = (
                payload.get("results", {}) if isinstance(payload, dict) else {}
            ).get(result_key, {})
            data = bucket.get("data", []) if isinstance(bucket, dict) else []
            items = [
                self._format_search_item(item, media_type)
                for item in data
                if isinstance(item, dict)
            ]
            has_next = bool(bucket.get("next")) if isinstance(bucket, dict) else False
            return {
                "items": items,
                "has_next": has_next,
                "storefront": getattr(api, "storefront", storefront or self.cfg.default_storefront),
            }
        finally:
            await self._close_clients(api, itunes)

    async def resolve_url(self, text_or_url: str) -> dict[str, Any]:
        url = self._extract_url(text_or_url)
        if not url:
            raise ServiceError("未识别到 Apple Music 链接")

        parsed = self._parse_url(url)
        if not parsed:
            raise ServiceError("链接类型未知")

        return {
            "target": {
                "media_type": parsed["media_type"],
                "id": parsed["id"],
                "storefront": parsed.get("storefront") or self.cfg.default_storefront,
                "raw_url": parsed["url"],
                "url": parsed["url"],
            }
        }

    async def artist_children(
        self,
        artist_id: str,
        relationship: str,
        storefront: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        rel = relationship.strip().lower()
        if rel not in {"albums", "music-videos"}:
            rel = "albums"

        api = None
        itunes = None
        try:
            api, itunes, _modules = await self._create_api_clients(storefront=storefront)
            response = await api.get_artist(
                artist_id,
                include="albums,music-videos",
                views="full-albums,compilation-albums,live-albums,singles,top-songs",
                limit=max(100, limit + offset + 20),
            )
            data = response.get("data", []) if isinstance(response, dict) else []
            if not data:
                return {
                    "items": [],
                    "has_next": False,
                }
            artist = data[0]
            relationships = artist.get("relationships", {}) if isinstance(artist, dict) else {}
            bucket = relationships.get(rel, {}) if isinstance(relationships, dict) else {}
            items = list(bucket.get("data", []) if isinstance(bucket, dict) else [])

            required = max(0, offset) + max(1, limit)
            if isinstance(bucket, dict) and bucket.get("next") and len(items) < required:
                try:
                    async for extra in api.extend_api_data(bucket):
                        if isinstance(extra, dict):
                            items.extend(extra.get("data", []) or [])
                        if len(items) >= required:
                            break
                except Exception:
                    logger.debug("extend artist children failed", exc_info=True)

            start = max(0, offset)
            end = start + max(1, limit)
            sliced = items[start:end]
            out = [
                self._format_search_item(item, "music-video" if rel == "music-videos" else "album")
                for item in sliced
                if isinstance(item, dict)
            ]
            has_next = len(items) > end
            return {
                "items": out,
                "has_next": has_next,
            }
        finally:
            await self._close_clients(api, itunes)

    async def download(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self._seq += 1
            job_id = f"job_{int(time.time() * 1000)}_{self._seq:06d}"
            now = time.time()
            self._jobs[job_id] = _Job(
                job_id=job_id,
                status="queued",
                request=dict(payload),
                created_at=now,
                updated_at=now,
            )
            self._order.append(job_id)
            self._prune_jobs_locked()
        await self._queue.put(job_id)
        return {
            "job_id": job_id,
            "queued": True,
        }

    async def job(self, job_id: str) -> dict[str, Any]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ServiceError("任务不存在")
            return self._job_to_dict(job)

    async def artwork(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = await self._resolve_target_from_payload(payload)
        animated = bool(payload.get("animated", False))

        api = None
        itunes = None
        try:
            api, itunes, modules = await self._create_api_clients(
                storefront=target["storefront"]
            )
            metadata = await self._fetch_media_metadata(api, target)
            display_name = self._media_display_name(metadata, fallback=target["id"])

            if animated:
                motion_url = self._extract_motion_url(metadata)
                if not motion_url:
                    raise ServiceError("该资源没有可用的动态封面")
                out_path = self.cfg.temp_dir / f"{self._safe_name(display_name)}-animated-{int(time.time() * 1000)}.mp4"
                await self._download_motion_video(motion_url, out_path)
                return {
                    "media_type": target["media_type"],
                    "media_id": target["id"],
                    "storefront": target["storefront"],
                    "display_name": display_name,
                    "animated": True,
                    "file": self._build_output_file(out_path, kind="video", temporary=True),
                }

            cover_url = self._extract_cover_url(metadata)
            if not cover_url:
                raise ServiceError("该资源没有可用封面")
            ext = ".png" if self.cfg.cover_format == "png" else ".jpg"
            out_path = self.cfg.temp_dir / f"{self._safe_name(display_name)}-cover-{int(time.time() * 1000)}{ext}"
            await self._download_to_file(cover_url, out_path)
            return {
                "media_type": target["media_type"],
                "media_id": target["id"],
                "storefront": target["storefront"],
                "display_name": display_name,
                "animated": False,
                "file": self._build_output_file(out_path, kind="image", temporary=True),
            }
        finally:
            await self._close_clients(api, itunes)

    async def lyrics(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = await self._resolve_target_from_payload(payload)
        media_type = self._normalize_media_type(target["media_type"])
        output_format = self._normalize_lyrics_format(str(payload.get("output_format", "lrc")))
        transfer_mode = self._normalize_transfer_mode(str(payload.get("transfer_mode", "one")))

        if media_type not in {"song", "album"}:
            raise ServiceError("歌词仅支持 song/album")

        api = None
        itunes = None
        try:
            api, itunes, modules = await self._create_api_clients(
                storefront=target["storefront"]
            )
            AppleMusicInterface = modules["AppleMusicInterface"]
            AppleMusicSongInterface = modules["AppleMusicSongInterface"]
            SyncedLyricsFormat = modules["SyncedLyricsFormat"]
            interface = AppleMusicInterface(api, itunes)
            song_interface = AppleMusicSongInterface(interface)
            lyrics_format_enum = self._to_synced_lyrics_enum(SyncedLyricsFormat, output_format)

            files: list[dict[str, Any]] = []
            failed_count = 0

            if media_type == "song":
                song_response = await api.get_song(str(target["id"]))
                data = song_response.get("data", []) if isinstance(song_response, dict) else []
                if not data:
                    raise ServiceError("歌曲不存在")
                song_meta = data[0]
                content = await self._extract_lyrics_content(
                    song_interface,
                    song_meta,
                    lyrics_format_enum,
                    output_format,
                )
                if not content:
                    raise ServiceError("该歌曲暂无歌词")
                base_name = self._media_display_name(song_meta, fallback=target["id"])
                out_path = self.cfg.temp_dir / f"{self._safe_name(base_name)}.lyrics.{output_format}"
                out_path.write_text(content, encoding="utf-8")
                files.append(self._build_output_file(out_path, temporary=True))
            else:
                album_response = await api.get_album(str(target["id"]))
                data = album_response.get("data", []) if isinstance(album_response, dict) else []
                if not data:
                    raise ServiceError("专辑不存在")
                album_meta = data[0]
                tracks = list(
                    (
                        album_meta.get("relationships", {})
                        .get("tracks", {})
                        .get("data", [])
                    )
                    or []
                )
                rel_tracks = (
                    album_meta.get("relationships", {}).get("tracks", {})
                    if isinstance(album_meta, dict)
                    else {}
                )
                if isinstance(rel_tracks, dict) and rel_tracks.get("next"):
                    try:
                        async for extra in api.extend_api_data(rel_tracks):
                            if isinstance(extra, dict):
                                tracks.extend(extra.get("data", []) or [])
                    except Exception:
                        logger.debug("extend album tracks failed", exc_info=True)

                album_name = self._media_display_name(album_meta, fallback=target["id"])
                album_dir = self.cfg.temp_dir / f"lyrics-album-{self._safe_name(album_name)}-{int(time.time() * 1000)}"
                album_dir.mkdir(parents=True, exist_ok=True)

                for idx, track in enumerate(tracks, start=1):
                    if not isinstance(track, dict):
                        continue
                    track_id = str(track.get("id", "")).strip()
                    if not track_id:
                        failed_count += 1
                        continue
                    try:
                        song_resp = await api.get_song(track_id)
                        song_data = song_resp.get("data", []) if isinstance(song_resp, dict) else []
                        if not song_data:
                            failed_count += 1
                            continue
                        song_meta = song_data[0]
                        content = await self._extract_lyrics_content(
                            song_interface,
                            song_meta,
                            lyrics_format_enum,
                            output_format,
                        )
                        if not content:
                            failed_count += 1
                            continue
                        order = int(
                            (
                                song_meta.get("attributes", {})
                                .get("trackNumber", idx)
                            )
                            or idx
                        )
                        track_name = self._media_display_name(song_meta, fallback=track_id)
                        out_path = album_dir / f"{order:02d}. {self._safe_name(track_name)}.lyrics.{output_format}"
                        out_path.write_text(content, encoding="utf-8")
                        files.append(self._build_output_file(out_path, temporary=True))
                    except Exception:
                        failed_count += 1

                if not files:
                    raise ServiceError("专辑没有可导出的歌词")

            zip_file: dict[str, Any] | None = None
            if transfer_mode == "zip" and files:
                zip_path, zip_name = self._create_zip_archive(
                    [Path(obj["path"]) for obj in files],
                    hint=f"{target['media_type']}-{target['id']}-lyrics",
                )
                zip_file = self._build_output_file(zip_path, kind="file", temporary=True)
                zip_file["name"] = zip_name

            return {
                "media_type": media_type,
                "media_id": target["id"],
                "storefront": target["storefront"],
                "format": output_format,
                "files": files,
                "zip_file": zip_file,
                "failed_count": failed_count,
            }
        finally:
            await self._close_clients(api, itunes)

    async def _worker_loop(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._set_job_status(job_id, "resolving")
                job = await self._get_job(job_id)
                result = await asyncio.wait_for(
                    self._execute_download(job.request, job_id=job_id),
                    timeout=max(60, self.cfg.job_timeout_seconds),
                )
                await self._set_job_completed(job_id, result)
            except asyncio.TimeoutError:
                await self._set_job_failed(job_id, "任务超时，请稍后重试。")
            except Exception as exc:
                await self._set_job_failed(job_id, str(exc))
            finally:
                self._queue.task_done()

    async def _execute_download(self, request: dict[str, Any], job_id: str) -> dict[str, Any]:
        target = await self._resolve_target_from_payload(request)
        media_type = self._normalize_media_type(target["media_type"])

        if media_type == "station":
            raise ServiceError("gamdl 当前不支持 station 下载")

        if media_type == "music-video" and not self.cfg.allow_music_video:
            raise ServiceError("当前配置已禁用 MV 下载")
        if media_type == "post" and not self.cfg.allow_post_video:
            raise ServiceError("当前配置已禁用 post 视频下载")

        transfer_mode = self._normalize_transfer_mode(str(request.get("transfer_mode", "one")))
        include_lyrics = bool(request.get("include_lyrics", False))
        include_cover = bool(request.get("include_cover", False))
        include_animated = bool(request.get("include_animated_cover", False))
        lyrics_format = self._normalize_lyrics_format(str(request.get("lyrics_format", "lrc")))

        await self._set_job_status(job_id, "downloading")

        if self.cfg.gamdl_invoke_mode == "subprocess":
            result = await self._download_with_subprocess(
                target=target,
                transfer_mode=transfer_mode,
                include_lyrics=include_lyrics,
                include_cover=include_cover,
                include_animated=include_animated,
                lyrics_format=lyrics_format,
            )
            await self._set_job_status(job_id, "packaging")
            return result

        try:
            result = await self._download_with_python(
                target=target,
                request=request,
                transfer_mode=transfer_mode,
                include_lyrics=include_lyrics,
                include_cover=include_cover,
                include_animated=include_animated,
                lyrics_format=lyrics_format,
            )
            await self._set_job_status(job_id, "packaging")
            return result
        except Exception:
            logger.exception("python download mode failed, fallback to subprocess")
            result = await self._download_with_subprocess(
                target=target,
                transfer_mode=transfer_mode,
                include_lyrics=include_lyrics,
                include_cover=include_cover,
                include_animated=include_animated,
                lyrics_format=lyrics_format,
            )
            await self._set_job_status(job_id, "packaging")
            return result

    async def _download_with_python(
        self,
        target: dict[str, Any],
        request: dict[str, Any],
        transfer_mode: str,
        include_lyrics: bool,
        include_cover: bool,
        include_animated: bool,
        lyrics_format: str,
    ) -> dict[str, Any]:
        modules = await self._load_modules()
        AppleMusicApi = modules["AppleMusicApi"]
        ItunesApi = modules["ItunesApi"]
        AppleMusicBaseDownloader = modules["AppleMusicBaseDownloader"]
        AppleMusicDownloader = modules["AppleMusicDownloader"]
        AppleMusicMusicVideoDownloader = modules["AppleMusicMusicVideoDownloader"]
        AppleMusicSongDownloader = modules["AppleMusicSongDownloader"]
        AppleMusicUploadedVideoDownloader = modules["AppleMusicUploadedVideoDownloader"]
        ArtistAutoSelect = modules["ArtistAutoSelect"]
        AppleMusicInterface = modules["AppleMusicInterface"]
        AppleMusicMusicVideoInterface = modules["AppleMusicMusicVideoInterface"]
        AppleMusicSongInterface = modules["AppleMusicSongInterface"]
        AppleMusicUploadedVideoInterface = modules["AppleMusicUploadedVideoInterface"]
        SongCodec = modules["SongCodec"]
        SyncedLyricsFormat = modules["SyncedLyricsFormat"]
        CoverFormat = modules["CoverFormat"]

        apple_music_api = None
        itunes_api = None
        errors: list[str] = []

        try:
            if self.cfg.use_wrapper:
                apple_music_api = await AppleMusicApi.create_from_wrapper(
                    wrapper_account_url=self.cfg.wrapper_account_url,
                    language=self.cfg.language,
                )
            else:
                if not self.cfg.cookies_path.exists():
                    raise ServiceError(f"cookies 文件不存在: {self.cfg.cookies_path}")
                apple_music_api = await AppleMusicApi.create_from_netscape_cookies(
                    cookies_path=str(self.cfg.cookies_path),
                    language=self.cfg.language,
                )

            if not getattr(apple_music_api, "active_subscription", False):
                raise ServiceError("Apple Music 账号未检测到有效订阅")

            if target["storefront"]:
                apple_music_api.storefront = target["storefront"]

            itunes_api = ItunesApi(apple_music_api.storefront, apple_music_api.language)
            interface = AppleMusicInterface(apple_music_api, itunes_api)
            song_interface = AppleMusicSongInterface(interface)
            music_video_interface = AppleMusicMusicVideoInterface(interface)
            uploaded_video_interface = AppleMusicUploadedVideoInterface(interface)

            cover_enum = CoverFormat.PNG if self.cfg.cover_format == "png" else CoverFormat.JPG
            cover_size = self._parse_cover_size(self.cfg.cover_size)

            base_downloader = AppleMusicBaseDownloader(
                output_path=str(self.cfg.download_dir),
                temp_path=str(self.cfg.temp_dir),
                use_wrapper=self.cfg.use_wrapper,
                wrapper_decrypt_ip=self.cfg.wrapper_decrypt_ip,
                save_cover=include_cover,
                cover_size=cover_size,
                cover_format=cover_enum,
                silent=True,
            )

            codec_priority = self._resolve_song_codec_priority(
                SongCodec,
                quality=str(request.get("quality", "alac")),
                aac_type=str(request.get("aac_type", "aac-lc")),
            )

            lyrics_enum = self._to_synced_lyrics_enum(SyncedLyricsFormat, lyrics_format)

            song_downloader = AppleMusicSongDownloader(
                base_downloader=base_downloader,
                interface=song_interface,
                codec_priority=codec_priority,
                synced_lyrics_format=lyrics_enum,
                no_synced_lyrics=not include_lyrics,
            )

            music_video_downloader = (
                AppleMusicMusicVideoDownloader(
                    base_downloader=base_downloader,
                    interface=music_video_interface,
                )
                if self.cfg.allow_music_video
                else None
            )
            uploaded_video_downloader = (
                AppleMusicUploadedVideoDownloader(
                    base_downloader=base_downloader,
                    interface=uploaded_video_interface,
                )
                if self.cfg.allow_post_video
                else None
            )

            try:
                artist_auto_select = ArtistAutoSelect(self.cfg.artist_auto_select)
            except Exception:
                artist_auto_select = ArtistAutoSelect.ALL_ALBUMS

            downloader = AppleMusicDownloader(
                interface=interface,
                base_downloader=base_downloader,
                song_downloader=song_downloader,
                music_video_downloader=music_video_downloader,
                uploaded_video_downloader=uploaded_video_downloader,
                artist_auto_select=artist_auto_select,
            )

            queue = await self._build_download_queue(downloader, target)
            if not queue:
                raise ServiceError("没有可下载内容")

            collected: list[dict[str, Any]] = []
            for item in queue:
                media_meta = getattr(item, "media_metadata", {}) or {}
                media_kind = self._normalize_media_type(str(media_meta.get("type", "")))
                if media_kind == "music-video" and not self.cfg.allow_music_video:
                    continue
                if media_kind == "post" and not self.cfg.allow_post_video:
                    continue

                title = (
                    media_meta.get("attributes", {})
                    .get("name", "Unknown")
                    if isinstance(media_meta, dict)
                    else "Unknown"
                )

                final_path = Path(str(getattr(item, "final_path", "") or "")).expanduser()
                exists_before = final_path.exists() and final_path.is_file()
                if not exists_before:
                    try:
                        await downloader.download(item)
                    except Exception as exc:
                        errors.append(f"{title}: {exc}")
                        continue

                if not final_path.exists() or not final_path.is_file():
                    errors.append(f"{title}: 输出文件不存在")
                    continue

                entry = {
                    "path": final_path.resolve(),
                    "track_id": self._resolve_media_id(media_meta),
                    "title": title,
                    "performer": str(
                        (media_meta.get("attributes", {}) or {}).get("artistName", "")
                    ),
                    "duration_millis": int(
                        ((media_meta.get("attributes", {}) or {}).get("durationInMillis", 0))
                        or 0
                    ),
                    "extras": [],
                }

                if include_cover:
                    cover_path = Path(str(getattr(item, "cover_path", "") or "")).expanduser()
                    if cover_path.exists() and cover_path.is_file():
                        entry["extras"].append(cover_path.resolve())

                if include_lyrics:
                    lyrics_path = Path(str(getattr(item, "synced_lyrics_path", "") or "")).expanduser()
                    if lyrics_path.exists() and lyrics_path.is_file():
                        entry["extras"].append(lyrics_path.resolve())

                if include_animated:
                    for name in ("square_animated_artwork.mp4", "tall_animated_artwork.mp4"):
                        p = final_path.parent / name
                        if p.exists() and p.is_file():
                            entry["extras"].append(p.resolve())

                collected.append(entry)

            if not collected:
                detail = "\n".join(errors[:3]) if errors else "无可用输出"
                raise ServiceError(f"下载失败: {detail}")

            return self._build_download_result(
                target=target,
                transfer_mode=transfer_mode,
                collected=collected,
                errors=errors,
            )
        finally:
            await self._close_clients(apple_music_api, itunes_api)

    async def _download_with_subprocess(
        self,
        target: dict[str, Any],
        transfer_mode: str,
        include_lyrics: bool,
        include_cover: bool,
        include_animated: bool,
        lyrics_format: str,
    ) -> dict[str, Any]:
        before = self._list_output_files(self.cfg.download_dir)
        url = target.get("url") or self._build_fallback_url(target)
        if not url:
            raise ServiceError("subprocess 模式需要可解析链接")

        cmd = self._build_subprocess_cmd(url)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=max(60, self.cfg.job_timeout_seconds),
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise ServiceError("下载超时")

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="ignore").strip()
            raise ServiceError(f"gamdl 执行失败: {err or f'code={proc.returncode}'}")

        logger.debug("gamdl stdout: %s", stdout.decode("utf-8", errors="ignore"))
        after = self._list_output_files(self.cfg.download_dir)
        new_files = sorted(after - before)
        if not new_files:
            raise ServiceError("下载完成但未检测到新文件")

        collected: list[dict[str, Any]] = []
        for path in new_files:
            extras: list[Path] = []
            if include_lyrics:
                lyric_path = path.with_suffix(f".{lyrics_format}")
                if lyric_path.exists() and lyric_path.is_file():
                    extras.append(lyric_path.resolve())
            if include_cover:
                for name in ("Cover.jpg", "Cover.jpeg", "Cover.png"):
                    p = path.parent / name
                    if p.exists() and p.is_file():
                        extras.append(p.resolve())
                        break
            if include_animated:
                for name in ("square_animated_artwork.mp4", "tall_animated_artwork.mp4"):
                    p = path.parent / name
                    if p.exists() and p.is_file():
                        extras.append(p.resolve())
            collected.append(
                {
                    "path": path.resolve(),
                    "track_id": target["id"],
                    "title": path.stem,
                    "performer": "",
                    "duration_millis": 0,
                    "extras": extras,
                }
            )

        return self._build_download_result(
            target=target,
            transfer_mode=transfer_mode,
            collected=collected,
            errors=[],
        )

    async def _build_download_queue(self, downloader: Any, target: dict[str, Any]) -> list[Any]:
        media_type = self._normalize_media_type(target["media_type"])
        media_id = str(target["id"])

        is_library = media_type.startswith("library-")
        if is_library:
            media_type = media_type.removeprefix("library-")

        # Prefer direct queue build by media_type/id to avoid URL canonicalization dependency.
        if hasattr(downloader, "_get_download_queue"):
            queue = await downloader._get_download_queue(media_type, media_id, is_library)
            if queue:
                return list(queue)

        url = target.get("url") or self._build_fallback_url(target)
        if not url:
            return []
        url_info = downloader.get_url_info(url)
        if not url_info:
            return []
        queue = await downloader.get_download_queue(url_info)
        return list(queue or [])

    def _build_download_result(
        self,
        target: dict[str, Any],
        transfer_mode: str,
        collected: list[dict[str, Any]],
        errors: list[str],
    ) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        dedup: set[str] = set()

        for entry in collected:
            media_path = Path(entry["path"]).resolve()
            key = str(media_path)
            if key not in dedup:
                dedup.add(key)
                files.append(
                    self._build_output_file(
                        media_path,
                        kind=self._path_kind(media_path),
                        track_id=str(entry.get("track_id", "")),
                        title=str(entry.get("title", "")),
                        performer=str(entry.get("performer", "")),
                        duration_millis=int(entry.get("duration_millis", 0) or 0),
                    )
                )

            for extra in entry.get("extras", []):
                extra_path = Path(extra).resolve()
                extra_key = str(extra_path)
                if extra_key in dedup:
                    continue
                if not extra_path.exists() or not extra_path.is_file():
                    continue
                dedup.add(extra_key)
                files.append(
                    self._build_output_file(
                        extra_path,
                        kind=self._path_kind(extra_path),
                    )
                )

        zip_file: dict[str, Any] | None = None
        if transfer_mode == "zip" and files:
            zip_path, zip_name = self._create_zip_archive(
                [Path(item["path"]) for item in files],
                hint=f"{target['media_type']}-{target['id']}",
            )
            zip_file = self._build_output_file(zip_path, kind="file", temporary=True)
            zip_file["name"] = zip_name

        result = {
            "media_type": target["media_type"],
            "media_id": target["id"],
            "storefront": target["storefront"],
            "transfer_mode": transfer_mode,
            "files": files,
            "zip_file": zip_file,
        }
        if errors:
            result["errors"] = errors[:10]
        return result

    async def _resolve_target_from_payload(self, payload: dict[str, Any]) -> dict[str, str]:
        media_type = self._normalize_media_type(str(payload.get("media_type", "")))
        media_id = str(payload.get("id", "")).strip()
        storefront = str(payload.get("storefront", "") or self.cfg.default_storefront).strip().lower() or self.cfg.default_storefront
        raw_url = str(payload.get("url", "")).strip()

        if raw_url:
            parsed = self._parse_url(raw_url)
            if parsed:
                media_type = media_type or parsed["media_type"]
                media_id = media_id or parsed["id"]
                storefront = parsed.get("storefront") or storefront
                raw_url = parsed.get("url") or raw_url

        if not media_type:
            raise ServiceError("缺少 media_type")
        if not media_id and media_type not in {"station"}:
            raise ServiceError("缺少资源 ID")

        return {
            "media_type": media_type,
            "id": media_id,
            "storefront": storefront,
            "url": raw_url,
        }

    async def _fetch_media_metadata(self, api: Any, target: dict[str, str]) -> dict[str, Any]:
        media_type = self._normalize_media_type(target["media_type"])
        media_id = target["id"]

        if media_type == "song":
            response = await api.get_song(media_id)
        elif media_type == "album":
            response = await api.get_album(media_id)
        elif media_type == "playlist":
            response = await api.get_playlist(media_id)
        elif media_type == "artist":
            response = await api.get_artist(media_id)
        elif media_type == "music-video":
            response = await api.get_music_video(media_id)
        elif media_type == "post":
            response = await api.get_uploaded_video(media_id)
        elif media_type == "library-album":
            response = await api.get_library_album(media_id)
        elif media_type == "library-playlist":
            response = await api.get_library_playlist(media_id)
        else:
            raise ServiceError(f"暂不支持该类型: {media_type}")

        if not isinstance(response, dict):
            raise ServiceError("Apple API 返回异常")
        data = response.get("data", [])
        if not data:
            raise ServiceError("资源不存在或不可访问")
        meta = data[0]
        if not isinstance(meta, dict):
            raise ServiceError("资源元数据格式异常")
        return meta

    async def _create_api_clients(
        self,
        storefront: str | None = None,
    ) -> tuple[Any, Any, dict[str, Any]]:
        modules = await self._load_modules()
        AppleMusicApi = modules["AppleMusicApi"]
        ItunesApi = modules["ItunesApi"]

        if self.cfg.use_wrapper:
            api = await AppleMusicApi.create_from_wrapper(
                wrapper_account_url=self.cfg.wrapper_account_url,
                language=self.cfg.language,
            )
        else:
            if not self.cfg.cookies_path.exists():
                raise ServiceError(f"cookies 文件不存在: {self.cfg.cookies_path}")
            api = await AppleMusicApi.create_from_netscape_cookies(
                cookies_path=str(self.cfg.cookies_path),
                language=self.cfg.language,
            )

        if storefront:
            api.storefront = storefront.lower().strip()

        itunes = ItunesApi(api.storefront, api.language)
        return api, itunes, modules

    async def _load_modules(self) -> dict[str, Any]:
        if self._modules is not None:
            return self._modules

        async with self._module_lock:
            if self._modules is not None:
                return self._modules

            self._prepend_import_path()
            try:
                api_mod = importlib.import_module("gamdl.api")
                downloader_mod = importlib.import_module("gamdl.downloader")
                interface_mod = importlib.import_module("gamdl.interface")
            except Exception as exc:
                raise ServiceError(
                    "无法导入 gamdl，请确认 requirements 已安装（pip install -r requirements.txt）。"
                ) from exc

            self._modules = {
                "AppleMusicApi": api_mod.AppleMusicApi,
                "ItunesApi": api_mod.ItunesApi,
                "AppleMusicBaseDownloader": downloader_mod.AppleMusicBaseDownloader,
                "AppleMusicDownloader": downloader_mod.AppleMusicDownloader,
                "AppleMusicMusicVideoDownloader": downloader_mod.AppleMusicMusicVideoDownloader,
                "AppleMusicSongDownloader": downloader_mod.AppleMusicSongDownloader,
                "AppleMusicUploadedVideoDownloader": downloader_mod.AppleMusicUploadedVideoDownloader,
                "ArtistAutoSelect": downloader_mod.ArtistAutoSelect,
                "AppleMusicInterface": interface_mod.AppleMusicInterface,
                "AppleMusicMusicVideoInterface": interface_mod.AppleMusicMusicVideoInterface,
                "AppleMusicSongInterface": interface_mod.AppleMusicSongInterface,
                "AppleMusicUploadedVideoInterface": interface_mod.AppleMusicUploadedVideoInterface,
                "SongCodec": interface_mod.SongCodec,
                "SyncedLyricsFormat": interface_mod.SyncedLyricsFormat,
                "CoverFormat": interface_mod.CoverFormat,
            }
            return self._modules

    def _prepend_import_path(self) -> None:
        if self.cfg.gamdl_python_path:
            path = Path(self.cfg.gamdl_python_path).expanduser().resolve()
            if path.exists():
                path_text = str(path)
                if path_text not in sys.path:
                    sys.path.insert(0, path_text)

    @staticmethod
    async def _close_clients(apple_music_api: Any, itunes_api: Any) -> None:
        if apple_music_api is not None:
            client = getattr(apple_music_api, "client", None)
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    logger.debug("failed to close AppleMusicApi client", exc_info=True)
        if itunes_api is not None:
            client = getattr(itunes_api, "client", None)
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    logger.debug("failed to close ItunesApi client", exc_info=True)

    async def _set_job_status(self, job_id: str, status: str) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = status
            job.updated_at = time.time()

    async def _set_job_failed(self, job_id: str, message: str) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = "failed"
            job.error = str(message or "任务失败")
            job.updated_at = time.time()

    async def _set_job_completed(self, job_id: str, result: dict[str, Any]) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = "completed"
            job.result = result
            job.error = ""
            job.updated_at = time.time()

    async def _get_job(self, job_id: str) -> _Job:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ServiceError("任务不存在")
            return job

    def _job_to_dict(self, job: _Job) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "status": job.status,
            "error": job.error,
            "result": job.result,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }

    def _prune_jobs_locked(self) -> None:
        max_jobs = 300
        while len(self._order) > max_jobs:
            old = self._order.pop(0)
            self._jobs.pop(old, None)

    def _format_search_item(self, item: dict[str, Any], fallback_type: str) -> dict[str, Any]:
        attrs = item.get("attributes", {}) if isinstance(item, dict) else {}
        media_type = self._normalize_media_type(str(item.get("type", fallback_type)))
        detail = ""
        if media_type == "song":
            detail = f"{attrs.get('albumName', '')}".strip()
        elif media_type == "album":
            track_count = attrs.get("trackCount")
            if track_count:
                detail = f"{track_count} tracks"

        return {
            "media_type": media_type,
            "id": str(item.get("id", "") or ""),
            "name": str(attrs.get("name", "") or ""),
            "artist": str(attrs.get("artistName", "") or ""),
            "album": str(attrs.get("albumName", "") or ""),
            "detail": detail,
            "url": str(attrs.get("url", "") or ""),
            "content_rating": str(attrs.get("contentRating", "") or ""),
        }

    def _extract_url(self, text: str) -> str:
        match = URL_EXTRACT_RE.search(text or "")
        if not match:
            return ""
        url = match.group(0).strip()
        return url.rstrip(").,，。！？!?")

    def _parse_url(self, url: str) -> dict[str, str] | None:
        url = (url or "").strip()
        if not url:
            return None

        catalog = CATALOG_URL_RE.match(url)
        if catalog:
            groups = catalog.groupdict()
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            sub_id = (query.get("i") or [""])[0].strip()
            media_type = groups.get("type", "").lower().strip()
            media_id = groups.get("id", "").strip()
            if sub_id:
                media_type = "song"
                media_id = sub_id
            storefront = (groups.get("storefront") or self.cfg.default_storefront).lower()
            return {
                "media_type": self._normalize_media_type(media_type),
                "id": media_id,
                "storefront": storefront,
                "url": url,
            }

        library = LIBRARY_URL_RE.match(url)
        if library:
            groups = library.groupdict()
            storefront = (groups.get("storefront") or self.cfg.default_storefront).lower()
            library_type = groups.get("type", "").lower()
            media_type = "library-playlist" if library_type == "playlist" else "library-album"
            return {
                "media_type": media_type,
                "id": groups.get("id", "").strip(),
                "storefront": storefront,
                "url": url,
            }

        return None

    def _normalize_media_type(self, media_type: str) -> str:
        t = (media_type or "").strip().lower()
        mapping = {
            "songs": "song",
            "song": "song",
            "albums": "album",
            "album": "album",
            "artists": "artist",
            "artist": "artist",
            "playlists": "playlist",
            "playlist": "playlist",
            "music-videos": "music-video",
            "music-video": "music-video",
            "musicvideo": "music-video",
            "mv": "music-video",
            "post": "post",
            "uploaded-videos": "post",
            "station": "station",
            "stations": "station",
            "library-playlists": "library-playlist",
            "library-playlist": "library-playlist",
            "library-albums": "library-album",
            "library-album": "library-album",
        }
        return mapping.get(t, t)

    @staticmethod
    def _normalize_transfer_mode(raw: str) -> str:
        return "zip" if (raw or "").strip().lower() == "zip" else "one"

    @staticmethod
    def _normalize_lyrics_format(raw: str) -> str:
        fmt = (raw or "").strip().lower()
        if fmt in {"lrc", "ttml", "srt"}:
            return fmt
        return "lrc"

    def _resolve_song_codec_priority(
        self,
        SongCodec: Any,
        quality: str,
        aac_type: str,
    ) -> list[Any]:
        quality = (quality or "").strip().lower()
        aac_type = (aac_type or "").strip().lower()

        if quality == "alac":
            names = ["alac", "aac-legacy"]
        elif quality == "atmos":
            names = ["atmos", "alac", "aac-legacy"]
        elif quality == "aac":
            aac_map = {
                "aac": "aac-legacy",
                "aac-lc": "aac",
                "aac-binaural": "aac-binaural",
                "aac-downmix": "aac-downmix",
            }
            names = [aac_map.get(aac_type, "aac"), "aac-legacy"]
        elif quality == "flac":
            # gamdl 当前直接输出 m4a，flac 在本插件中回退为 ALAC。
            names = ["alac", "aac-legacy"]
        else:
            names = self.cfg.song_codec_priority or ["alac", "aac-legacy"]

        out = []
        for name in names:
            try:
                out.append(SongCodec(name))
            except Exception:
                continue
        if not out:
            out = [SongCodec.AAC_LEGACY]
        return out

    @staticmethod
    def _to_synced_lyrics_enum(SyncedLyricsFormat: Any, output_format: str) -> Any:
        fmt = (output_format or "lrc").lower()
        if fmt == "ttml":
            return SyncedLyricsFormat.TTML
        if fmt == "srt":
            return SyncedLyricsFormat.SRT
        return SyncedLyricsFormat.LRC

    async def _extract_lyrics_content(
        self,
        song_interface: Any,
        song_meta: dict[str, Any],
        lyrics_format_enum: Any,
        output_format: str,
    ) -> str:
        lyrics_obj = await song_interface.get_lyrics(song_meta, lyrics_format_enum)
        if not lyrics_obj:
            return ""
        if output_format in {"lrc", "ttml", "srt"}:
            return str(getattr(lyrics_obj, "synced", "") or "").strip()
        return str(getattr(lyrics_obj, "unsynced", "") or "").strip()

    def _extract_cover_url(self, metadata: dict[str, Any]) -> str:
        attrs = metadata.get("attributes", {}) if isinstance(metadata, dict) else {}
        artwork = attrs.get("artwork", {}) if isinstance(attrs, dict) else {}
        template = str(artwork.get("url", "") or "").strip()
        if not template:
            return ""

        width, height = self._parse_cover_wh(self.cfg.cover_size)
        url = template.replace("{w}", str(width)).replace("{h}", str(height))
        if "{f}" in url:
            url = url.replace("{f}", self.cfg.cover_format)
        if self.cfg.cover_format == "png":
            url = re.sub(r"\.jpg(?=$|\?)", ".png", url, flags=re.IGNORECASE)
        return url

    def _extract_motion_url(self, metadata: dict[str, Any]) -> str:
        attrs = metadata.get("attributes", {}) if isinstance(metadata, dict) else {}
        editorial = attrs.get("editorialVideo", {}) if isinstance(attrs, dict) else {}
        if not isinstance(editorial, dict):
            return ""

        candidates = [
            self._dig_video_url(editorial, ["motionDetailSquare", "video"]),
            self._dig_video_url(editorial, ["motionSquareVideo1x1", "video"]),
            self._dig_video_url(editorial, ["motionDetailTall", "video"]),
            self._dig_video_url(editorial, ["motionTallVideo3x4", "video"]),
        ]
        for candidate in candidates:
            if candidate:
                return candidate
        return ""

    def _dig_video_url(self, obj: Any, path: list[str]) -> str:
        current = obj
        for key in path:
            if not isinstance(current, dict):
                return ""
            current = current.get(key)
        return self._pick_url(current)

    def _pick_url(self, value: Any) -> str:
        if isinstance(value, str):
            text = value.strip()
            return text if text.startswith("http") else ""
        if isinstance(value, dict):
            direct = value.get("url")
            if isinstance(direct, str) and direct.strip().startswith("http"):
                return direct.strip()
            for nested in value.values():
                found = self._pick_url(nested)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._pick_url(item)
                if found:
                    return found
        return ""

    async def _download_to_file(self, url: str, out_path: Path) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            try:
                response = await client.get(url)
            except Exception as exc:
                raise ServiceError(f"下载文件失败: {exc}") from exc
        if response.status_code >= 400:
            raise ServiceError(f"下载文件失败: HTTP {response.status_code}")
        out_path.write_bytes(response.content)

    async def _download_motion_video(self, motion_url: str, out_path: Path) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ServiceError("动态封面需要 ffmpeg，请先安装后重试")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-loglevel",
            "error",
            "-y",
            "-i",
            motion_url,
            "-c",
            "copy",
            str(out_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="ignore").strip()
            raise ServiceError(f"下载动态封面失败: {err or 'ffmpeg error'}")
        if not out_path.exists() or not out_path.is_file():
            text = stdout.decode("utf-8", errors="ignore").strip()
            raise ServiceError(f"下载动态封面失败: 输出文件不存在 {text}")

    def _build_output_file(
        self,
        path: Path,
        kind: str | None = None,
        temporary: bool = False,
        track_id: str = "",
        title: str = "",
        performer: str = "",
        duration_millis: int = 0,
    ) -> dict[str, Any]:
        path = path.resolve()
        stat = path.stat()
        return {
            "path": str(path),
            "name": path.name,
            "size": int(stat.st_size),
            "kind": kind or self._path_kind(path),
            "track_id": track_id,
            "title": title,
            "performer": performer,
            "duration_millis": int(duration_millis or 0),
            "temporary": bool(temporary),
        }

    def _path_kind(self, path: Path) -> str:
        return MEDIA_EXT_KIND.get(path.suffix.lower(), "file")

    def _create_zip_archive(self, paths: list[Path], hint: str) -> tuple[Path, str]:
        valid = [p.resolve() for p in paths if p.exists() and p.is_file()]
        if not valid:
            raise ServiceError("没有可打包的文件")

        safe_hint = self._safe_name(hint) or "bundle"
        display_name = f"{safe_hint}.zip"
        zip_path = self.cfg.temp_dir / f"{safe_hint}-{int(time.time() * 1000)}.zip"
        base_dir = self._common_parent(valid)

        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for file_path in valid:
                    if base_dir is not None:
                        try:
                            arcname = file_path.relative_to(base_dir)
                        except Exception:
                            arcname = file_path.name
                    else:
                        arcname = file_path.name
                    archive.write(file_path, arcname=str(arcname))
        except OSError as exc:
            raise ServiceError(f"创建 ZIP 失败: {exc}") from exc

        return zip_path.resolve(), display_name

    def _common_parent(self, paths: list[Path]) -> Path | None:
        if not paths:
            return None
        common = paths[0].parent
        for path in paths[1:]:
            while common != path and common not in path.parents:
                if common == common.parent:
                    return common
                common = common.parent
        return common

    def _build_subprocess_cmd(self, url: str) -> list[str]:
        cmd = [
            self.cfg.gamdl_executable,
            "--output-path",
            str(self.cfg.download_dir),
            "--temp-path",
            str(self.cfg.temp_dir),
            "--artist-auto-select",
            self.cfg.artist_auto_select,
            "--log-level",
            "ERROR",
            "--language",
            self.cfg.language,
            "--song-codec-priority",
            ",".join(self.cfg.song_codec_priority),
        ]
        if self.cfg.use_wrapper:
            cmd.extend([
                "--use-wrapper",
                "--wrapper-account-url",
                self.cfg.wrapper_account_url,
                "--wrapper-decrypt-ip",
                self.cfg.wrapper_decrypt_ip,
            ])
        else:
            cmd.extend([
                "--cookies-path",
                str(self.cfg.cookies_path),
            ])
        cmd.append(url)
        return cmd

    def _list_output_files(self, root: Path) -> set[Path]:
        if not root.exists():
            return set()
        out: set[Path] = set()
        for path in root.rglob("*"):
            if path.is_file():
                out.add(path.resolve())
        return out

    def _build_fallback_url(self, target: dict[str, str]) -> str:
        storefront = target.get("storefront") or self.cfg.default_storefront
        media_type = self._normalize_media_type(target.get("media_type", ""))
        media_id = target.get("id", "")
        if not media_type or not media_id:
            return ""
        type_map = {
            "song": "song",
            "album": "album",
            "artist": "artist",
            "playlist": "playlist",
            "music-video": "music-video",
            "post": "post",
            "station": "station",
            "library-playlist": "playlist",
            "library-album": "album",
        }
        slug_type = type_map.get(media_type)
        if not slug_type:
            return ""
        return f"https://music.apple.com/{storefront}/{slug_type}/{media_id}"

    def _resolve_media_id(self, media_metadata: dict[str, Any]) -> str:
        attrs = media_metadata.get("attributes", {}) if isinstance(media_metadata, dict) else {}
        play_params = attrs.get("playParams", {}) if isinstance(attrs, dict) else {}
        media_id = (
            play_params.get("catalogId")
            or play_params.get("id")
            or media_metadata.get("id")
            or ""
        )
        return str(media_id)

    def _media_display_name(self, metadata: dict[str, Any], fallback: str) -> str:
        attrs = metadata.get("attributes", {}) if isinstance(metadata, dict) else {}
        name = str(attrs.get("name", "") or "").strip()
        return name or fallback

    @staticmethod
    def _safe_name(text: str) -> str:
        raw = (text or "").strip()
        if not raw:
            return "item"
        sanitized = re.sub(r"[\\/:*?\"<>|]", "_", raw)
        sanitized = re.sub(r"\s+", " ", sanitized).strip()
        return sanitized[:180] or "item"

    @staticmethod
    def _parse_cover_wh(raw: str) -> tuple[int, int]:
        text = (raw or "").lower().strip()
        if "x" in text:
            left, right = text.split("x", 1)
            try:
                w = max(256, int(left.strip()))
                h = max(256, int(right.strip()))
                return w, h
            except Exception:
                pass
        try:
            size = max(256, int(text))
            return size, size
        except Exception:
            return 3000, 3000

    def _parse_cover_size(self, raw: str) -> int:
        w, _h = self._parse_cover_wh(raw)
        return w
