"""2026-11-09 remote fix: Instagram login from the phone (Tailscale redirect URI, token paste, public callback) and
the YouTube reach report (bucketing, funnel, patterns, Markdown/Telegram). Fakes only — no network."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from clipforge.publish.timez import UTC
from tests.test_instagram import _schedule_ig, env  # noqa: F401  (fixture)
from tests.test_publish import NOW

TS = "https://laptop.tail123.ts.net"


@pytest.fixture
def ts(monkeypatch):
    from clipforge.publish import instagram as ig

    monkeypatch.setattr(ig, "public_base", lambda cfg=None: TS)
    return TS


# ----------------------------------------------------------------------------- redirect URI
def test_redirect_uri_local_vs_remote(env, ts):  # noqa: F811
    from clipforge.publish import instagram as ig

    assert ig.redirect_uri(8000) == "http://localhost:8000/api/instagram/oauth/callback"
    assert ig.redirect_uri(8000, "localhost") == "http://localhost:8000/api/instagram/oauth/callback"
    assert ig.redirect_uri(None, "laptop.tail123.ts.net") == TS + "/api/instagram/oauth/callback"
    assert ig.redirect_uris(8000) == ["http://localhost:8000/api/instagram/oauth/callback",
                                      TS + "/api/instagram/oauth/callback"]


def test_public_base_from_config_wins(env, monkeypatch):  # noqa: F811
    from clipforge.config import load_config
    from clipforge.publish import instagram as ig

    cfg = load_config().with_overrides({"instagram.public_url": "https://x.example.ts.net/"})
    assert ig.public_base(cfg) == "https://x.example.ts.net"
    monkeypatch.setattr(ig, "public_base", lambda cfg=None: "")
    with pytest.raises(ValueError, match="public_url"):
        ig.redirect_uri(None, "phone-host")


REMOTE = {"X-Forwarded-For": "100.101.102.103", "Host": "laptop.tail123.ts.net"}


@pytest.fixture
def phone(env, ts, tmp_path, monkeypatch):  # noqa: F811
    """(app, signed-in phone client, CSRF headers): the real login over the tailnet host."""
    from fastapi.testclient import TestClient

    import clipforge.config as config_mod
    from clipforge.api.server import create_app

    env_file = tmp_path / ".env"
    monkeypatch.setattr(config_mod, "ENV_PATH", env_file)
    monkeypatch.setitem(config_mod.Secrets.model_config, "env_file", str(env_file))
    app = create_app()
    app.state.publish_ticker = False
    with TestClient(app) as local:
        assert local.post("/api/auth/setup", json={"password": "correct horse battery"}).status_code == 200
        c = TestClient(app, headers=REMOTE)
        assert c.post("/api/auth/login", json={"password": "correct horse battery"}).status_code == 200
        yield app, c, {"X-CSRF-Token": c.cookies["cf_csrf"]}


def test_oauth_start_from_tailnet_uses_https_redirect_and_callback_is_public(phone):
    from fastapi.testclient import TestClient

    from clipforge.publish import instagram as ig

    app, c, h = phone
    r = c.post("/api/instagram/oauth/start", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["redirect_uri"] == TS + "/api/instagram/oauth/callback"
    assert "redirect_uri=https%3A%2F%2Flaptop.tail123.ts.net%2Fapi%2Finstagram%2Foauth%2Fcallback" in r.json()["url"]
    assert ig.attempts()[-1]["redirect_uri"] == TS + "/api/instagram/oauth/callback"
    other = TestClient(app, headers={**REMOTE, "Host": "other.example"})
    other.cookies = c.cookies
    r = other.post("/api/instagram/oauth/start", headers=h)
    assert r.status_code == 400 and "Connect with a token" in r.json()["detail"]
    # Meta's redirect arrives WITHOUT the session cookie (SameSite=Strict): the callback still answers, but does
    # nothing without a valid one-time state
    anon = TestClient(app, headers=REMOTE)
    r = anon.get("/api/instagram/oauth/callback?code=x&state=forged", follow_redirects=False)
    assert r.status_code == 400 and "expired" in r.text
    r = anon.get("/api/instagram/oauth/callback?error=access_denied&error_reason=user_denied"
                 "&error_description=Permissions+error", follow_redirects=False)
    assert r.status_code in (302, 307) and "Permissions" in r.headers["location"]
    assert anon.get("/api/instagram/attempts").status_code == 401  # everything else still needs a login
    rows = ig.attempts()
    assert rows[-1]["error_reason"] == "user_denied" and rows[-2]["result"] == "failed"
    assert all("access_token" not in json.dumps(x) for x in rows)
    # a real flow started on the phone finishes through the public callback
    state = [k for k, (uri, _) in ig._flows.items() if uri.startswith(TS)]
    ig._flows[state[0]] = (TS + "/api/instagram/oauth/callback", 0)
    r = anon.get(f"/api/instagram/oauth/callback?code=good-code&state={state[0]}", follow_redirects=False)
    assert r.status_code in (302, 307) and "ig_pick=" in r.headers["location"]


# ----------------------------------------------------------------------------- token paste
def _manual_with_history():
    from clipforge.api import db
    from clipforge.publish import channels as chs

    ch = chs.add_manual_instagram("figuring.clips")
    chs.update(ch.id, confirmed=True)
    items = _schedule_ig(ch.id, n=3)
    with db.session() as s:  # one Reel already posted by hand
        it = s.get(db.QueueItem, items[0].id)
        it.status, it.ig_media_id, it.permalink = "published", "m1", "https://instagram.com/reel/x"
        s.add(it)
        s.commit()
    return ch, [i.id for i in items]


def test_token_paste_converts_manual_account_in_place_to_auto(env, ts):  # noqa: F811
    from clipforge.publish import channels as chs
    from clipforge.publish import instagram as ig
    from clipforge.publish import queue as q

    fake, _ = env
    ch, ids = _manual_with_history()
    res = ig.connect_with_token("u" * 60, prefer_username="figuring.clips", now=NOW)
    assert res["channel_id"] == ch.id == "ig:manual:figuring.clips"  # same row: queue + history stay attached
    new = chs.get(ch.id)
    assert not ig.is_manual(new) and new.extra["ig_user_id"] == "1789" and new.extra["converted_from_manual"]
    assert new.settings["mode"] == "auto" and new.confirmed
    assert [q.get(i).channel_id for i in ids] == [ch.id] * 3 and q.get(ids[0]).status == "published"
    tok = ig.load_tokens("1789")
    assert tok["page_token"] == "page-token" and tok["user_token"].startswith("long-token")  # exchanged server-side
    el = ig.eligibility(new, live=True)
    assert el["auto_ready"], el["summary"]


def test_token_paste_rejects_bad_tokens(env, ts):  # noqa: F811
    from clipforge.publish import instagram as ig

    fake, _ = env
    with pytest.raises(ValueError, match="doesn't look like"):
        ig.connect_with_token("short")
    fake.token_valid = False
    with pytest.raises(ValueError, match="not valid"):
        ig.connect_with_token("u" * 60)
    fake.token_valid = True
    orig = fake.debug_token
    fake.debug_token = lambda t: {**orig(t), "type": "PAGE"}
    with pytest.raises(ValueError, match="User Access Token"):
        ig.connect_with_token("u" * 60)


def test_token_paste_missing_permission_stays_assisted(env, ts):  # noqa: F811
    from clipforge.publish import channels as chs
    from clipforge.publish import instagram as ig

    fake, _ = env
    ch, _ = _manual_with_history()
    orig = fake.permissions
    fake.permissions = lambda t: [p for p in orig(t) if p["permission"] != "instagram_content_publish"]
    res = ig.connect_with_token("u" * 60, prefer_username="figuring.clips", now=NOW)
    assert chs.get(res["channel_id"]).settings["mode"] == "assisted"
    assert "instagram_content_publish" in ig.eligibility(chs.get(ch.id))["summary"]


def test_token_route_from_phone(phone):
    app, c, h = phone
    _manual_with_history()
    assert c.post("/api/instagram/token", json={"token": "u" * 60}).status_code == 403  # CSRF still required
    r = c.post("/api/instagram/token", json={"token": "u" * 60}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["channel_id"] == "ig:manual:figuring.clips" and r.json()["eligibility"]["auto_ready"]
    info = c.get("/api/instagram/app").json()
    assert info["redirect_uris"][-1] == TS + "/api/instagram/oauth/callback" and info["token_steps"]
    assert c.get("/api/publishing/reach").json() == {"available": False}


def test_container_test_never_publishes_unless_allowed(env, ts, tmp_path):  # noqa: F811
    from clipforge.config import load_config
    from clipforge.publish import channels as chs
    from clipforge.publish import instagram as ig

    fake, _ = env
    _manual_with_history()
    ch = chs.get(ig.connect_with_token("u" * 60, prefer_username="figuring.clips", now=NOW)["channel_id"])
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x" * 1000)
    res = ig.container_test(ch, clip, sleep=lambda s: None)
    assert res["ok"] and res["status_code"] == "FINISHED" and not res["published"] and not fake.published
    cfg = load_config().with_overrides({"instagram.test_publish_ok": True})
    res = ig.container_test(ch, clip, cfg=cfg, sleep=lambda s: None)
    assert res["published"] and len(fake.published) == 1


# ----------------------------------------------------------------------------- reach report
def _video(vid, privacy="public", views=0, published="2026-11-04T05:00:00Z", publish_at=None, dur="PT22S", title="t",
           upload="processed", mfk=False):
    st = {"privacyStatus": privacy, "uploadStatus": upload, "license": "youtube", "madeForKids": mfk}
    if publish_at:
        st["publishAt"] = publish_at
    return {"id": vid, "snippet": {"title": title, "publishedAt": published}, "status": st,
            "statistics": {"viewCount": str(views), "likeCount": "1"}, "contentDetails": {"duration": dur}}


def _raw():
    h = ["video", "views", "engagedViews", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
         "likes", "comments", "shares", "subscribersGained"]
    return {
        "collected_at": "2026-11-09T00:00:00+00:00", "channel": {"subscribers": 1, "views": 300, "videos": 6},
        "videos": [
            _video("tested", views=75, dur="PT58S", title="Biggest Threat"),
            _video("working", views=120, title="Sam Altman on fear"),
            _video("quiet", views=3, title="Life's Crazy: X"),
            _video("new", views=0, published="2026-11-08T12:00:00Z"),
            _video("sched", privacy="private", publish_at="2026-11-10T07:00:00Z", published="2026-11-07T12:00:00Z"),
            _video("locked", privacy="private", publish_at="2026-11-05T07:00:00Z"),
        ],
        "by_video": {"headers": h, "rows": [["tested", 65, 43, 13, 18, 32.6, 3, 0, 0, 0],
                                            ["working", 110, 80, 20, 20, 95.0, 5, 1, 1, 1]]},
        "traffic": {"headers": ["insightTrafficSourceType", "views"], "rows": [["SHORTS", 50], ["YT_SEARCH", 73], ["YT_CHANNEL", 27]]},
        "by_day": {"headers": ["day", "views"], "rows": [["2026-11-04", 60], ["2026-11-05", 56], ["2026-11-06", 0]]},
        "traffic_per_video": {"tested": [["SHORTS", 49], ["YT_SEARCH", 11]], "working": [["SHORTS", 30]], "quiet": [["YT_SEARCH", 3]]},
        "errors": {},
    }


def test_reach_buckets_and_funnel():
    from clipforge.publish import reach

    rep = reach.build(_raw(), {"tested": {"topic": "resilience", "episode": "Huberman"}})
    b = {v["id"]: v["bucket"] for v in rep["videos"]}
    assert b == {"tested": "tested_rejected", "working": "working", "quiet": "no_test_traffic", "new": "too_new",
                 "sched": "scheduled", "locked": "invisible"}
    f = rep["funnel"]
    assert f["uploaded"] == 6 and f["public"] == 4 and f["scheduled"] == 1 and f["invisible"] == 1
    assert f["with_feed_traffic"] == 2 and f["feed_views"] == 79
    assert f["views_per_short"]["n"] == 3 and f["views_per_short"]["median"] == 75
    assert f["traffic_share"]["YT_SEARCH"] == pytest.approx(0.49, abs=0.01) and f["analytics_through"] == "2026-11-05"
    assert rep["checks"]["private_past_publish_time"] == ["locked"]
    lp = {g["group"]: g for g in rep["patterns"]["length_vs_view_pct"]}
    assert lp[">45 s"]["median"] == 32.6 and not lp[">45 s"]["enough"]
    assert {g["group"] for g in rep["patterns"]["guest_name_in_title_vs_views"]} == {"True", "False"}


def test_reach_bucket_edge_cases():
    from clipforge.publish import reach

    now = dt.datetime(2026, 11, 9, tzinfo=UTC)
    v = {"status": {"privacyStatus": "public", "madeForKids": True}, "views": 999}
    assert reach.bucket_of(v, now) == "invisible"
    v = {"status": {"privacyStatus": "public", "uploadStatus": "rejected"}}
    assert reach.bucket_of(v, now) == "invisible"
    v = {"status": {"privacyStatus": "public"}, "public_at": now - dt.timedelta(hours=72), "views": 10, "feed_views": 25,
         "avg_view_pct": 90}
    assert reach.bucket_of(v, now) == "working"  # enough feed views count as a test even with few lifetime views
    assert reach._dur("PT1M5S") == 65 and reach._dur("PT22S") == 22


def test_reach_markdown_and_telegram():
    from clipforge.publish import reach

    rep = reach.build(_raw())
    md = reach.render_md(rep, findings=["one"], changes=["two"])
    assert "## Funnel" in md and "## Top findings" in md and "Restrictions" in md and "| tested |" in md
    tg = reach.telegram_summary(rep)
    assert "Shorts feed tested 2/4" in tg and "median 75" in tg


def test_reach_rules_are_flagged_and_keep_selection_fingerprint(env):  # noqa: F811
    from clipforge.config import load_config
    from clipforge.stages import select_clips as sc

    cfg = load_config()
    t = sc.load_prompt()
    assert sc.HOOK_RULE in sc.with_reach_rules(t, cfg, "select")
    off = cfg.with_overrides({"reach.hook_rule": False, "reach.title_rules": False})
    assert sc.with_reach_rules(t, off, "select") == t
    tt = sc.load_prompt("clip_titles.md")
    assert sc.TITLE_RULES in sc.with_reach_rules(tt, cfg, "titles")
    assert sc.with_reach_rules(tt, cfg, "titles").format(podcast_title="p", channel="c", n=1, clips="x")
    # the rules are not in select:/clips: or the prompt files → finished selections stay valid
    assert "hook_rule" not in cfg.select.model_dump() and "EXTRA RULE" not in t


def test_reach_weekly_due(env, tmp_path):  # noqa: F811
    from clipforge.config import load_config
    from clipforge.publish import reach

    cfg = load_config()
    now = dt.datetime(2026, 11, 9, tzinfo=UTC)
    assert reach.weekly_due(cfg, now)
    (reach.reports_dir(cfg) / "reach_latest.json").write_text(json.dumps({"generated_at": "2026-11-05T00:00:00+00:00"}))
    assert not reach.weekly_due(cfg, now) and reach.weekly_due(cfg, now + dt.timedelta(days=3))
    assert not reach.weekly_due(cfg.with_overrides({"reach.weekly_report": False}), now + dt.timedelta(days=30))
