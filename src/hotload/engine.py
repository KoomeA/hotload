"""Thin wrapper over the yt-dlp Python API. Blocking; run in a worker thread/process."""

import re
import zipfile
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
    subs_only: bool = False


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
        Preset("subtitles", "Subtitles only", "", subs_only=True),
    )
}


LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,2}$")
SPONSORBLOCK_MODES = ("off", "mark", "remove")
# Conservative defaults: "remove" cuts only clearly promotional segments; "mark" adds chapters for the rest too.
SB_REMOVE = ["sponsor", "selfpromo", "interaction"]
SB_MARK = ["sponsor", "selfpromo", "interaction", "intro", "outro", "preview", "music_offtopic"]
SUBTITLE_FORMATS = ("srt", "vtt")
SUBTITLE_MODES = ("embed", "file")


def validate_lang(code: str) -> str:
    """Language codes end up inside yt-dlp format selectors, so they must be strictly validated."""
    if not LANG_RE.fullmatch(code):
        raise ValueError(f"Invalid language code: {code!r}")
    return code


def format_for(preset: Preset, audio_lang: str | None) -> str:
    """Prefer the requested audio language, falling back to the preset's normal selection."""
    if not audio_lang:
        return preset.format
    lang = validate_lang(audio_lang)
    selector = re.sub(r"\bba\b(?!\[)", f"ba[language^={lang}]", preset.format)
    selector = selector.replace("ba[ext=m4a]", f"ba[ext=m4a][language^={lang}]")
    return f"{selector}/{preset.format}"


@dataclass
class DownloadResult:
    path: Path
    title: str | None = None
    site: str | None = None


def _base_opts(settings: Settings, proxy: str | None = None, cookiefile: Path | None = None) -> dict:
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
    if cookiefile:
        opts["cookiefile"] = str(cookiefile)
    if settings.ffmpeg_location:
        opts["ffmpeg_location"] = settings.ffmpeg_location
    return opts


def extract(url: str, settings: Settings, proxy: str | None = None, cookiefile: Path | None = None) -> dict:
    """Return sanitised metadata for the UI (no download)."""
    try:
        with SafeYoutubeDL({**_base_opts(settings, proxy, cookiefile), "skip_download": True}, settings) as ydl:
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
        "subtitles": sorted(info.get("subtitles") or {})[:60],
        "auto_captions": sorted(info.get("automatic_captions") or {})[:60],
        "audio_langs": sorted({
            f["language"] for f in info.get("formats") or []
            if f.get("language") and f.get("acodec") not in (None, "none") and LANG_RE.fullmatch(f["language"])
        }),
    }


def build_download_opts(
    settings: Settings,
    workdir: Path,
    preset: Preset,
    proxy: str | None = None,
    cookiefile: Path | None = None,
    start: float | None = None,
    end: float | None = None,
    subtitle_langs: list[str] | None = None,
    subtitle_format: str = "srt",
    subtitle_mode: str = "file",
    subtitle_auto: bool = False,
    audio_lang: str | None = None,
    sponsorblock: str = "off",
    max_duration: int | None = None,
    block_adult: bool = False,
    blocked_media: frozenset[str] = frozenset(),
    progress: Callable[[dict], None] | None = None,
) -> dict:
    opts = _base_opts(settings, proxy, cookiefile)
    langs = [validate_lang(code) for code in (subtitle_langs or [])]
    if preset.subs_only and not langs:
        raise EngineError("Choose at least one subtitle language")
    if subtitle_format not in SUBTITLE_FORMATS or subtitle_mode not in SUBTITLE_MODES:
        raise EngineError("Unsupported subtitle option")
    opts.update(
        format=format_for(preset, audio_lang) if not preset.subs_only else None,
        outtmpl=str(workdir / "%(title).150B [%(id)s].%(ext)s"),
        paths={"home": str(workdir)},
        match_filter=_duration_filter(max_duration or settings.max_duration_seconds, start, end, block_adult, blocked_media),
    )
    if preset.merge_format:
        opts["merge_output_format"] = preset.merge_format
    pps = []
    if preset.audio_codec:
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": preset.audio_codec,
                    "preferredquality": preset.audio_quality or "0"})
        pps += [{"key": "FFmpegMetadata", "add_metadata": True}, {"key": "EmbedThumbnail"}]
        opts["writethumbnail"] = True
    if langs:
        opts.update(writesubtitles=True, writeautomaticsub=subtitle_auto, subtitleslangs=langs)
        if preset.subs_only:
            opts["skip_download"] = True
        if subtitle_mode == "embed" and not preset.audio_codec and not preset.subs_only:
            pps.append({"key": "FFmpegEmbedSubtitle"})
        else:
            opts["subtitlesformat"] = f"{subtitle_format}/best"
            pps.append({"key": "FFmpegSubtitlesConvertor", "format": subtitle_format, "when": "before_dl"})
    if sponsorblock not in SPONSORBLOCK_MODES:
        raise EngineError("Unsupported SponsorBlock option")
    if sponsorblock != "off" and not preset.subs_only:
        if start is not None or end is not None:
            raise EngineError("Choose either trimming or SponsorBlock, not both")
        cats = SB_REMOVE if sponsorblock == "remove" else SB_MARK
        # SponsorBlock only knows YouTube; on other sites these steps do nothing. They must run before audio extraction.
        pps = [
            {"key": "SponsorBlock", "categories": cats, "when": "after_filter"},
            {"key": "ModifyChapters", "remove_sponsor_segments": cats if sponsorblock == "remove" else [],
             "remove_chapters_patterns": [], "remove_ranges": [], "force_keyframes": False,
             "sponsorblock_chapter_title": "[SponsorBlock]: %(category_names)l"},
            *pps,
        ]
    if pps:
        opts["postprocessors"] = pps
    if (start is not None or end is not None) and not preset.subs_only:
        opts["download_ranges"] = download_range_func(None, [(start or 0, end or float("inf"))])
        opts["force_keyframes_at_cuts"] = True
    if progress:
        opts["progress_hooks"] = [progress]
    return opts


