"""Second half of scripts/deploy_e2e.py: run ON THE SERVER's config — queue the job's clips on a fake YouTube channel,
let the server's scheduler upload them (private + publishAt), then let the fake YouTube publish them at the slot.
Usage: CLIPFORGE_CONFIG=… CLIPFORGE_ENV=… python scripts/deploy_e2e_publish.py <job id>"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clipforge.api import db  # noqa: E402
from clipforge.config import load_config  # noqa: E402
from clipforge.publish import queue as q, runner  # noqa: E402
from clipforge.publish import youtube as yt  # noqa: E402
from tests.fake_youtube import FakeYouTube  # noqa: E402


def main(job_id: int) -> int:
    cfg = load_config().with_overrides({"publishing.mode": "api"})
    job = db.get_job(job_id)
    assert job and job.video_id, "no such job"
    fake = FakeYouTube("UCe2e")
    yt.set_client_factory(lambda cid, c: fake)
    with db.session() as s:
        if not s.get(db.Channel, "UCe2e"):
            s.add(db.Channel(id="UCe2e", title="E2E Channel", uploads_playlist="UUe2e", confirmed=True))
            s.commit()
    from clipforge.services.clips import approve_above

    print(f"approved {approve_above(job_id, 0)} clips (like tapping ✅ in Telegram)")
    added = q.add_clips("UCe2e", job.video_id, None, cfg)
    for it in added:
        q.generate_metadata(it.id, cfg)
    sched = q.autoschedule("UCe2e", [it.id for it in added], cfg)
    print(f"queued {len(added)} clips, scheduled {len(sched)}")
    now = dt.datetime.now(dt.timezone.utc)
    rep = runner.run_once(cfg, now=now, source="e2e")
    print(f"scheduler run: uploaded={len(rep.uploaded)} failed={len(rep.failed)} {'; '.join(rep.messages)}")
    assert rep.uploaded and not rep.failed
    for b in fake.uploaded_bodies:
        assert b["status"]["privacyStatus"] == "private" and b["status"].get("publishAt"), b["status"]
    print("every upload: privacyStatus=private + publishAt (nothing public before its slot)")
    later = now + dt.timedelta(days=4)
    fake.publish_due(later)
    rep2 = runner.run_once(cfg, now=later, source="e2e")
    print(f"after the slots: published={len(rep2.published)}")
    assert len(rep2.published) == len(rep.uploaded)
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1])))
