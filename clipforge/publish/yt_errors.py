"""One table for every Google (YouTube Data API) error → what ClipForge does. Uses the real `reason` field.

Types (Publishing v2, DECISIONS → "Publishing v2"):
  project_quota  the API project's daily quota is used up      → wait for the next Pacific day, no retries
  channel_limit  the channel's own upload limit (rolling 24 h) → channel cooldown, then ONE probe upload
  auth           login refused / revoked / missing scope        → account REQUIRES_REAUTHENTICATION
  temporary      5xx, rate limits, network                      → exponential backoff
  permanent      bad request, forbidden, invalid metadata       → FAILED, never retried
Reasons checked against developers.google.com/youtube/v3/docs/videos/insert#errors and /docs/errors (2026-11-05).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

PROJECT_QUOTA, CHANNEL_LIMIT, AUTH, TEMPORARY, PERMANENT = "project_quota", "channel_limit", "auth", "temporary", "permanent"

REASON_TYPE: dict[str, str] = {
    "quotaExceeded": PROJECT_QUOTA, "dailyLimitExceeded": PROJECT_QUOTA,
    "uploadLimitExceeded": CHANNEL_LIMIT,
    "authError": AUTH, "insufficientPermissions": AUTH, "invalid_grant": AUTH, "unauthorized": AUTH,
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": AUTH, "youtubeSignupRequired": AUTH,
    "rateLimitExceeded": TEMPORARY, "userRateLimitExceeded": TEMPORARY, "backendError": TEMPORARY,
    "internalError": TEMPORARY, "serviceUnavailable": TEMPORARY, "transientError": TEMPORARY,
    "forbidden": PERMANENT, "forbiddenLicenseSetting": PERMANENT, "forbiddenPrivacySetting": PERMANENT,
    "mediaBodyRequired": PERMANENT, "invalidTitle": PERMANENT, "invalidDescription": PERMANENT,
    "invalidTags": PERMANENT, "invalidCategoryId": PERMANENT, "invalidPublishAt": PERMANENT,
    "invalidFilename": PERMANENT, "invalidVideoMetadata": PERMANENT, "defaultLanguageNotSet": PERMANENT,
    "invalidRecordingDetails": PERMANENT, "invalidVideoGameRating": PERMANENT, "badRequest": PERMANENT,
}


@dataclass
class GError:
    type: str
    reason: str
    status: int | None
    message: str

    def text(self) -> str:
        return f"{self.status or ''} {self.reason}: {self.message}".strip()


def classify(exc: BaseException) -> GError:
    from clipforge.publish import youtube as yt

    if isinstance(exc, yt.NeedsReauth):
        return GError(AUTH, "invalid_grant", None, str(exc))
    if isinstance(exc, yt.QuotaExceeded):  # our own ledger says the project quota is used up
        return GError(PROJECT_QUOTA, "quotaExceeded", None, str(exc))
    try:
        from google.auth.exceptions import RefreshError

        if isinstance(exc, RefreshError):
            return GError(AUTH, "invalid_grant", None, str(exc)[:300])
    except ImportError:  # pragma: no cover
        pass
    status, reason, message = None, "", str(exc)[:300]
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            status = int(getattr(exc.resp, "status", 0) or 0) or None
            try:
                err = json.loads(exc.content.decode("utf-8", "replace")).get("error", {})
                reasons = [e.get("reason", "") for e in err.get("errors", []) if e.get("reason")]
                reason = reasons[0] if reasons else str(err.get("status") or "")
                message = err.get("message", message)
            except (ValueError, AttributeError):
                pass
    except ImportError:  # pragma: no cover
        pass
    if reason in REASON_TYPE:
        return GError(REASON_TYPE[reason], reason, status, message)
    if status is None:  # not an HTTP answer: network / socket / timeout
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)) or "timed out" in message.lower():
            return GError(TEMPORARY, type(exc).__name__, None, message)
        return GError(TEMPORARY, type(exc).__name__, None, message)
    if status == 401:
        return GError(AUTH, reason or "unauthorized", status, message)
    if status >= 500 or status in (408, 429):
        return GError(TEMPORARY, reason or f"http{status}", status, message)
    return GError(PERMANENT, reason or f"http{status}", status, message)
