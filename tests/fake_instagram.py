"""In-memory stand-in for the Instagram Graph API (no network in tests)."""

from __future__ import annotations

from pathlib import Path

from clipforge.publish.instagram import IGError


class FakeInstagram:
    def __init__(self, ig_id: str = "1789", username: str = "figuring.clips") -> None:
        self.ig_id, self.username = ig_id, username
        self.containers: dict[str, dict] = {}
        self.published: list[dict] = []
        self.media_store: dict[str, dict] = {}
        self.fail_upload_once_at: int | None = None  # simulate a dropped connection after N bytes
        self.process_ticks = 1                       # status polls before FINISHED
        self.error_containers = False
        self.quota_usage = 0
        self.refreshed = 0
        self.revoked: list[str] = []
        self.calls: list[str] = []
        self.auth_broken = False
        self.lose_publish_response = False
        self.upload_crash_at: int | None = None  # simulate the process dying mid-upload

    def _auth(self, token: str) -> None:
        if self.auth_broken or not token:
            raise IGError("Meta API error 190: Error validating access token", code=190, auth=True)

    # ---- OAuth / accounts
    def exchange_code(self, code: str, redirect_uri: str) -> str:
        assert code == "good-code", code
        return "short-token"

    def long_lived(self, user_token: str) -> tuple[str, int]:
        self.refreshed += 1
        return f"long-token-{self.refreshed}", 60 * 86400

    def pages(self, user_token: str) -> list[dict]:
        self._auth(user_token)
        return [
            {"id": "page1", "name": "Figuring Clips Page", "access_token": "page-token",
             "instagram_business_account": {"id": self.ig_id, "username": self.username, "profile_picture_url": "",
                                            "followers_count": 321}},
            {"id": "page2", "name": "Page without Instagram", "access_token": "page-token-2"},
        ]

    def account(self, ig_id: str, token: str) -> dict:
        self._auth(token)
        return {"id": ig_id, "username": self.username, "followers_count": 400, "media_count": len(self.published)}

    # ---- publishing
    def create_reel(self, ig_id: str, token: str, caption: str, share_to_feed: bool, thumb_offset: int | None) -> dict:
        self._auth(token)
        self.calls.append("create")
        cid = f"c{len(self.containers) + 1}"
        self.containers[cid] = {"caption": caption, "share_to_feed": share_to_feed, "thumb_offset": thumb_offset,
                                "received": 0, "size": None, "polls": 0, "status": "IN_PROGRESS"}
        return {"id": cid, "uri": f"https://rupload.facebook.com/ig-api-upload/v26.0/{cid}"}

    def upload(self, container_id: str, token: str, path: Path, offset: int = 0) -> None:
        self._auth(token)
        c = self.containers[container_id]
        size = Path(path).stat().st_size
        c["size"] = size
        assert offset == c["received"], (offset, c["received"])
        if self.upload_crash_at is not None and offset < self.upload_crash_at:
            c["received"] = self.upload_crash_at
            self.upload_crash_at = None
            raise KeyboardInterrupt  # the process dies
        if self.fail_upload_once_at is not None and offset < self.fail_upload_once_at:
            c["received"] = self.fail_upload_once_at
            self.fail_upload_once_at = None
            raise IGError("connection reset", transient=True)
        c["received"] = size
        self.calls.append(f"upload@{offset}")

    def upload_offset(self, container_id: str, token: str) -> int:
        return self.containers[container_id]["received"]

    def container_status(self, container_id: str, token: str) -> dict:
        self._auth(token)
        c = self.containers[container_id]
        if c["status"] in ("EXPIRED", "PUBLISHED"):
            return {"status_code": c["status"]}
        if self.error_containers:
            return {"status_code": "ERROR", "status": "Error: video too short"}
        if c["received"] != c["size"]:
            return {"status_code": "IN_PROGRESS"}
        c["polls"] += 1
        c["status"] = "FINISHED" if c["polls"] >= self.process_ticks else "IN_PROGRESS"
        return {"status_code": c["status"]}

    def publish(self, ig_id: str, token: str, creation_id: str) -> str:
        self._auth(token)
        c = self.containers[creation_id]
        assert c["status"] == "FINISHED", c
        c["status"] = "PUBLISHED"
        mid = f"m{len(self.published) + 1}"
        self.published.append({"id": mid, "caption": c["caption"]})
        self.media_store[mid] = {"id": mid, "permalink": f"https://www.instagram.com/reel/{mid}/", "caption": c["caption"],
                                 "timestamp": "2026-11-04T10:00:00+0000"}
        if self.lose_publish_response:  # Meta published it, but the answer never arrived
            self.lose_publish_response = False
            raise IGError("network: read timeout", transient=True)
        self.quota_usage += 1
        return mid

    def media(self, media_id: str, token: str) -> dict:
        return self.media_store[media_id]

    def recent_media(self, ig_id: str, token: str, limit: int = 50) -> list[dict]:
        return list(self.media_store.values())[:limit]

    def publishing_limit(self, ig_id: str, token: str) -> dict:
        return {"quota_usage": self.quota_usage, "config": {"quota_total": 100, "quota_duration": 86400}}

    def insights(self, media_id: str, token: str) -> dict:
        return {"views": 1500, "reach": 1200, "likes": 80, "comments": 6, "shares": 9, "saved": 14}

    granted = ["instagram_basic", "instagram_content_publish", "pages_read_engagement", "pages_show_list",
               "business_management", "instagram_manage_insights"]
    token_valid = True

    def permissions(self, user_token: str) -> list[dict]:
        self._auth(user_token)
        return [{"permission": p, "status": "granted"} for p in self.granted] + [
            {"permission": "pages_manage_posts", "status": "declined"}]

    def debug_token(self, token: str) -> dict:
        return {"is_valid": self.token_valid, "type": "USER", "expires_at": 0}

    def revoke(self, user_token: str) -> None:
        self.revoked.append(user_token)
