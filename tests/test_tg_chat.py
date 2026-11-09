"""Telegram chat mode (notify/tg_chat.py): plain messages → an answer or ONE normal command. Mock LLM, fake Telegram."""

from __future__ import annotations

from tests.test_telegram import OWNER, seed_job, send, tg  # noqa: F401


def _texts(fake, n=8):
    return fake.transcript().splitlines()[-n:] if hasattr(fake, "transcript") else []


def test_where_are_the_clips_answers_without_the_llm(tg, monkeypatch):  # noqa: F811
    from clipforge.notify import tg_chat

    b, fake, root = tg
    job, ws = seed_job(root)
    monkeypatch.setattr(tg_chat, "_llm", lambda: (_ for _ in ()).throw(AssertionError("no LLM for 'where'")))
    send(b, fake, f"/job {job.id}")
    send(b, fake, "where are the clips? I need the links")
    t = fake.transcript()
    assert "→ /where" in t and str(ws.clips_dir) in t
    send(b, fake, f"bhai clip {job.id}-2 kahan hai, link do")
    t = fake.transcript()
    assert "/where " + f"{job.id}-2" in t and "02_clip.mp4" in t


def test_plain_words_run_one_command_and_show_it(tg):  # noqa: F811
    from clipforge.services import jobs as job_svc

    b, fake, root = tg
    job, ws = seed_job(root)
    send(b, fake, f"/job {job.id}")
    send(b, fake, "please approve 1 and 2")
    t = fake.transcript()
    assert f"→ /approve {job.id} 1,2" in t
    _, c1 = job_svc.find_cand(ws, 1)
    _, c3 = job_svc.find_cand(ws, 3)
    assert c1.approved is True and c3.approved is not True
    send(b, fake, "what's posting next?")
    assert "→ /queue" in fake.transcript()


def test_dangerous_commands_still_ask_and_viewers_stay_read_only(tg):  # noqa: F811
    from clipforge.notify import tg_store as st
    from clipforge.services import jobs as job_svc

    b, fake, root = tg
    job, _ = seed_job(root)
    send(b, fake, f"delete job {job.id}")
    t = fake.transcript()
    assert "→ /delete" in t and job_svc.job_or_404(job.id)   # asked, nothing deleted
    st.add_chat(77, 77, "viewer", "private", "friend")
    send(b, fake, "approve 1", chat=77)
    assert "read-only" in fake.transcript()


def test_small_talk_gets_an_answer_and_history_is_kept(tg):  # noqa: F811
    from clipforge.notify import tg_chat

    b, fake, _ = tg
    send(b, fake, "hello")
    assert "I can show jobs" in fake.transcript()
    assert tg_chat._history(OWNER)[-1]["u"] == "hello"


def test_chat_off_falls_back_and_llm_down_is_explained(tg, monkeypatch):  # noqa: F811
    import clipforge.config as config_mod
    from clipforge.notify import tg_chat

    b, fake, root = tg
    monkeypatch.setattr(tg_chat, "_llm", lambda: (_ for _ in ()).throw(RuntimeError("ollama down")))
    send(b, fake, "how are my reels doing")
    assert "isn't answering" in fake.transcript()
    p = config_mod.DEFAULT_CONFIG_PATH
    p.write_text(p.read_text(encoding="utf-8").replace("telegram: {enabled: true,", "telegram: {enabled: true, chat: false,"),
                 encoding="utf-8")
    send(b, fake, "hello again")
    assert "Send me a YouTube link" in fake.transcript()
