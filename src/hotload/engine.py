"""Thin wrapper over the yt-dlp Python API. Blocking; run in a worker thread/process."""

import re
import subprocess
import time
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
from yt_dlp.utils import YoutubeDLError, download_range_func

from . import netguard, security
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


_SCHEMES = ("http://", "https://")


class SafeYoutubeDL(yt_dlp.YoutubeDL):
    """YoutubeDL with layered protection against being used as a proxy into internal networks.

    1. Connect-time guard (`netguard`): inside `with`, every name lookup made by this thread drops non-public addresses.
       This is the layer that stops redirects, DNS rebinding and literal-IP tricks for yt-dlp's own HTTP traffic.
    2. `urlopen` pre-check: the requested URL must be http(s) and pass `security.validate_url` (blocklist, allowlist,
       a readable error for the user). Other schemes (ftp:, data:, file:) are refused outright.
    3. `dl` pre-check: FFmpeg gets media URLs from here. Every one is validated: the selected URL(s), each of
       `requested_formats` and the manifest URL (a merged video+audio selection hands FFmpeg several URLs at once).
    What it cannot cover: FFmpeg does its own networking (redirects, manifest references) and upstream proxies resolve
    names themselves, so production also needs the network-layer egress filter (docs/LAUNCH.md).

    `limits=(seconds, bytes)` bounds *metadata* fetches (extraction): total wall-clock time and total bytes read through
    this object, so a hostile server that streams forever or drips bytes cannot pin a thread or exhaust memory.
    """

    def __init__(self, params: dict, settings: Settings, limits: tuple[float, int] | None = None):
        super().__init__(params)
        self._hl_settings = settings
        self._hl_limits = limits
        self._hl_started = time.monotonic()
        self._hl_bytes = 0
        self._hl_guard = None

    # --- connect-time guard for the duration of the `with` block
    def _proxy_endpoints(self) -> set[tuple[str, int]]:
        from urllib.parse import urlsplit

        # Only real proxy entries. getproxies() also returns the no_proxy list under "no": treating those hosts as
        # proxies would exempt exactly the addresses (localhost, internal ranges) the guard exists to block.
        env = urllib.request.getproxies()
        urls = [self.params.get("proxy"), *(env[k] for k in ("http", "https", "all") if k in env)]
        found = set()
        for u in urls:
            if not u:
                continue
            parts = urlsplit(u if "//" in u else f"//{u}")
            if parts.hostname:
                found.add((parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)))
        return found

    def __enter__(self):
        if not self._hl_settings.allow_private_targets:
            self._hl_guard = netguard.guard(self._proxy_endpoints())
            self._hl_guard.__enter__()
        return super().__enter__()

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            if self._hl_guard is not None:
                self._hl_guard.__exit__(*exc)
                self._hl_guard = None

    # --- pre-checks
    def _hl_check(self, url: str | None) -> None:
        if not url or self._hl_settings.allow_private_targets:
            return
        if not url.lower().startswith(_SCHEMES):
            raise YoutubeDLError("Blocked request: only http and https addresses are allowed")
        try:
            security.validate_url(url, self._hl_settings, enforce_allowlist=False)
        except security.UnsafeURL as exc:
            raise YoutubeDLError(f"Blocked request to an unsafe address ({exc})") from exc

    def urlopen(self, req):
        self._hl_check(req if isinstance(req, str) else getattr(req, "url", None))
        resp = super().urlopen(req)
        if self._hl_limits:
            self._hl_bound(resp)
        return resp

    def _hl_bound(self, resp) -> None:
        """Wrap `resp.read` so metadata fetches are bounded in total time and total bytes.

        `read(n)` blocks until n bytes arrive, so a server that drips one byte at a time would never let a deadline check
        run. `read1()` returns as soon as *any* data is available, so the checks run every time the server sends something
        (and at worst every socket timeout when it sends nothing)."""
        seconds, max_bytes = self._hl_limits
        original = resp.read
        fp = getattr(resp, "fp", None)
        read1 = getattr(fp, "read1", None)

        def some(n: int) -> bytes:
            if read1 is None:
                return original(n)
            try:
                return read1(n, decode_content=True)
            except TypeError:
                return read1(n)
            except YoutubeDLError:
                raise
            except Exception as exc:  # urllib3 timeouts/protocol errors: report as an engine error, never a crash
                raise YoutubeDLError("Connection problem while reading the page") from exc

        def bounded(amt=None):
            chunks, wanted = [], amt
            while True:
                if time.monotonic() - self._hl_started > seconds:
                    raise YoutubeDLError("The site took too long to respond")
                piece = some(65536 if wanted is None else min(65536, wanted))
                self._hl_bytes += len(piece)
                if self._hl_bytes > max_bytes:
                    raise YoutubeDLError("The page is too large to read")
                chunks.append(piece)
                if not piece:
                    if getattr(fp, "closed", False):
                        resp.close()
                    break
                if wanted is not None:
                    wanted -= len(piece)
                    if wanted <= 0:
                        break
            return b"".join(chunks)

        resp.read = bounded

    def dl(self, name, info, subtitle=False, test=False):
        urls = [*str(info.get("url") or "").split("\n"), info.get("manifest_url")]
        for fmt in info.get("requested_formats") or ():
            urls += [*str(fmt.get("url") or "").split("\n"), fmt.get("manifest_url")]
        for url in urls:
            self._hl_check(url)
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
    if not LANG_RE.fullmatch(code) or code.lower() == "all":  # yt-dlp treats "all" as every track: hundreds of files
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
        "playlist_items": "1",  # one URL is one download: a channel/playlist URL must not fan out inside a single job
        "concurrent_fragment_downloads": 1,  # no helper threads: the connect-time guard is per thread
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
        limits = (settings.extract_deadline_seconds, settings.extract_max_bytes)
        with SafeYoutubeDL({**_base_opts(settings, proxy, cookiefile), "skip_download": True}, settings, limits) as ydl:
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
        "live_status": info.get("live_status"),
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
    live_seconds: int | None = None,
    max_duration: int | None = None,
    max_filesize: int | None = None,
    block_adult: bool = False,
    blocked_media: frozenset[str] = frozenset(),
    progress: Callable[[dict], None] | None = None,
) -> dict:
    opts = _base_opts(settings, proxy, cookiefile)
    size_cap = min(max_filesize or settings.max_filesize_bytes, settings.max_filesize_bytes)
    time_cap = int(max_duration or settings.max_duration_seconds)
    opts["max_filesize"] = size_cap
    uses_ffmpeg_network = start is not None or end is not None or bool(live_seconds)
    if uses_ffmpeg_network and settings.quotas_enabled and not settings.egress_filtered:
        # FFmpeg does its own networking (redirects, manifest references), which the in-process guard cannot see. On the
        # hosted service these features stay off until the operator confirms the workers sit behind the egress filter.
        raise EngineError("Trimming and live recording are temporarily unavailable")
    langs = [validate_lang(code) for code in (subtitle_langs or [])]
    if preset.subs_only and not langs:
        raise EngineError("Choose at least one subtitle language")
    if subtitle_format not in SUBTITLE_FORMATS or subtitle_mode not in SUBTITLE_MODES:
        raise EngineError("Unsupported subtitle option")
    opts.update(
        format=format_for(preset, audio_lang) if not preset.subs_only else None,
        outtmpl=str(workdir / "%(title).150B [%(id)s].%(ext)s"),
        paths={"home": str(workdir)},
        match_filter=_duration_filter(max_duration or settings.max_duration_seconds, start, end, block_adult, blocked_media,
                                      recording=bool(live_seconds)),
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
    # Bound what FFmpeg may write, whatever the source claims: a transcode of an endless or mislabelled file would
    # otherwise run for hours and fill the disk. -t truncates (the duration check below refuses known-long media first).
    opts["postprocessor_args"] = {"extractaudio": ["-t", str(time_cap), "-fs", str(size_cap)], "merger": ["-fs", str(size_cap)]}
    ffmpeg_in = ["-protocol_whitelist", "http,https,tcp,tls,crypto", "-rw_timeout", "30000000"]
    if (start is not None or end is not None) and not preset.subs_only:
        opts["download_ranges"] = download_range_func(None, [(start or 0, end or float("inf"))])
        opts["force_keyframes_at_cuts"] = True
        opts["external_downloader_args"] = {"ffmpeg_i": ffmpeg_in, "ffmpeg": ["-fs", str(size_cap)]}
    if live_seconds:
        if start is not None or end is not None or sponsorblock != "off" or preset.subs_only:
            raise EngineError("Live recording cannot be combined with trimming, SponsorBlock or subtitles-only")
        # FFmpeg itself enforces the time cap, so the recording ends cleanly with a finalised file. The input timeout
        # stops a stalled stream from holding a worker slot forever.
        opts["external_downloader"] = {"default": "ffmpeg"}
        opts["external_downloader_args"] = {"ffmpeg_i": ffmpeg_in, "ffmpeg": ["-t", str(int(live_seconds)), "-fs", str(size_cap)]}
        opts["live_from_start"] = False
    def watchdog(d: dict) -> None:
        # max_filesize only checks Content-Length up front; a chunked or lying server needs a running count.
        if d.get("status") == "downloading" and (d.get("downloaded_bytes") or 0) > size_cap:
            raise yt_dlp.utils.DownloadError("The file is larger than your plan allows")
        if progress:
            progress(d)

    opts["progress_hooks"] = [watchdog]
    return opts


def _duration_filter(max_seconds: int, start: float | None, end: float | None, block_adult: bool = False,
                     blocked_media: frozenset[str] = frozenset(), recording: bool = False):
    def _filter(info, *, incomplete):
        if blocked_media and f"{info.get('extractor_key')}:{info.get('id')}" in blocked_media:
            return "This content is not available"
        if block_adult and (info.get("age_limit") or 0) >= 18:
            return "Age confirmation required for this content"
        if recording:
            if info.get("live_status") == "is_upcoming":
                return "This stream has not started yet"
            return None  # the recording length is capped by FFmpeg, not by the media duration
        if info.get("is_live"):
            return "This is a live stream: choose Record live stream to capture it"
        duration = info.get("duration")
        if duration is None:
            return None
        effective = (min(end, duration) if end else duration) - (start or 0)
        if effective > max_seconds:
            return f"Selection is longer than the {max_seconds // 60} minute limit"
        return None

    return _filter


_SKIP_SUFFIXES = {".part", ".ytdl", ".jpg", ".jpeg", ".webp", ".png", ".json", ".temp", ".description", ".meta", ".orig", ".uncut"}
_SUB_SUFFIXES = {".srt", ".vtt", ".ass", ".lrc"}


def download(url: str, settings: Settings, workdir: Path, preset: Preset, proxy: str | None = None, cookiefile: Path | None = None, **kwargs) -> DownloadResult:
    workdir.mkdir(parents=True, exist_ok=True)
    opts = build_download_opts(settings, workdir, preset, proxy=proxy, cookiefile=cookiefile, **kwargs)
    try:
        with SafeYoutubeDL(opts, settings) as ydl:
            info = ydl.extract_info(url, download=True)
    except YoutubeDLError as exc:
        raise _wrap(exc) from exc
    if (info or {}).get("duration") is None and not kwargs.get("live_seconds") and not preset.subs_only:
        # The duration limit trusts what the site reports, and many sites (direct files, some HLS) report nothing.
        for media in _media_files(workdir)[:3]:
            verify_duration(media, kwargs.get("max_duration") or settings.max_duration_seconds, kwargs.get("start"), kwargs.get("end"))
    path = package(workdir, keep_subs=kwargs.get("subtitle_mode", "file") == "file" or preset.subs_only)
    return DownloadResult(path, (info or {}).get("title"), (info or {}).get("extractor_key"))


def _media_files(workdir: Path) -> list[Path]:
    return sorted((p for p in workdir.iterdir() if p.is_file() and p.suffix.lower() not in _SKIP_SUFFIXES | _SUB_SUFFIXES | {".zip"}),
                  key=lambda p: -p.stat().st_size)


def verify_duration(path: Path, max_seconds: int, start: float | None = None, end: float | None = None) -> None:
    """Refuse (and delete) a finished file that is longer than the plan allows. Skipped if ffprobe is unavailable."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=30, check=False).stdout.strip()
        seconds = float(out)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return
    allowed = max_seconds * 1.05 + 2  # container rounding
    if seconds > allowed:
        path.unlink(missing_ok=True)
        raise EngineError(f"This is longer than your plan allows ({max_seconds // 60} minutes)")


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
            "skip_download": True, "playlistend": limit, "playlist_items": None}
    try:
        with SafeYoutubeDL(opts, settings, (settings.extract_deadline_seconds, settings.extract_max_bytes)) as ydl:
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
    if re.search(r"proxy", msg, re.IGNORECASE):  # these messages can name the operator's internal proxy host and port
        msg = "Connection problem. Please try again."
    return EngineError(msg, retryable=bool(_RETRYABLE.search(str(exc))) and "Blocked request" not in msg)


def _clean(exc: Exception) -> str:
    msg = str(exc).replace("ERROR: ", "").strip()
    return msg.splitlines()[0][:300] if msg else "Download failed"
