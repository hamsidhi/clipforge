"""Queue item state machine (both platforms). Every state that needs the user carries a `reason` code; the UI turns
it into a sentence + one-click fixes, so nothing is ever a bare "Needs you".

    draft ─► queued ─► uploading ─► scheduled ─► published          (YouTube: private + publishAt, YouTube publishes)
                │          │            └──────► locked_private     (YouTube kept it private and dropped the schedule)
                │          └──► failed (retryable: Retry)
                ├──► needs_manual   (assisted mode only: you upload in Studio / Instagram app, then Sync or Link)
                └──► missed         (the slot passed before the upload / publish; Reschedule or Publish now)
    uploaded = on YouTube as private/unlisted by your choice (never goes public by itself)
    Instagram: queued ─► uploading (container uploaded ~20 min early) ─► ready ─► published   (publish at the slot)
"""

from __future__ import annotations

STATUSES = ("draft", "queued", "uploading", "ready", "scheduled", "uploaded", "published", "failed",
            "locked_private", "needs_manual", "missed")
ATTENTION = ("failed", "locked_private", "needs_manual", "missed")

# code -> (short label, actions offered in the UI)
# actions: sync, link, open_batch, reschedule, publish_now, retry, reconnect, audit_info, studio, unbatch, open_app
REASONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "assisted_batch": ("Assisted mode: upload this batch in YouTube Studio", ("open_batch", "sync", "link", "reschedule")),
    "assisted_unlinked": ("Uploaded in Studio? Not linked yet", ("sync", "link", "reschedule")),
    "assisted_no_batch": ("Assisted mode: not in a Studio batch, so nobody uploaded it", ("reschedule",)),
    "not_scheduled_in_studio": ("Uploaded, but no schedule set in Studio", ("studio", "sync")),
    "batched_in_api": ("In a Studio batch, so API mode won't upload it (no double uploads)", ("unbatch", "sync", "link")),
    "slot_passed": ("The slot passed before the upload", ("reschedule", "publish_now")),
    "slot_too_close": ("Less than 15 min left when the upload could start", ("reschedule", "publish_now")),
    "interrupted": ("Upload was interrupted. Check Studio", ("studio", "sync", "link", "reschedule")),
    "login_expired": ("Login expired", ("reconnect",)),
    "locked_private": ("Locked private by YouTube", ("audit_info", "studio")),
    "quota": ("Upload quota used up: continues tomorrow automatically", ()),
    "upload_failed": ("Upload failed", ("retry", "reschedule")),
    "rejected": ("YouTube rejected the video", ("studio",)),
    "deleted": ("Video not found on YouTube (deleted?)", ("reschedule",)),
    "metadata": ("Title/description need a fix", ("edit",)),
    "file_missing": ("Clip file missing: re-render it", ("open_app",)),
    "channel_unconfirmed": ("Confirm the channel before the first upload", ("confirm",)),
    "ig_container_failed": ("Instagram couldn't process the video", ("retry", "reschedule")),
    "ig_missed": ("Instagram slot missed (laptop off or app closed at publish time)", ("reschedule", "publish_now")),
    "ig_limit": ("Instagram publishing limit reached: retrying later", ()),
    "ig_assisted": ("Assisted mode: post it in the Instagram app", ("open_batch", "link", "reschedule")),
}


def label(reason: str) -> str:
    return REASONS.get(reason, ("", ()))[0]


def actions(reason: str) -> list[str]:
    return list(REASONS.get(reason, ("", ()))[1])
