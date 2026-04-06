from __future__ import annotations

import re
from dataclasses import dataclass


APPLE_MUSIC_URL_RE = re.compile(r"https?://(?:music|beta\.music|classical\.music)\.apple\.com/[^\s]+")

QUALITY_VALUES = {"alac", "flac", "aac", "atmos"}
AAC_VALUES = {"aac", "aac-lc", "aac-binaural", "aac-downmix"}
MV_AUDIO_VALUES = {"atmos", "ac3", "aac"}
LYRICS_FORMAT_VALUES = {"lrc", "ttml"}


@dataclass(slots=True)
class SelectionAction:
    index: int = 0
    op: str = "download"


def extract_first_apple_music_url(text: str) -> str:
    m = APPLE_MUSIC_URL_RE.search(text or "")
    return m.group(0).strip() if m else ""


def parse_am_payload(message: str) -> tuple[str, str]:
    msg = (message or "").strip()
    if msg.startswith("/"):
        msg = msg[1:]
    if msg.lower().startswith("am"):
        msg = msg[2:].strip()
    if not msg:
        return "", ""
    parts = msg.split(maxsplit=1)
    cmd = parts[0].strip().lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    return cmd, arg


def parse_selection_action(text: str) -> SelectionAction | None:
    raw = (text or "").strip().lower()
    if not raw:
        return None

    if raw in {"专辑", "album", "albums"}:
        return SelectionAction(index=0, op="artist_albums")
    if raw in {"mv", "mvs", "musicvideo", "music-video", "music-videos"}:
        return SelectionAction(index=0, op="artist_mvs")

    parts = raw.split()
    if not parts:
        return None
    if not parts[0].isdigit():
        return None
    idx = int(parts[0])
    if idx <= 0:
        return None
    if len(parts) == 1:
        return SelectionAction(index=idx, op="download")

    token = parts[1]
    if token in {"zip", "压缩", "打包"}:
        return SelectionAction(index=idx, op="zip")
    if token in {"歌词", "lyric", "lyrics"}:
        return SelectionAction(index=idx, op="lyrics")
    if token in {"封面", "cover"}:
        return SelectionAction(index=idx, op="cover")
    if token in {"动态封面", "motion", "animated", "animatedcover"}:
        return SelectionAction(index=idx, op="animated_cover")
    if token in {"专辑", "album", "albums"}:
        return SelectionAction(index=idx, op="artist_albums")
    if token in {"mv", "mvs", "musicvideo", "music-video", "music-videos"}:
        return SelectionAction(index=idx, op="artist_mvs")
    return SelectionAction(index=idx, op="download")


def normalize_transfer_mode(raw: str) -> str:
    text = (raw or "").strip().lower()
    if text in {"zip", "压缩", "打包"}:
        return "zip"
    return "one"


def apply_setting_token(settings: dict, token: str) -> tuple[bool, str]:
    t = (token or "").strip().lower()
    if not t:
        return False, ""

    if t in QUALITY_VALUES:
        settings["quality"] = t
        return True, f"音质已设置为 {t.upper()}"

    if t in AAC_VALUES:
        settings["aac_type"] = t
        return True, f"AAC 模式已设置为 {t}"

    if t.startswith("mv-"):
        mv = t[3:]
        if mv in MV_AUDIO_VALUES:
            settings["mv_audio_type"] = mv
            return True, f"MV 音轨已设置为 {mv}"

    if t in MV_AUDIO_VALUES:
        settings["mv_audio_type"] = t
        return True, f"MV 音轨已设置为 {t}"

    if t in LYRICS_FORMAT_VALUES:
        settings["lyrics_format"] = t
        return True, f"歌词格式已设置为 {t.upper()}"

    if t in {"zip", "逐个", "one", "one-by-one", "one_by_one"}:
        settings["transfer_mode"] = "zip" if t == "zip" else "one"
        return True, f"传输模式已设置为 {'ZIP' if settings['transfer_mode'] == 'zip' else '逐个发送'}"

    if t in {"歌词开", "lyrics_on"}:
        settings["include_lyrics"] = True
        return True, "已开启自动附带歌词"
    if t in {"歌词关", "lyrics_off"}:
        settings["include_lyrics"] = False
        return True, "已关闭自动附带歌词"

    if t in {"封面开", "cover_on"}:
        settings["include_cover"] = True
        return True, "已开启自动附带封面"
    if t in {"封面关", "cover_off"}:
        settings["include_cover"] = False
        return True, "已关闭自动附带封面"

    if t in {"动态封面开", "animated_on"}:
        settings["include_animated_cover"] = True
        return True, "已开启自动附带动态封面"
    if t in {"动态封面关", "animated_off"}:
        settings["include_animated_cover"] = False
        return True, "已关闭自动附带动态封面"

    return False, ""
