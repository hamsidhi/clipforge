"""Stage 1 — get the source video (YouTube URL or local file) + 16 kHz mono WAV for Whisper."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from clipforge.models import Chapter, VideoInfo
from clipforge.utils.ffmpeg import Timer, probe_video, run_ffmpeg
from clipforge.utils.log import CancelledError, ProgressReporter, log
from clipforge.workspace import Workspace, is_url

YTDLP_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]"
# YouTube's anti-bot wall (common from cloud/datacenter IPs); matched case-insensitively on the yt-dlp message
BLOCK_MARKERS = ("confirm you're not a bot", "confirm you’re not a bot", "sign in to confirm", "http error 429",
                 "too many requests")


class DownloadBlocked(RuntimeError):
    """YouTube refused this machine (bot check / rate limit). On a server the download goes to the laptop instead."""


def is_blocked(message: str) -> bool:
    m = message.lower()
    return any(k in m for k in BLOCK_MARKERS)
# Prefer H.264 inside the ≤1080p limit: fastest to decode on CPU and plays in every browser.
YTDLP_SORT = ["res:1080", "vcodec:h264", "acodec:m4a"]


def _download_youtube(url: str, ws: Workspace, reporter: ProgressReporter, section: tuple[float, float] | None) -> dict:
    import yt_dlp

    timer = Timer()

    def hook(d: dict) -> None:
        if reporter.cancelled():
            raise CancelledError("Job cancelled by user")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                frac = d.get("downloaded_bytes", 0) / total
                reporter.progress(frac * 0.9, timer.eta(frac), f"downloading {d.get('_percent_str', '').strip()}")

    opts: dict = {
        "format": YTDLP_FORMAT,
        "format_sort": YTDLP_SORT,
        "merge_output_format": "mp4",
        "outtmpl": str(ws.dir / "source.%(ext)s"),
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 5,
        "concurrent_fragment_downloads": 4,
    }
    from clipforge.utils.ffmpeg import ffmpeg_bin

    opts["ffmpeg_location"] = str(Path(ffmpeg_bin()).parent)
    if section:
        from yt_dlp.utils import download_range_func

        opts["download_ranges"] = download_range_func(None, [section])
        opts["force_keyframes_at_cuts"] = True
    from clipforge.config import load_config

    scfg = load_config().server
    cookies = Path(scfg.download_cookies_file).expanduser() if scfg.download_cookies_file else None
    if cookies and cookies.exists():
        opts["cookiefile"] = str(cookies)  # optional, server only — see AUTOMATION_GUIDE Part I (account risk)
    info = _extract_with_backoff(yt_dlp, opts, url, scfg.download_max_attempts, reporter)
    _fetch_captions(yt_dlp, info, ws, section, opts.get("cookiefile"))
    # yt-dlp may have produced source.mp4 or source.mkv/webm depending on streams
    if not ws.source.exists():
        produced = sorted(ws.dir.glob("source.*"))
        produced = [p for p in produced if p.suffix not in (".part", ".json", ".ytdl")]
        if not produced:
            raise RuntimeError("yt-dlp finished but no source file was written")
        run_ffmpeg(["-i", str(produced[0]), "-c", "copy", "-movflags", "+faststart", str(ws.source)])
        produced[0].unlink(missing_ok=True)
    return info


def _fetch_captions(yt_dlp, info: dict, ws: Workspace, section: tuple[float, float] | None, cookiefile: str | None) -> None:
    """YouTube captions (manual + auto-generated, json3) for transcript.source auto/youtube — saves a full Whisper run."""
    from clipforge.config import load_config
    from clipforge.stages import transcript_input as ti

    tcfg = load_config().transcript
    if tcfg.source == "whisper":
        return
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, **({"cookiefile": cookiefile} if cookiefile else {})}) as ydl:
            n = ti.fetch_youtube(info, ws, tcfg.youtube_languages, section, lambda u: ydl.urlopen(u).read())
        if n:
            log.info(f"Saved {n} YouTube caption track(s)")
    except Exception as exc:  # captions are an optimisation: Whisper still works without them
        log.warning(f"Couldn't fetch YouTube captions ({exc}); Whisper will transcribe instead.")


def _extract_with_backoff(yt_dlp, opts: dict, url: str, attempts: int, reporter: ProgressReporter,
                          sleep=None) -> dict:
    """A bot check is retried a few times with exponential backoff (60 s, 120 s, …) — never hammering YouTube —
    then raised as DownloadBlocked so the caller can route the download elsewhere."""
    import time

    sleep = sleep or time.sleep
    for attempt in range(1, max(1, attempts) + 1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as exc:
            if not is_blocked(str(exc)):
                raise RuntimeError(
                    f"yt-dlp could not download {url}:\n{exc}\n"
                    "Try: pip install -U yt-dlp  (YouTube changes often). Or download the video yourself and pass the file path."
                ) from exc
            if attempt >= attempts:
                raise DownloadBlocked(
                    f"YouTube asked this computer to sign in to confirm it's not a bot ({attempts} tries). "
                    "Send the video file instead, or let the laptop download it.") from exc
            wait = 60 * 2 ** (attempt - 1)
            reporter.log(f"YouTube bot check — waiting {wait} s before try {attempt + 1}/{attempts}", "warning")
            for _ in range(wait):
                if reporter.cancelled():
                    raise CancelledError("Job cancelled by user")
                sleep(1)
    raise DownloadBlocked("YouTube blocked the download.")


def _link_or_copy(src: Path, dest: Path) -> None:
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def run(source: str, ws: Workspace, reporter: ProgressReporter, section: tuple[float, float] | None = None) -> VideoInfo:
    """Download / import the video, write info.json and audio.wav. Idempotent."""
    raw_info: dict = {}
    if not ws.source.exists():
        if is_url(source):
            raw_info = _download_youtube(source, ws, reporter, section)
        else:
            src = Path(source).expanduser().resolve()
            if not src.exists():
                raise FileNotFoundError(f"Input file not found: {src}")
            if section:
                run_ffmpeg(["-ss", str(section[0]), "-to", str(section[1]), "-i", str(src), "-c:v", "libx264",
                            "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(ws.source)])
            elif src.suffix.lower() == ".mp4":
                _link_or_copy(src, ws.source)
            else:
                log.info(f"Remuxing {src.name} to mp4 …")
                try:
                    run_ffmpeg(["-i", str(src), "-c", "copy", "-movflags", "+faststart", str(ws.source)])
                except Exception:
                    run_ffmpeg(["-i", str(src), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                                "-c:a", "aac", "-b:a", "192k", str(ws.source)])
            raw_info = {"id": ws.video_id, "title": src.stem, "channel": "", "webpage_url": ""}
    elif ws.info.exists():
        prev = ws.read_model(ws.info, VideoInfo)
        raw_info = {"id": prev.id, "title": prev.title, "channel": prev.channel, "webpage_url": prev.url,
                    "chapters": [c.model_dump() for c in prev.chapters]}

    probe = probe_video(ws.source)
    if not probe["has_audio"]:
        raise RuntimeError(f"{ws.source} has no audio track — ClipForge needs speech to find clips.")
    offset = section[0] if section else 0.0
    chapters = []
    for c in raw_info.get("chapters") or []:
        s, e = float(c.get("start_time", c.get("start", 0))) - offset, float(c.get("end_time", c.get("end", 0))) - offset
        if e > 0 and s < probe["duration"]:
            chapters.append(Chapter(title=c.get("title", ""), start=max(0, s), end=min(probe["duration"], e)))
    info = VideoInfo(
        id=ws.video_id,
        title=raw_info.get("title") or ws.video_id,
        channel=raw_info.get("channel") or raw_info.get("uploader") or "",
        url=raw_info.get("webpage_url") or (source if is_url(source) else ""),
        duration=probe["duration"], width=probe["width"], height=probe["height"], fps=probe["fps"],
        chapters=chapters,
    )
    ws.write_model(ws.info, info)

    if not ws.audio.exists():
        reporter.progress(0.92, None, "extracting audio")
        tmp = ws.audio.with_suffix(".tmp.wav")
        run_ffmpeg(["-i", str(ws.source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)])
        tmp.replace(ws.audio)
    if not ws.thumbnail.exists():
        try:
            from clipforge.utils.ffmpeg import extract_frame

            extract_frame(ws.source, min(60.0, info.duration / 3), ws.thumbnail, width=480)
        except Exception as exc:  # thumbnail is cosmetic
            log.warning(f"Could not create thumbnail: {exc}")
    reporter.progress(1.0, 0, "done")
    return info