def _duration_filter(max_seconds: int, start: float | None, end: float | None, block_adult: bool = False,
                     blocked_media: frozenset[str] = frozenset()):
    def _filter(info, *, incomplete):
        if blocked_media and f"{info.get('extractor_key')}:{info.get('id')}" in blocked_media:
            return "This content is not available"
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


_SKIP_SUFFIXES = {".part", ".ytdl", ".jpg", ".jpeg", ".webp", ".png", ".json", ".temp"}
_SUB_SUFFIXES = {".srt", ".vtt", ".ass", ".lrc"}


def download(url: str, settings: Settings, workdir: Path, preset: Preset, proxy: str | None = None, cookiefile: Path | None = None, **kwargs) -> DownloadResult:
    workdir.mkdir(parents=True, exist_ok=True)
    opts = build_download_opts(settings, workdir, preset, proxy=proxy, cookiefile=cookiefile, **kwargs)
    try:
        with SafeYoutubeDL(opts, settings) as ydl:
            info = ydl.extract_info(url, download=True)
    except YoutubeDLError as exc:
        raise _wrap(exc) from exc
    path = package(workdir, keep_subs=kwargs.get("subtitle_mode", "file") == "file" or preset.subs_only)
    return DownloadResult(path, (info or {}).get("title"), (info or {}).get("extractor_key"))


def package(workdir: Path, keep_subs: bool = True) -> Path:
    """Pick the deliverable: a single file as-is, or a ZIP when media plus subtitle files were produced."""
    files = [p for p in workdir.iterdir() if p.is_file() and p.suffix.lower() not in _SKIP_SUFFIXES and p.suffix != ".zip"]
    subs = sorted(p for p in files if p.suffix.lower() in _SUB_SUFFIXES)
    media = [p for p in files if p not in subs]
    outputs = (media + subs) if keep_subs else media
    if not outputs:
        raise EngineError("Nothing was downloaded (the media may exceed a limit)")
    if len(outputs) == 1:
        return outputs[0]
    main = max(media, key=lambda p: p.stat().st_size) if media else outputs[0]
    archive = workdir / f"{main.stem[:120]}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in outputs:
            zf.write(p, arcname=p.name)
    return archive


def extract_playlist(url: str, settings: Settings, proxy: str | None = None, limit: int = 50) -> dict:
    """List a playlist's entries without resolving each one. Non-playlists return {"playlist": False}."""
    opts = {**_base_opts(settings, proxy), "noplaylist": False, "extract_flat": "in_playlist",
            "skip_download": True, "playlistend": limit}
    try:
        with SafeYoutubeDL(opts, settings) as ydl:
            info = ydl.extract_info(url, download=False)
    except YoutubeDLError as exc:
        raise _wrap(exc) from exc
    if not info or info.get("_type") != "playlist":
        return {"playlist": False}
    entries = []
    for e in info.get("entries") or []:
        link = (e or {}).get("url") or (e or {}).get("webpage_url")
        if link and link.startswith(("http://", "https://")):
            entries.append({"url": link, "title": e.get("title"), "duration": e.get("duration")})
    return {"playlist": True, "title": info.get("title"), "count": len(entries), "entries": entries[:limit]}


def _wrap(exc: Exception) -> EngineError:
    msg = _clean(exc)
    return EngineError(msg, retryable=bool(_RETRYABLE.search(str(exc))) and "Blocked request" not in msg)


def _clean(exc: Exception) -> str:
    msg = str(exc).replace("ERROR: ", "").strip()
    return msg.splitlines()[0][:300] if msg else "Download failed"
