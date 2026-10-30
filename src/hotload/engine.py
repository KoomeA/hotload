"""Thin wrapper over the yt-dlp Python API. Blocking; run in a worker thread/process."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
from yt_dlp.utils import YoutubeDLError, download_range_func

from . import security
from .config import Settings

# Messages that suggest the origin throttled/blocked this egress IP, so retrying via another proxy may help.
_RETRYABLE = re.compile(
    r"HTTP Error (403|429|5\d\d)|timed out|Unable to connect|Connection (reset|refused)|proxy|"
    r"Sign in to confirm|unable to download (webpage|video data)|Too Many Requests",
    re.IGNORECASE,
)


class EngineError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class SafeYoutubeDL(yt_dlp.YoutubeDL):
    """YoutubeDL that refuses to fetch non-public addresses.

    Covers every request yt-dlp makes itself (extractor pages, manifests, fragments, redirect targets it
    re-issues) via `urlopen`, and the selected media URL handed to FFmpeg for trimmed downloads via `dl`
    (FFmpeg does its own networking, so redirects it follows are only constrained by the egress policy).
    """

    def __init__(self, params: dict, settings: Settings):
        super().__init__(params)
        self._hl_settings = settings

    def _hl_check(self, url: str | None) -> None:
        if not url or self._hl_settings.allow_private_targets or not url.startswith(("http://", "https://")):
            return
        try:
            security.validate_url(url, self._hl_settings, enforce_allowlist=False)
        except security.UnsafeURL as exc:
            raise YoutubeDLError(f"Blocked request to an unsafe address ({exc})") from exc

    def urlopen(self, req):
        self._hl_check(req if isinstance(req, str) else getattr(req, "url", None))
        return super().urlopen(req)

    def dl(self, name, info, subtitle=False, test=False):
        self._hl_check(info.get("url"))
        self._hl_check(info.get("manifest_url"))
        return super().dl(name, info, subtitle=subtitle, test=test)


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


def _base_opts(settings: Settings, proxy: str | None = None) -> dict:
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
    if proxy:
        opts["proxy"] = proxy
    if settings.ffmpeg_location:
        opts["ffmpeg_location"] = settings.ffmpeg_location
    return opts


def extract(url: str, settings: Settings, proxy: str | None = None) -> dict:
    """Return sanitised metadata for the UI (no download)."""
    try:
        with SafeYoutubeDL({**_base_opts(settings, proxy), "skip_download": True}, settings) as ydl:
            info = ydl.extract_info(url, download=False)
    except YoutubeDLError as exc:
        raise _wrap(exc) from exc
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
    proxy: str | None = None,
    start: float | None = None,
    end: float | None = None,
    subtitles: bool = False,
    block_adult: bool = False,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    opts = _base_opts(settings, proxy)
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


def download(url: str, settings: Settings, workdir: Path, preset: Preset, proxy: str | None = None, **kwargs) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    opts = build_download_opts(settings, workdir, preset, proxy=proxy, **kwargs)
    try:
        with SafeYoutubeDL(opts, settings) as ydl:
            ydl.download([url])
    except YoutubeDLError as exc:
        raise _wrap(exc) from exc
    files = [p for p in workdir.iterdir() if p.is_file() and p.suffix not in (".part", ".ytdl", ".jpg", ".webp", ".png", ".vtt", ".srt")]
    if not files:
        raise EngineError("Nothing was downloaded (the media may exceed a limit)")
    return max(files, key=lambda p: p.stat().st_size)


def _wrap(exc: Exception) -> EngineError:
    msg = _clean(exc)
    return EngineError(msg, retryable=bool(_RETRYABLE.search(str(exc))) and "Blocked request" not in msg)


def _clean(exc: Exception) -> str:
    msg = str(exc).replace("ERROR: ", "").strip()
    return msg.splitlines()[0][:300] if msg else "Download failed"
