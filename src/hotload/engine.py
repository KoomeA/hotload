"""Thin wrapper over the yt-dlp Python API. Blocking; run in a worker thread/process."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError, download_range_func

from .config import Settings


class EngineError(Exception):
    pass


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    format: str
    audio_codec: str | None = None
    audio_quality: str | None = None
    merge_format: str | None = None


PRESETS: dict[str, Preset] = {
    p.key: p
    for p in (
        Preset("best_mp4", "Best video (MP4)", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b", merge_format="mp4"),
        Preset("1080p", "1080p (MP4)", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/bv*[height<=1080]+ba", merge_format="mp4"),
        Preset("720p", "720p (MP4)", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720]/bv*[height<=720]+ba", merge_format="mp4"),
        Preset("480p", "480p (MP4)", "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480]/bv*[height<=480]+ba", merge_format="mp4"),
        Preset("mp3", "Audio (MP3 192k)", "ba/b", "mp3", "192"),
        Preset("m4a", "Audio (M4A)", "ba[ext=m4a]/ba/b", "m4a", "0"),
        Preset("opus", "Audio (Opus)", "ba/b", "opus", "0"),
    )
}


def _base_opts(settings: Settings) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": settings.extract_timeout_seconds,
        "retries": 3,
        "restrictfilenames": True,
        "max_filesize": settings.max_filesize_bytes,
        "allow_unplayable_formats": False,  # never touch DRM
        "ignoreerrors": False,
    }
    if settings.proxy_url:
        opts["proxy"] = settings.proxy_url
    if settings.ffmpeg_location:
        opts["ffmpeg_location"] = settings.ffmpeg_location
    return opts


def extract(url: str, settings: Settings) -> dict:
    """Return sanitised metadata for the UI (no download)."""
    try:
        with yt_dlp.YoutubeDL({**_base_opts(settings), "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise EngineError(_clean(exc)) from exc
    if info is None:
        raise EngineError("No media found at this URL")
    formats = info.get("formats") or []
    if formats and not any(not f.get("has_drm") for f in formats):
        raise EngineError("This media is DRM-protected and cannot be downloaded")
    return summarise(info)


def summarise(info: dict) -> dict:
    heights = sorted({f["height"] for f in info.get("formats") or [] if f.get("height")}, reverse=True)
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "site": info.get("extractor_key"),
        "age_limit": info.get("age_limit") or 0,
        "is_live": bool(info.get("is_live")),
        "heights": heights,
        "has_subtitles": bool(info.get("subtitles")),
    }


def build_download_opts(
    settings: Settings,
    workdir: Path,
    preset: Preset,
    start: float | None = None,
    end: float | None = None,
    subtitles: bool = False,
    block_adult: bool = False,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    opts = _base_opts(settings)
    opts.update(
        format=preset.format,
        outtmpl=str(workdir / "%(title).150B [%(id)s].%(ext)s"),
        paths={"home": str(workdir)},
        match_filter=_duration_filter(settings.max_duration_seconds, start, end, block_adult),
    )
    if preset.merge_format:
        opts["merge_output_format"] = preset.merge_format
    pps = []
    if preset.audio_codec:
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": preset.audio_codec,
                    "preferredquality": preset.audio_quality or "0"})
        pps += [{"key": "FFmpegMetadata", "add_metadata": True}, {"key": "EmbedThumbnail"}]
        opts["writethumbnail"] = True
    if subtitles and not preset.audio_codec:
        opts.update(writesubtitles=True, writeautomaticsub=False, subtitleslangs=["en.*"])
        pps.append({"key": "FFmpegEmbedSubtitle"})
    if pps:
        opts["postprocessors"] = pps
    if start is not None or end is not None:
        opts["download_ranges"] = download_range_func(None, [(start or 0, end or float("inf"))])
        opts["force_keyframes_at_cuts"] = True
    if progress:
        opts["progress_hooks"] = [progress]
    return opts


def _duration_filter(max_seconds: int, start: float | None, end: float | None, block_adult: bool = False):
    def _filter(info, *, incomplete):
        if block_adult and (info.get("age_limit") or 0) >= 18:
            return "Age confirmation required for this content"
        if info.get("is_live"):
            return "Live streams are not supported yet"
        duration = info.get("duration")
        if duration is None:
            return None
        effective = (min(end, duration) if end else duration) - (start or 0)
        if effective > max_seconds:
            return f"Selection is longer than the {max_seconds // 60} minute limit"
        return None

    return _filter


def download(url: str, settings: Settings, workdir: Path, preset: Preset, **kwargs) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    opts = build_download_opts(settings, workdir, preset, **kwargs)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except DownloadError as exc:
        raise EngineError(_clean(exc)) from exc
    files = [p for p in workdir.iterdir() if p.is_file() and p.suffix not in (".part", ".ytdl", ".jpg", ".webp", ".png", ".vtt", ".srt")]
    if not files:
        raise EngineError("Nothing was downloaded (the media may exceed a limit)")
    return max(files, key=lambda p: p.stat().st_size)


def _clean(exc: Exception) -> str:
    msg = str(exc).replace("ERROR: ", "").strip()
    return msg.splitlines()[0][:300] if msg else "Download failed"
