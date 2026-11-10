"""Captions/titles/YouTube audio language follow the clip's spoken language (2026-11-10)."""

from __future__ import annotations

from clipforge.utils.text import language_rule, spoken_language


def test_english_clip_is_english():
    assert spoken_language("so when we started openai in 2015 i think you could count on one hand".split()) == "en"
    assert spoken_language("I BELIEVE IT IS PROBABLY THE BEST TIME IN HISTORY TO BUILD, TO MAKE THINGS".split()) == "en"


def test_hindi_and_hinglish_clips_are_hindi():
    assert spoken_language("toh dekho yaar business mein sabse badi galti ye hai ki log sochte nahi".split()) == "hi"
    assert spoken_language("मैं आपको बताता हूँ कि AI बहुत powerful है".split()) == "hi"
    assert spoken_language([]) == "hi"


def test_rules_name_the_language():
    assert "English only" in language_rule("en") and "Hinglish" in language_rule("hi")


def test_upload_body_uses_the_clip_language():
    import datetime as dt

    from clipforge.api import db
    from clipforge.publish.runner import upload_body

    it = db.QueueItem(channel_id="c", video_id="v", rank=1, title="t", description="d", spoken_lang="en")
    body = upload_body(it, {"language": "hi"}, dt.datetime(2026, 11, 11, tzinfo=dt.timezone.utc))
    assert body["snippet"]["defaultAudioLanguage"] == "en" and body["snippet"]["defaultLanguage"] == "en"
    it.spoken_lang = ""
    assert upload_body(it, {"language": "hi"}, dt.datetime(2026, 11, 11, tzinfo=dt.timezone.utc))["snippet"]["defaultAudioLanguage"] == "hi"


class _FakeModel:
    def __init__(self, per_window):
        self.per_window, self.i = per_window, 0

    def detect_language(self, audio):
        p = self.per_window[self.i % len(self.per_window)]
        self.i += 1
        return max(p, key=p.get), max(p.values()), list(p.items())


def test_episode_language_samples_the_whole_episode():
    import numpy as np

    from clipforge.stages.transcribe import episode_language

    audio = np.zeros(16000 * 600, dtype=np.float32)
    # Hindi intro, then an English interview: English wins
    m = _FakeModel([{"hi": 0.9, "en": 0.1}] + [{"en": 0.8, "hi": 0.1}] * 7)
    assert episode_language(m, audio) == "en"
    # Hinglish often comes back as Urdu: still Hindi
    m = _FakeModel([{"ur": 0.5, "hi": 0.3, "en": 0.2}] * 8)
    assert episode_language(m, audio) == "hi"


def test_clip_whisper_language_follows_the_transcript():
    from clipforge.models import Segment, Transcript, Word
    from clipforge.stages.clip_whisper import clip_language

    words = [Word(start=i, end=i + 0.5, text=w) for i, w in enumerate("so when we started openai we had no idea".split())]
    t = Transcript(language="hi", model="pasted", duration=10, source="pasted",
                   segments=[Segment(id=0, start=0, end=10, text="", words=words)])
    assert clip_language(t, "smart") == "en"          # English captions → re-transcribe in English, not Hindi
    assert clip_language(t, "hi") == "hi"             # the user forced Hindi
    t.source, t.language = "whisper", "en"
    assert clip_language(t, "smart") == "en"
