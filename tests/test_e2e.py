"""End-to-end: synthetic video → mock LLM selection → reframe → captions → render → metadata.

Skips Whisper (a hand-made transcript is used) so it runs in ~1 minute on CPU. Needs ffmpeg.
"""

from __future__ import annotations

import json

import pytest

from clipforge import pipeline
from clipforge.config import Config
from clipforge.models import EnergyData, Segment, Transcript, VideoInfo, Word
from clipforge.utils import ffmpeg as ff
from clipforge.workspace import Workspace

try:
    ff.ffmpeg_bin()
    HAVE_FFMPEG = True
except ff.FFmpegError:
    HAVE_FFMPEG = False

pytestmark = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")

DURATION = 70.0


def _make_video(path):
    # 1280x720 test pattern with a hard "camera cut" at 30 s + a tone
    ff.run_ffmpeg([
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration=30",
        "-f", "lavfi", "-i", f"smptebars=size=1280x720:rate=30:duration={DURATION - 30}",
        "-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=48000:duration={DURATION}",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ])


def _transcript() -> Transcript:
    segs, t, i = [], 0.5, 0
    words_text = "paisa kamana easy nahi hai lekin 100 crore ka business banana possible hai.".split()
    while t < DURATION - 5:
        words = []
        for w in words_text:
            words.append(Word(start=round(t, 2), end=round(t + 0.35, 2), text=w, roman=w))
            t += 0.4
        segs.append(Segment(id=i, start=words[0].start, end=words[-1].end, text=" ".join(words_text), words=words))
        t += 0.8
        i += 1
    return Transcript(language="hi", model="test", duration=DURATION, segments=segs)


@pytest.fixture
def workspace(tmp_path):
    cfg = Config().with_overrides({
        "output.dir": str(tmp_path / "D'Souza workspace"), "llm.provider": "mock",  # quote + space in path "clips.count": 2, "clips.min_sec": 20, "clips.max_sec": 40,
        "render.preset": "ultrafast", "render.workers": 2,
    })
    ws = Workspace(cfg.workspace, "synthetic")
    _make_video(ws.source)
    probe = ff.probe_video(ws.source)
    ws.write_model(ws.info, VideoInfo(id="synthetic", title="Synthetic Test Podcast", channel="Test Channel",
                                      url="https://example.com/v", duration=probe["duration"], width=1280, height=720, fps=30))
    ff.run_ffmpeg(["-i", str(ws.source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(ws.audio)])
    ws.write_model(ws.transcript, _transcript())
    ws.write_model(ws.energy, EnergyData(rms_db=[], peaks=[]))
    for stage in ("download", "transcribe", "analyze"):
        ws.mark_done(stage, "test")
    return cfg, ws


def test_full_render(workspace):
    cfg, ws = workspace
    cands = pipeline.select(ws, cfg)
    assert 1 <= len(cands) <= 2
    outs = pipeline.render(ws, cfg)
    assert len(outs) == len(cands)
    for c, out in zip(sorted(cands, key=lambda c: c.rank), sorted(outs)):
        info = ff.probe_video(out)
        assert (info["width"], info["height"]) == (1080, 1920)
        assert info["vcodec"] == "h264" and info["acodec"] == "aac"
        assert abs(info["duration"] - c.duration) < 0.5
        assert out.name.startswith(f"{c.rank:02d}_")
    metas = pipeline.metadata(ws, cfg)
    assert all(len(m.hashtags) <= 5 and all(h == h.lower() for h in m.hashtags) for m in metas)
    assert all("Source: Synthetic Test Podcast" in m.credit for m in metas)
    assert ws.metadata_csv.exists() and ws.gallery.exists()

    # resume: nothing re-rendered
    mtimes = {p: p.stat().st_mtime for p in outs}
    pipeline.render(ws, cfg)
    assert all(p.stat().st_mtime == m for p, m in mtimes.items())

    # editing one clip re-renders only that clip and keeps an undo copy
    data = json.loads(ws.candidates.read_text(encoding="utf-8"))
    data["candidates"][0]["title"] = "Edited title"
    ws.candidates.write_text(json.dumps(data), encoding="utf-8")
    new = pipeline.render(ws, cfg, only=[data["candidates"][0]["rank"]])
    assert len(new) == 1 and "edited-title" in new[0].name
    assert (ws.work_dir(data["candidates"][0]["rank"]) / "previous.mp4").exists()
