"""Clear a day / a week, and Sync noticing videos deleted in YouTube Studio right away (2026-11-10)."""

from __future__ import annotations

from tests.test_publish import NOW, _one_channel_two_items, env  # noqa: F401


def _day(it, cfg):
    from clipforge.publish.timez import as_utc, utc_to_local

    return utc_to_local(as_utc(it.slot_utc), cfg.publishing.timezone).date().isoformat()


def test_clear_day_removes_planned_and_lists_uploaded(env):  # noqa: F811
    from clipforge.publish import queue as q, runner
    from clipforge.services import publishing as pub

    cfg, fake, (a, b) = _one_channel_two_items(env)
    first, second = sorted([a, b], key=lambda x: x.slot_utc)
    runner.run_once(cfg, now=NOW)        # API mode uploads queued clips straight away
    up = q.get(first.id)
    assert up.youtube_video_id
    with __import__("clipforge.api.db", fromlist=["db"]).session() as s:  # make the 2nd one "not uploaded yet"
        row = s.get(type(up), second.id)
        row.status, row.youtube_video_id = "queued", ""
        s.add(row)
        s.commit()
    days = sorted({_day(first, cfg), _day(second, cfg)})
    res = pub.clear_range(days[0], 7)
    assert second.id in res["removed"] and q.get(second.id) is None
    assert [o["id"] for o in res["on_platform"]] == [first.id] and "studio.youtube.com" in res["on_platform"][0]["url"]
    assert q.get(first.id)                # still tracked: YouTube would post it anyway


def test_sync_removes_clip_deleted_in_studio_before_its_slot(env):  # noqa: F811
    from clipforge.publish import queue as q, reconcile, runner

    cfg, fake, (a, b) = _one_channel_two_items(env)
    runner.run_once(cfg, now=NOW)
    a, b = q.get(a.id), q.get(b.id)
    assert a.youtube_video_id and b.youtube_video_id
    fake.store.pop(a.youtube_video_id)    # user deleted it in Studio; its slot is still in the future
    fake.order.remove(a.youtube_video_id)
    res = reconcile.sync("UCtest", cfg)
    assert [r["queue_id"] for r in res["removed"]] == [a.id]
    assert q.get(a.id) is None and q.get(b.id)



def test_all_deleted_in_studio_are_removed_but_not_on_an_empty_answer(env):  # noqa: F811
    from clipforge.publish import queue as q, reconcile, runner

    cfg, fake, (a, b) = _one_channel_two_items(env)
    runner.run_once(cfg, now=NOW)
    ids = [q.get(a.id).youtube_video_id, q.get(b.id).youtube_video_id]
    saved = dict(fake.store), list(fake.order)
    fake.store.clear()
    fake.order.clear()                     # API answers nothing at all → keep everything
    assert reconcile.drop_deleted("UCtest", fake) == [] and q.get(a.id) and q.get(b.id)
    fake.store, fake.order = saved         # the channel answers, but both videos were deleted in Studio
    for v in ids:
        fake.store.pop(v)
        fake.order.remove(v)
    fake.add_video("other1", "Some other upload", 30)
    assert len(reconcile.drop_deleted("UCtest", fake)) == 2 and q.get(a.id) is None and q.get(b.id) is None
