from __future__ import annotations

from .models import SearchItem, SessionSettings


class Renderer:
    @staticmethod
    def _format_elapsed(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}s"
        m, s = divmod(seconds, 60)
        if m < 60:
            return f"{m}m{s:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m{s:02d}s"

    @staticmethod
    def help_text() -> str:
        return (
            "Apple Music 命令列表:\n"
            "- am help: 查看本帮助\n"
            "- am 搜歌 <关键词>: 搜索歌曲并可序号选择\n"
            "- am 搜专 <关键词>: 搜索专辑并可序号选择\n"
            "- am 搜人 <关键词>: 搜索艺人并进入专辑/MV二级选择\n"
            "- am 链接 <apple music url>: 解析链接并下载\n"
            "- am 歌词 <song-url|song-id|album-url|album-id>: 导出歌词\n"
            "- am 封面 <url|type id>: 导出封面\n"
            "- am 动态封面 <url|type id>: 导出动态封面\n"
            "- am 设置: 查看当前会话设置\n"
            "- am 设置 <值>: 修改会话设置（如 zip / flac / 歌词关）\n\n"
            "搜索后可回复: 1 / 1 zip / 1 歌词 / 1 封面 / 1 动态封面 / 专辑 / mv"
        )

    @staticmethod
    def render_settings(settings: SessionSettings) -> str:
        transfer = "ZIP" if settings.transfer_mode == "zip" else "逐个发送"
        auto_lyrics = "开" if settings.include_lyrics else "关"
        auto_cover = "开" if settings.include_cover else "关"
        auto_animated = "开" if settings.include_animated_cover else "关"
        return (
            "当前会话下载设置:\n"
            f"- 音质: {settings.quality.upper()}\n"
            f"- AAC 模式: {settings.aac_type}\n"
            f"- MV 音轨: {settings.mv_audio_type}\n"
            f"- 歌词格式: {settings.lyrics_format.upper()}\n"
            f"- 自动附带歌词: {auto_lyrics}\n"
            f"- 自动附带封面: {auto_cover}\n"
            f"- 自动附带动态封面: {auto_animated}\n"
            f"- 传输模式: {transfer}"
        )

    @staticmethod
    def render_search(kind: str, query: str, items: list[SearchItem]) -> str:
        title_map = {
            "song": "歌曲",
            "album": "专辑",
            "artist": "艺人",
            "artist_album": "艺人专辑",
            "artist_mv": "艺人 MV",
        }
        title = title_map.get(kind, kind)
        lines = [f"{title}搜索结果: {query}"]
        for idx, item in enumerate(items, start=1):
            main = item.name
            if item.artist and item.album:
                main = f"{item.name} - {item.artist} / {item.album}"
            elif item.artist:
                main = f"{item.name} - {item.artist}"
            elif item.detail:
                main = f"{item.name} - {item.detail}"
            lines.append(f"{idx}. {main}")
        lines.append("回复序号选择，例如: 1 或 1 zip")
        if kind == "artist":
            lines.append("艺人可用: 1 专辑 / 1 mv")
        return "\n".join(lines)

    @staticmethod
    def render_job_queued(job_id: str) -> str:
        return f"下载任务已创建，job_id={job_id}，完成后将主动推送。"

    def render_job_progress(self, job_id: str, status: str, elapsed_sec: int) -> str:
        status_text = status or "running"
        elapsed = self._format_elapsed(max(0, int(elapsed_sec)))
        return f"任务进行中 (job_id={job_id})，状态={status_text}，已耗时 {elapsed}。"

    @staticmethod
    def render_job_failed(job_id: str, reason: str) -> str:
        return f"任务失败 (job_id={job_id}): {reason}"

    @staticmethod
    def render_job_done(job_id: str, sent_count: int) -> str:
        return f"任务完成 (job_id={job_id})，已发送 {sent_count} 个文件。"
