"""In-memory stand-in for the YouTube Data/Analytics API (no network in tests)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path


class FakeYouTube:
    def __init__(self, channel_id: str = "UCtest", lock_private: bool = False, fail_uploads: int = 0) -> None:
        self.channel_id = channel_id
        self.lock_private = lock_private        # simulate the unverified-project lock
        self.fail_uploads = fail_uploads        # first N uploads raise
        self.store: dict[str, dict] = {}
        self.order: list[str] = []
        self.uploaded_bodies: list[dict] = []
        self.subscribers = 120
        self.analytics_rows: dict[str, list[list]] = {}
        self.process_after_reads = 1            # like YouTube: an upload is "uploaded/processing" for a while
        self._reads: dict[str, int] = {}

    # ---- helpers for tests
    def add_video(self, vid: str, title: str, seconds: int, privacy: str = "public", publish_at: str | None = None,
                  published_at: str = "2026-11-01T13:00:00Z", views: int = 0, file_name: str = "") -> None:
        st = {"privacyStatus": privacy, "uploadStatus": "processed"}
        if publish_at:
            st["publishAt"] = publish_at
        self.store[vid] = {"id": vid, "snippet": {"title": title, "publishedAt": published_at},
                           "status": st, "contentDetails": {"duration": f"PT{seconds}S"},
                           "statistics": {"viewCount": str(views), "likeCount": "3", "commentCount": "1"},
                           "fileDetails": {"fileName": file_name}, "processingDetails": {"processingStatus": "succeeded"}}
        self.order.insert(0, vid)

    # ---- API surface used by ClipForge
    def my_channels(self) -> list[dict]:
        return [{"id": self.channel_id, "title": "Test Channel", "handle": "@test", "thumbnail": "",
                 "uploads_playlist": "UU" + self.channel_id[2:], "subscribers": self.subscribers}]

    def upload(self, path: Path, body: dict, on_progress=None) -> str:
        assert Path(path).exists(), path
        if self.fail_uploads:
            self.fail_uploads -= 1
            raise RuntimeError("network blip")
        self.uploaded_bodies.append(body)
        vid = f"yt{len(self.uploaded_bodies)}"
        st = dict(body["status"])
        if self.lock_private:
            st.pop("publishAt", None)
        self.store[vid] = {"id": vid, "snippet": {"title": body["snippet"]["title"], "publishedAt": "2026-11-04T00:00:00Z"},
                           "status": {**st, "uploadStatus": "uploaded"}, "contentDetails": {"duration": "PT30S"},
                           "statistics": {"viewCount": "0"}, "fileDetails": {"fileName": Path(path).name},
                           "processingDetails": {"processingStatus": "processing"}}
        self.order.insert(0, vid)
        if on_progress:
            on_progress(1.0)
        return vid

    def publish_due(self, now: dt.datetime) -> None:
        """What YouTube does at publishAt."""
        for v in self.store.values():
            pa = v["status"].get("publishAt")
            if v["status"]["privacyStatus"] == "private" and pa and dt.datetime.fromisoformat(pa.replace("Z", "+00:00")) <= now:
                v["status"]["privacyStatus"] = "public"
                v["snippet"]["publishedAt"] = pa

    def videos(self, ids: list[str]) -> list[dict]:
        out = []
        for i in ids:
            v = self.store.get(i)
            if not v:
                continue
            out.append(v)
            if v["status"].get("uploadStatus") == "uploaded":  # finishes processing after N reads
                self._reads[i] = self._reads.get(i, 0) + 1
                if self._reads[i] > self.process_after_reads:
                    v["status"]["uploadStatus"] = "processed"
                    v["processingDetails"]["processingStatus"] = "succeeded"
                    out[-1] = v
        return out

    def uploads(self, playlist_id: str, max_items: int = 100) -> list[str]:
        return self.order[:max_items]

    def channel_stats(self) -> dict:
        return {"subscribers": self.subscribers, "views": 1000, "videos": len(self.store)}

    def analytics(self, start, end, metrics, dimensions=None, filters=None) -> list[list]:
        if dimensions == "creatorContentType":
            return [["SHORTS", 500_000], ["VIDEO_ON_DEMAND", 10]]
        if dimensions == "day":
            return [[start, 100], ["x2", 50], ["x3", 25]]
        return [[55.5, 2]]
