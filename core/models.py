from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ServiceError(Exception):
    """Service request failed."""


@dataclass(slots=True)
class OutputFile:
    path: str
    name: str
    size: int
    kind: str
    track_id: str = ""
    title: str = ""
    performer: str = ""
    duration_millis: int = 0
    temporary: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OutputFile":
        return cls(
            path=str(data.get("path", "")),
            name=str(data.get("name", "")),
            size=int(data.get("size", 0) or 0),
            kind=str(data.get("kind", "file") or "file"),
            track_id=str(data.get("track_id", "") or ""),
            title=str(data.get("title", "") or ""),
            performer=str(data.get("performer", "") or ""),
            duration_millis=int(data.get("duration_millis", 0) or 0),
            temporary=bool(data.get("temporary", False)),
        )


@dataclass(slots=True)
class SearchItem:
    media_type: str
    item_id: str
    name: str
    artist: str = ""
    album: str = ""
    detail: str = ""
    url: str = ""
    content_rating: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchItem":
        return cls(
            media_type=str(data.get("media_type", "") or ""),
            item_id=str(data.get("id", "") or ""),
            name=str(data.get("name", "") or ""),
            artist=str(data.get("artist", "") or ""),
            album=str(data.get("album", "") or ""),
            detail=str(data.get("detail", "") or ""),
            url=str(data.get("url", "") or ""),
            content_rating=str(data.get("content_rating", "") or ""),
        )


@dataclass(slots=True)
class DownloadResult:
    media_type: str
    media_id: str
    storefront: str
    transfer_mode: str
    files: list[OutputFile] = field(default_factory=list)
    zip_file: OutputFile | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DownloadResult":
        files = [OutputFile.from_dict(it) for it in data.get("files", []) or []]
        zip_obj = data.get("zip_file")
        return cls(
            media_type=str(data.get("media_type", "") or ""),
            media_id=str(data.get("media_id", "") or ""),
            storefront=str(data.get("storefront", "") or ""),
            transfer_mode=str(data.get("transfer_mode", "one") or "one"),
            files=files,
            zip_file=OutputFile.from_dict(zip_obj) if isinstance(zip_obj, dict) else None,
        )


@dataclass(slots=True)
class JobStatus:
    job_id: str
    status: str
    error: str = ""
    result: DownloadResult | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobStatus":
        result_data = data.get("result")
        return cls(
            job_id=str(data.get("job_id", "") or ""),
            status=str(data.get("status", "") or ""),
            error=str(data.get("error", "") or ""),
            result=DownloadResult.from_dict(result_data)
            if isinstance(result_data, dict)
            else None,
        )


@dataclass(slots=True)
class SessionSettings:
    quality: str = "alac"
    aac_type: str = "aac-lc"
    mv_audio_type: str = "atmos"
    lyrics_format: str = "lrc"
    include_lyrics: bool = False
    include_cover: bool = False
    include_animated_cover: bool = False
    transfer_mode: str = "one"

    def to_dict(self) -> dict[str, Any]:
        return {
            "quality": self.quality,
            "aac_type": self.aac_type,
            "mv_audio_type": self.mv_audio_type,
            "lyrics_format": self.lyrics_format,
            "include_lyrics": self.include_lyrics,
            "include_cover": self.include_cover,
            "include_animated_cover": self.include_animated_cover,
            "transfer_mode": self.transfer_mode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SessionSettings":
        data = data or {}
        return cls(
            quality=str(data.get("quality", "alac") or "alac"),
            aac_type=str(data.get("aac_type", "aac-lc") or "aac-lc"),
            mv_audio_type=str(data.get("mv_audio_type", "atmos") or "atmos"),
            lyrics_format=str(data.get("lyrics_format", "lrc") or "lrc"),
            include_lyrics=bool(data.get("include_lyrics", False)),
            include_cover=bool(data.get("include_cover", False)),
            include_animated_cover=bool(data.get("include_animated_cover", False)),
            transfer_mode=str(data.get("transfer_mode", "one") or "one"),
        )


@dataclass(slots=True)
class SelectionState:
    kind: str
    title: str
    items: list[SearchItem]
    storefront: str
    created_ts: float
    artist_name: str = ""
