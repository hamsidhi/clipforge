"""Transcript input (pasted / YouTube captions) + clip-level Whisper. No network, no real Whisper."""

from __future__ import annotations

import json

import pytest

from clipforge import pipeline
from clipforge.config import Config
from clipforge.models import EnergyData, Segment, VideoInfo, Word
from clipforge.stages import clip_whisper
from clipforge.stages import transcript_input as ti
from clipforge.utils.log import NullReporter
from clipforge.workspace import Workspace

YT_COPY = """Transcript
0:00
[Music]
0:02
2 seconds
Raj: aaj hum baat karenge business ki
0:07
paisa kaise banta hai
1:02:03
last line
"""
YT_INLINE = "0:00 pehli line\n0:05 doosri line yahan\n0:09 teesri\n"
SRT = """1
00:00:01,000 --> 00:00:04,000
Hello <i>there</i>

2
00:00:04,500 --> 00:00:07,250
[Applause]
second cue
"""
VTT = """WEBVTT
Kind: captions

00:00:01.000 --> 00:00:03.000 align:start position:0%
pehla<00:00:01.500><c> wakya</c>

00:00:03.000 --> 00:00:05.000
pehla wakya
doosra wakya
"""
BRACKETS = "[00:00:01] one two\n[00:01:30] three four five\n(01:02:03) six\n"


# ============================================================================ parsers
def test_parse_youtube_copy_own_line_junk_and_speakers():
    p = ti.parse_text(YT_COPY)
    assert p.fmt == "timestamps" and not p.errors
    assert [(ln.start, ln.text) for ln in p.lines] == [(2, "aaj hum baat karenge business ki"),
                                                        (7, "paisa kaise banta hai"), (3723, "last line")]


def test_parse_inline_brackets_srt_vtt():
    assert [ln.start for ln in ti.parse_text(YT_INLINE).lines] == [0, 5, 9]
    b = ti.parse_text(BRACKETS)
    assert [(ln.start, ln.text) for ln in b.lines] == [(1, "one two"), (90, "three four five"), (3723, "six")]
    s = ti.parse_text(SRT)
    assert s.fmt == "srt" and [(ln.start, ln.end, ln.text) for ln in s.lines] == [(1, 4, "Hello there"), (4.5, 7.25, "second cue")]
    v = ti.parse_text(VTT)
    assert v.fmt == "vtt" and [ln.text for ln in v.lines] == ["pehla wakya", "doosra wakya"]  # rolling repeat dropped


def test_parse_errors_carry_line_numbers():
    with pytest.raises(ti.TranscriptParseError) as e:
        ti.parse_or_raise("0:10 a\n0:20 b\n0:05 c\n")
    assert "line 3" in str(e.value)
    with pytest.raises(ti.TranscriptParseError) as e:
        ti.parse_or_raise("just some text\nwithout any\ntimes at all\nreally")
    assert "line 3" in str(e.value) or "no timestamps" in str(e.value)
    with pytest.raises(ti.TranscriptParseError) as e:
        ti.parse_or_raise("1\n00:00:xx,000 --> 00:00:02,000\nhi\n")
    assert "line 2" in str(e.value)


def test_json3_word_offsets_are_exact():
    data = {"events": [{"tStartMs": 1000, "dDurationMs": 3000, "segs": [{"utf8": "aaj"}, {"utf8": " hum", "tOffsetMs": 400},
                                                                     {"utf8": " [संगीत]", "tOffsetMs": 900}]},
                       {"tStartMs": 2500, "aAppend": 1, "segs": [{"utf8": "\n"}]},
                       {"tStartMs": 4000, "dDurationMs": 2000, "segs": [{"utf8": "baat"}, {"utf8": " karte", "tOffsetMs": 500}]}]}
    lines, exact = ti.parse_json3(data)
    assert exact and [w.text for w in lines[0].words] == ["aaj", "hum"]
    assert lines[0].words[1].start == 1.4 and lines[1].words[1].start == 4.5
    t = ti.to_transcript(lines, 10, "hi", "youtube", exact)
    assert t.word_timing == "exact" and t.source == "youtube"


def test_estimated_words_spread_evenly():
    t = ti.to_transcript(ti.parse_text("0:00 a b c d\n0:04 e\n").lines, 10, "hi", "pasted", exact=False)
    assert [w.start for w in t.segments[0].words] == [0, 1, 2, 3] and t.word_timing == "estimated"


