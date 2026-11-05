"""Publication state machine (both platforms) + the ONE place that turns a queue item into words.

One `QueueItem` row = one publication = (clip, platform, account). Stored status values are short lowercase codes
(kept from Phase 12.5 so old rows and old processes keep working); `CANONICAL` maps them to the names used in the
docs, and `status_text()` gives the plain-words label/detail/next step shown by the web app AND Telegram.

    draft ─► queued ─► uploading ─► processing ─► scheduled ─► published      (YouTube: private + publishAt)
       ▲        │          │             │            └─────► locked_private  (YouTube kept it private)
       │        │          │             └─► uploaded (private/unlisted by your choice, never public by itself)
       │        │          └──► failed (Retry)                     Instagram: … processing ─► ready ─► published
       │        ├──► needs_manual   (assisted mode only: upload in Studio / the Instagram app, then Sync or Link)
       │        ├──► missed         (the slot passed before the upload / publish: Reschedule or Publish now)
       └────────┴──► cancelled
    queued + next_attempt_at = RETRYING;  queued + reason login_expired = REQUIRES_REAUTHENTICATION.

Every transition goes through `check()` (called by `queue.save`); an illegal one raises `IllegalTransition`.
"published" also needs a platform media id (YouTube video id / Instagram media id); the only exception is an
Instagram Reel whose link you pasted for a manual (no-API) account, which is labelled as such.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

STATUSES = ("draft", "queued", "uploading", "processing", "ready", "scheduled", "uploaded", "published", "failed",
            "locked_private", "needs_manual", "missed", "cancelled")
ATTENTION = ("failed", "locked_private", "needs_manual", "missed")

# Names from the publishing spec (DECISIONS → "Publishing repair") for each stored status.
CANONICAL = {
    "draft": "DRAFT", "queued": "SCHEDULED", "uploading": "UPLOADING", "processing": "AWAITING_PLATFORM_PROCESSING",
    "ready": "READY_TO_PUBLISH", "scheduled": "PUBLISHED_PRIVATE_SCHEDULED", "uploaded": "UPLOADED_PRIVATE",
    "published": "PUBLISHED", "failed": "FAILED", "locked_private": "LOCKED_PRIVATE", "needs_manual": "NEEDS_MANUAL",
    "missed": "MISSED", "cancelled": "CANCELLED",
}

_PLATFORM_STATES = {"scheduled", "uploaded", "published", "locked_private", "processing", "ready"}
# from → allowed to.  Same-state saves are always allowed.  Linking / Sync may jump to whatever the platform reports.
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"queued", "cancelled"},
    "queued": {"draft", "uploading", "needs_manual", "missed", "failed", "cancelled"} | _PLATFORM_STATES,
    "uploading": {"queued", "failed", "needs_manual", "missed"} | _PLATFORM_STATES,
    "processing": {"queued", "failed", "needs_manual", "missed"} | _PLATFORM_STATES,
    "ready": {"queued", "failed", "missed", "processing", "published"},
    "scheduled": {"processing", "published", "locked_private", "uploaded", "failed", "needs_manual"},
    "uploaded": {"scheduled", "published", "locked_private", "failed"},
    "published": set(),                                   # final
    "locked_private": {"scheduled", "published", "uploaded", "failed", "needs_manual", "queued"},  # queued: Studio instead
    "failed": {"queued", "draft", "needs_manual", "cancelled"} | _PLATFORM_STATES,
    "missed": {"queued", "draft", "needs_manual", "failed", "cancelled"} | _PLATFORM_STATES,
    "needs_manual": {"queued", "draft", "missed", "failed", "cancelled"} | _PLATFORM_STATES,
    "cancelled": {"draft", "queued"},
}


class IllegalTransition(ValueError):
    pass


def check(old: str | None, new: str, *, item: Any = None) -> None:
    """Raise IllegalTransition (and log it) for a transition that isn't in TRANSITIONS, or for "published" without a
    platform media id. `old` None = a new row."""
    from clipforge.utils.log import log

    if new not in STATUSES:
        msg = f"unknown status {new!r}"
    elif old is None or old == new or old not in TRANSITIONS or new in TRANSITIONS[old]:
        msg = ""
        if new == "published" and item is not None and not _has_media(item):
            msg = "can't mark published without a platform media id"
    else:
        msg = f"illegal transition {old} → {new}"
    if msg:
        ident = f"queue item {getattr(item, 'id', '?')}" if item is not None else "queue item"
        log.error(f"[states] {ident}: {msg}")
        raise IllegalTransition(f"{ident}: {msg}")


def _has_media(it: Any) -> bool:
    if getattr(it, "youtube_video_id", "") or getattr(it, "ig_media_id", ""):
        return True
    # a manual (no-API) Instagram account: the Reel link you pasted is the only reference that can exist
    return getattr(it, "platform", "") == "instagram" and bool(getattr(it, "permalink", "")) and \
        str(getattr(it, "channel_id", "")).startswith("ig:manual:")


# code -> (short label, actions offered in the UI)
# actions: sync, link, open_batch, reschedule, publish_now, retry, reconnect, audit_info, studio, unbatch, open_app,
#          edit, confirm, switch_assisted, switch_auto
REASONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "assisted_batch": ("Assisted mode: upload this batch in YouTube Studio", ("open_batch", "sync", "link", "reschedule")),
    "assisted_unlinked": ("Uploaded in Studio? Not linked yet", ("sync", "link", "reschedule")),
    "assisted_no_batch": ("Assisted mode: not in a Studio batch, so nobody uploaded it", ("reschedule", "switch_auto")),
    "not_scheduled_in_studio": ("Uploaded, but no schedule set in Studio", ("studio", "sync")),
    "batched_in_api": ("In a Studio batch, so auto mode won't upload it (no double uploads)", ("unbatch", "sync", "link")),
    "slot_passed": ("The slot passed before the upload", ("reschedule", "publish_now")),
    "slot_too_close": ("Less than 15 min left when the upload could start", ("reschedule", "publish_now")),
    "interrupted": ("Upload was interrupted. Check Studio", ("studio", "sync", "link", "reschedule")),
    "login_expired": ("Login expired: reconnect the account", ("reconnect",)),
    "locked_private": ("Locked private by YouTube", ("audit_info", "studio", "studio_instead")),
    "quota": ("Upload quota used up: continues tomorrow automatically", ()),
    "retrying": ("Retrying automatically", ("retry",)),
    "upload_failed": ("Upload failed", ("retry", "reschedule")),
    "upload_limit": ("YouTube's upload limit for this channel is reached", ("retry", "studio_instead")),
    "rejected": ("YouTube rejected the video", ("studio",)),
    "deleted": ("Video not found on YouTube (deleted?)", ("reschedule",)),
    "metadata": ("Title/description need a fix", ("edit",)),
    "file_missing": ("Clip file missing: re-render it", ("open_app",)),
    "channel_unconfirmed": ("Confirm the channel before the first upload", ("confirm",)),
    "auto_unavailable": ("Auto mode can't upload for this account", ("reconnect", "switch_assisted")),
    "ig_container_failed": ("Instagram couldn't process the video", ("retry", "reschedule")),
    "ig_format": ("The video doesn't meet Instagram's Reels specs", ("open_app",)),
    "ig_missed": ("Instagram slot missed (laptop off or app closed at publish time)", ("reschedule", "publish_now")),
    "ig_limit": ("Instagram publishing limit reached: retrying later", ()),
    "ig_find_media": ("Instagram says it's published; looking for the Reel", ("link",)),
    "ig_assisted": ("Assisted mode: post it in the Instagram app", ("open_batch", "link", "reschedule")),
}


def label(reason: str) -> str:
    return REASONS.get(reason, ("", ()))[0]


def actions(reason: str) -> list[str]:
    return list(REASONS.get(reason, ("", ()))[1])


def canonical(it: Any) -> str:
    """Spec name, including the two sub-states of 'queued'."""
    if it.status == "queued":
        if it.reason == "login_expired":
            return "REQUIRES_REAUTHENTICATION"
        if it.next_attempt_at or it.reason == "retrying":
            return "RETRYING"
        if not it.slot_utc:
            return "READY"
    return CANONICAL.get(it.status, it.status.upper())


def _when(t: dt.datetime | None, zone: str) -> str:
    if not t:
        return "—"
    from clipforge.publish.timez import utc_to_local

    return utc_to_local(t, zone).strftime("%a %d %b, %H:%M")


def status_text(it: Any, zone: str = "Asia/Kolkata", mode: str = "", progress: float | None = None) -> dict[str, Any]:
    """{state, label, detail, actions} in plain words. Shared by the web app (queue.public) and Telegram.
    `mode` = resolved account mode (auto | assisted) when the caller knows it."""
    yt = it.platform != "instagram"
    where = "YouTube" if yt else "Instagram"
    slot = _when(it.slot_utc, zone) + (" IST" if zone == "Asia/Kolkata" else "")
    st, reason, err = it.status, it.reason or "", (it.error or "").strip()
    acts = actions(reason)
    if st == "draft":
        lab, det = "Draft", "Not scheduled yet."
    elif st == "queued":
        if reason == "login_expired":
            lab, det = "Needs re-login", err or "The account's login expired: Reconnect it on the Accounts page."
        elif reason == "quota":
            lab, det = "Waiting for quota", err or "Today's upload quota is used up; continues after midnight Pacific time."
        elif it.next_attempt_at or reason == "retrying":
            lab = f"Retrying automatically (attempt {it.attempts + 1})"
            det = f"Next try {_when(it.next_attempt_at, zone)}. Last error: {err or 'unknown'}"
            acts = acts or ["retry"]
        elif reason:
            lab, det = label(reason) or "Waiting", err
        elif not it.slot_utc:
            lab, det = "Ready", "Approved, no time picked yet."
        elif mode == "assisted":
            lab = "Planned (assisted)"
            det = (f"You upload it in YouTube Studio for {slot}, then Sync." if yt else
                   f"You post it in the Instagram app at {slot}, then paste the Reel link.")
        elif yt:
            lab, det = "Scheduled, not uploaded yet", f"ClipForge uploads it as private with YouTube publishing it at {slot}."
        else:
            lab, det = "Scheduled, not uploaded yet", (f"ClipForge uploads it ~20 min before {slot} and publishes it at "
                                                        "the slot (the app must be running then).")
    elif st == "uploading":
        pct = f" ({round(progress * 100)}%)" if progress is not None else ""
        lab, det = f"Uploading to {where}{pct}", "Sending the video file."
    elif st == "processing":
        lab, det = f"Uploaded, {where} is processing it", err or "Waiting for the platform to finish processing."
        if reason:
            lab = label(reason) or lab
    elif st == "ready":
        lab, det = "Uploaded, publishes at the slot", f"Instagram finished processing. ClipForge publishes it at {slot}."
        if reason:
            lab, det = label(reason) or lab, err or det
    elif st == "scheduled":
        lab, det = "Uploaded (private), goes public at the slot", f"On YouTube as private; YouTube makes it public at {slot}."
    elif st == "uploaded":
        lab, det = f"On YouTube as {it.privacy}", "Uploaded as you chose; it never goes public by itself."
    elif st == "published":
        if not yt and not it.ig_media_id:
            lab, det = "Posted by you", "Reel link pasted by you (not verified through the Instagram API)."
        else:
            lab, det = "Published", f"Live on {where} since {_when(it.published_at or it.slot_utc, zone)}."
    elif st == "locked_private":
        lab, det = "Uploaded but locked private", err or "YouTube kept it private; it won't go public by itself."
    elif st == "failed":
        lab, det = label(reason) or "Failed", err or "Unknown error."
        acts = acts or ["retry"]
    elif st == "missed":
        lab, det = label(reason) or "Missed slot", err
    elif st == "needs_manual":
        lab, det = label(reason) or "Needs you", err
    elif st == "cancelled":
        lab, det = "Cancelled", err
    else:
        lab, det = st, err
    return {"state": canonical(it), "label": lab, "detail": det, "actions": acts}
