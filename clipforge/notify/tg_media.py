"""Sending clip files to Telegram, with a documented size fallback chain (tested in tests/test_telegram.py):

  1. preview = the 540×960 proxy, sendVideo + supports_streaming (a few MB)
     full    = the real 1080×1920 file, sendDocument (Telegram doesn't re-compress documents)
  2. limit: 50 MB on the cloud Bot API, 2000 MB with a self-hosted telegram-bot-api server (telegram.api_base_url)
  3. too big → a "Telegram-fit" copy: 2-pass H.264 at a target bitrate (still 1080×1920 when the bitrate allows it,
     720×1280 below ~1.2 Mbps), cached in clips/_work/NN/telegram_fit.mp4; the caption says it was re-encoded
  4. still too big / encoding failed → a private Tailscale link to the file (only opens on your tailnet), else a hint
A file_id cache means re-sending the same file costs no upload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clipforge.notify import tg_store as st
from clipforge.notify.tg_api import TgError, split_caption
from clipforge.utils.log import log

AUDIO_KBPS = 128
FIT_MARGIN = 0.92  # container overhead + bitrate variance


def _send_file(api, chat_id: int, path: Path, method: str, field: str, caption: str, buttons, **extra: Any) -> dict:
    key = st.file_key(path, method)
    cap, rest = split_caption(caption)
    markup = {"inline_keyboard": buttons} if buttons else None
    fid = st.cached_file_id(key)
    res = None
    if fid:
        try:
            res = api.call(method, chat_id=chat_id, **{field: fid}, caption=cap, parse_mode="HTML", reply_markup=markup, **extra)
        except TgError:
            st.forget_file_id(key)  # stale id (other bot token, deleted) → upload again
            res = None
    if res is None:
        mime = "video/mp4" if path.suffix.lower() == ".mp4" else "application/octet-stream"
        with open(path, "rb") as f:
            res = api.call(method, files={field: (path.name, f, mime)}, chat_id=chat_id, caption=cap, parse_mode="HTML",
                           reply_markup=markup, **extra)
        got = (res.get("video") or res.get("document") or res.get("photo", [{}])[-1] or {}).get("file_id") if isinstance(res, dict) else None
        if got:
            st.cache_file_id(key, got)
    for chunk in rest:
        api.call("sendMessage", chat_id=chat_id, text=chunk, parse_mode="HTML", disable_web_page_preview=True)
    return res or {}


def telegram_fit(src: Path, limit: int, duration: float) -> Path:
    """2-pass re-encode to fit `limit` bytes. Raises RuntimeError when even 720p would be unwatchable."""
    from clipforge.utils.ffmpeg import run_ffmpeg

    rank_dir = src.parent / "_work" / src.name.split("_", 1)[0]
    rank_dir.mkdir(parents=True, exist_ok=True)
    out = rank_dir / "telegram_fit.mp4"
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime and out.stat().st_size <= limit:
        return out
    total_kbps = limit * 8 * FIT_MARGIN / 1000 / max(duration, 1)
    video_kbps = int(total_kbps - AUDIO_KBPS)
    if video_kbps < 400:
        raise RuntimeError(f"too long for {limit // (1024 * 1024)} MB even at low quality")
    scale = "scale=1080:1920" if video_kbps >= 1200 else "scale=720:1280"
    passlog = rank_dir / "tgfit"
    common = ["-i", str(src), "-vf", scale, "-c:v", "libx264", "-preset", "medium", "-b:v", f"{video_kbps}k",
              "-maxrate", f"{int(video_kbps * 1.2)}k", "-bufsize", f"{video_kbps * 2}k", "-pix_fmt", "yuv420p",
              "-passlogfile", str(passlog)]
    import os

    run_ffmpeg([*common, "-pass", "1", "-an", "-f", "mp4", os.devnull])
    tmp = out.with_suffix(".tmp.mp4")
    run_ffmpeg([*common, "-pass", "2", "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-movflags", "+faststart", str(tmp)])
    tmp.replace(out)
    for f in rank_dir.glob("tgfit*"):
        f.unlink(missing_ok=True)
    if out.stat().st_size > limit:
        raise RuntimeError("the re-encoded copy is still too big")
    return out


def deliver(api, chat_id: int, clip: Path, mode: str, caption: str, buttons=None, *, clip_id: str = "",
            duration: float = 0, link_fn=None) -> str:
    """Send a clip as preview (streamable video) or full quality (document). Returns how it went:
    'sent' | 'reencoded' | 'link' | 'none'."""
    limit = api.send_limit
    if mode == "preview":
        from clipforge.stages.preview import ensure_preview

        try:
            path = ensure_preview(clip)
        except Exception as exc:
            log.warning(f"[telegram] preview failed for {clip.name}: {exc}")
            path = clip
        if path.stat().st_size <= limit:
            _send_file(api, chat_id, path, "sendVideo", "video", caption, buttons, supports_streaming=True,
                       width=540, height=960, duration=int(duration) or None)
            return "sent"
    else:
        if clip.stat().st_size <= limit:
            _send_file(api, chat_id, clip, "sendDocument", "document", caption, buttons, disable_content_type_detection=True)
            return "sent"
        try:
            fit = telegram_fit(clip, limit, duration or _duration(clip))
            _send_file(api, chat_id, fit, "sendDocument", "document",
                       caption + f"\n<i>Re-encoded to fit Telegram's {limit // (1024 * 1024)} MB limit "
                                 f"(the original is {clip.stat().st_size / 1048576:.0f} MB).</i>", buttons,
                       disable_content_type_detection=True)
            return "reencoded"
        except Exception as exc:
            log.warning(f"[telegram] Telegram-fit copy of {clip.name} failed: {exc}")
    url = link_fn(f"/api/clips/{clip_id}/video?download=1") if link_fn and clip_id else None
    if url:
        api.call("sendMessage", chat_id=chat_id, parse_mode="HTML", disable_web_page_preview=True,
                 text=f"{caption}\n📎 Too big for Telegram — download it here (opens only on your Tailscale network):\n{url}",
                 reply_markup={"inline_keyboard": buttons} if buttons else None)
        return "link"
    api.call("sendMessage", chat_id=chat_id, parse_mode="HTML",
             text=f"{caption}\n📎 Too big for Telegram ({clip.stat().st_size / 1048576:.0f} MB). Set up remote access "
                  "(Tailscale) or a local Bot API server to get it here — see docs/TELEGRAM.md.",
             reply_markup={"inline_keyboard": buttons} if buttons else None)
    return "none"


def _duration(p: Path) -> float:
    try:
        from clipforge.utils.ffmpeg import probe_video

        return float(probe_video(p).get("duration") or 60)
    except Exception:
        return 60.0


def album(api, chat_id: int, photos: list[tuple[Path, str]]) -> None:
    """Thumbnails as albums of 2–10 (one photo is sent alone). Buttons can't go on albums — cards follow."""
    photos = [(p, c) for p, c in photos if p and p.exists()]
    for i in range(0, len(photos), 10):
        group = photos[i:i + 10]
        if len(group) == 1:
            p, c = group[0]
            with open(p, "rb") as f:
                api.call("sendPhoto", files={"photo": (p.name, f, "image/jpeg")}, chat_id=chat_id, caption=c[:1024])
            continue
        files, media = {}, []
        handles = []
        try:
            for j, (p, c) in enumerate(group):
                h = open(p, "rb")
                handles.append(h)
                files[f"p{j}"] = (p.name, h, "image/jpeg")
                media.append({"type": "photo", "media": f"attach://p{j}", "caption": c[:1024]})
            api.call("sendMediaGroup", files=files, chat_id=chat_id, media=media)
        finally:
            for h in handles:
                h.close()