# ============================================================================ source priority + sanity
def _ws(tmp_path, duration=600.0) -> Workspace:
    ws = Workspace(tmp_path / "ws", "vid")
    ws.write_model(ws.info, VideoInfo(id="vid", title="T", duration=duration))
    return ws


def _captions(ws, manual=True, auto=True):
    tracks = []
    if manual:
        tracks.append({"kind": "manual", "lang": "hi", "ext": "vtt", "data": "WEBVTT\n\n00:00:01.000 --> 00:09:50.000\nmanual line\n"})
    if auto:
        tracks.append({"kind": "auto", "lang": "hi", "ext": "json3", "data": {"events": [
            {"tStartMs": 1000, "dDurationMs": 589000, "segs": [{"utf8": "auto"}, {"utf8": " words", "tOffsetMs": 300}]}]}})
    ws.write_json(ws.dir / ti.CAPTIONS_FILE, {"tracks": tracks, "offset": 0, "length": None})


def test_auto_priority_order(tmp_path):
    ws = _ws(tmp_path)
    assert ti.choose(ws, "auto", 600, "hi")[0] is None  # nothing → Whisper
    _captions(ws, manual=True, auto=False)
    t, r = ti.choose(ws, "auto", 600, "hi")
    assert r["source"] == "youtube" and t.word_timing == "estimated"
    _captions(ws, manual=True, auto=True)
    t, r = ti.choose(ws, "auto", 600, "hi")
    assert t.word_timing == "exact" and "auto" in r["label"]  # word timing beats manual in auto mode
    t, r = ti.choose(ws, "youtube", 600, "hi")
    assert "manual" in r["label"]  # source=youtube: manual first
    ti.save_input(ws, "0:00 pasted line\n9:55 end\n", None)
    t, r = ti.choose(ws, "auto", 600, "hi")
    assert r["source"] == "pasted" and not r["warnings"]
    assert ti.choose(ws, "whisper", 600, "hi")[0] is None


def test_pick_tracks_skips_auto_translations():
    info = {"subtitles": {"en": [{"ext": "vtt", "url": "u1"}]},
            "automatic_captions": {"en": [{"ext": "json3", "url": "x?tlang=en"}],
                                   "hi-orig": [{"ext": "vtt", "url": "v"}, {"ext": "json3", "url": "j"}]}}
    assert ti.pick_tracks(info, ["hi", "en"]) == [("manual", "en", {"ext": "vtt", "url": "u1"}),
                                                  ("auto", "hi", {"ext": "json3", "url": "j"})]


def test_duration_mismatch_and_script_warnings(tmp_path):
    ws = _ws(tmp_path)
    ti.save_input(ws, "0:00 pehli line\n4:00 aakhri line\n", None)  # ends at ~4 min, the video is 10 min
    _, r = ti.choose(ws, "auto", 600, "hi")
    assert any("ends at 4:0" in w and "10:00" in w for w in r["warnings"])
    ti.save_input(ws, "0:00 यह हिंदी है\n9:50 अंत\n", None)
    _, r = ti.choose(ws, "auto", 600, "en")
    assert any("script" in w for w in r["warnings"])


def test_pasted_full_video_times_move_into_a_section(tmp_path):
    ws = _ws(tmp_path, duration=300)
    ti.save_input(ws, "0:10 early\n10:05 inside\n14:50 also inside\n20:00 late\n", (600.0, 900.0))
    t, r = ti.choose(ws, "auto", 300, "hi")
    assert [(s.start, s.text) for s in t.segments] == [(5, "inside"), (290, "also inside")]


def test_transcribe_stage_skips_whisper_and_reports(tmp_path, monkeypatch):
    from clipforge.stages import transcribe

    monkeypatch.setattr(transcribe, "run", lambda *a, **k: pytest.fail("full Whisper must not run"))
    cfg = Config().with_overrides({"output.dir": str(tmp_path / "ws")})
    ws = _ws(tmp_path)
    ti.save_input(ws, "0:00 hello\n9:50 bye\n", None)
    pipeline.run_transcribe(ws, cfg, NullReporter())
    t = pipeline.load_transcript(ws)
    rep = ti.read_report(ws.dir)
    assert t.source == "pasted" and t.word_timing == "estimated" and rep["label"] == "pasted" and rep["saved_sec"] > 0
    fp = pipeline.fp_transcribe(ws, cfg)
    assert fp != pipeline.fp_transcribe(ws, cfg.with_overrides({"transcript.source": "whisper"}))


# ============================================================================ clip-level Whisper
WORDS = "paisa kamana easy nahi hai lekin business banana possible hai.".split()


def _pasted_lines(duration: float) -> str:
    out, t = [], 0
    while t < duration - 5:
        out.append(f"{t // 60}:{t % 60:02d} {' '.join(WORDS)}")
        t += 5
    return "\n".join(out) + "\n"


@pytest.fixture
def est(tmp_path, monkeypatch):
    cfg = Config().with_overrides({"output.dir": str(tmp_path / "ws"), "llm.provider": "mock", "clips.count": 2,
                                   "clips.min_sec": 20, "clips.max_sec": 40})
    ws = Workspace(cfg.workspace, "vid")
    ws.write_model(ws.info, VideoInfo(id="vid", title="T", duration=120))
    ti.save_input(ws, _pasted_lines(120), None)
    ws.write_model(ws.energy, EnergyData(rms_db=[], peaks=[]))
    calls: list[tuple[float, float]] = []

    def fake_range(ws_, cfg_, a, b, lang, state):  # "Whisper": real words 0.1 s later than the estimate
        calls.append((a, b))
        segs, t = [], a + 0.1
        while t + 4 < b:
            words = [Word(start=round(t + k * 0.4, 3), end=round(t + k * 0.4 + 0.3, 3), text=w) for k, w in enumerate(WORDS)]
            segs.append(Segment(id=0, start=words[0].start, end=words[-1].end, text=" ".join(WORDS), words=words))
            t += 5
        return segs

    monkeypatch.setattr(clip_whisper, "transcribe_range", fake_range)
    pipeline.run_transcribe(ws, cfg, NullReporter())
    for stage in ("download", "analyze"):
        ws.mark_done(stage, "test")
    return cfg, ws, calls


def test_estimated_then_clip_whisper_resnaps_on_exact_words(est):
    cfg, ws, calls = est
    cands = pipeline.select(ws, cfg)
    assert cands and len(calls) == len(cands)
    for (a, b), c in zip(calls, cands):
        assert a <= c.start and c.end <= b  # Whisper ran only on clip ± 1.5 s
        assert b - a <= c.duration + 2 * cfg.transcript.clip_pad_sec + 4
    t = pipeline.load_transcript(ws)
    assert len(t.exact_ranges) == len(cands)
    for c in cands:  # boundaries sit on exact word times now
        assert any(abs(c.start - (w.start - 0.15)) < 0.2 or abs(c.start - w.start) < 0.2 for w in t.all_words())
    rep = ti.read_report(ws.dir)
    assert rep["clip_whisper_sec"] >= 0 and "saved_sec" in rep


def test_rerender_uses_cache_not_whisper(est):
    cfg, ws, calls = est
    cands = pipeline.select(ws, cfg)
    n = len(calls)
    t = pipeline.load_transcript(ws)
    assert clip_whisper.ensure_exact(ws, cfg, t, [(c.start, c.end) for c in cands]) is False  # covered → nothing
    # transcript.json rebuilt (e.g. transcribe re-run): the per-range cache files answer, Whisper doesn't run
    pipeline.run_transcribe(ws, cfg.with_overrides({"transcript.source": "pasted"}), NullReporter())
    t2 = pipeline.load_transcript(ws)
    assert t2.exact_ranges == []
    assert clip_whisper.ensure_exact(ws, cfg, t2, [(c.start, c.end) for c in cands]) is True
    assert len(calls) == n and len(pipeline.load_transcript(ws).exact_ranges) == len(cands)
    assert all(json.loads(f.read_text())["range"] for f in (ws.dir / "clip_whisper").glob("*.json"))


def test_exact_source_never_runs_whisper(tmp_path, monkeypatch):
    monkeypatch.setattr(clip_whisper, "transcribe_range", lambda *a: pytest.fail("no Whisper for exact timing"))
    cfg = Config().with_overrides({"output.dir": str(tmp_path / "ws")})
    ws = _ws(tmp_path)
    _captions(ws, manual=False, auto=True)
    pipeline.run_transcribe(ws, cfg, NullReporter())
    t = pipeline.load_transcript(ws)
    assert t.word_timing == "exact" and clip_whisper.ensure_exact(ws, cfg, t, [(1, 30)]) is False
