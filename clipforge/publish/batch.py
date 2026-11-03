"""Assisted mode: prepare a folder you upload yourself in YouTube Studio, plus a checklist page."""

from __future__ import annotations

import html
import json
import os
import shutil
from pathlib import Path

from clipforge.config import Config, load_config
from clipforge.publish import channels as chs
from clipforge.publish import queue as q
from clipforge.publish.timez import fmt_local, now_utc, utc_to_local
from clipforge.utils.text import slugify


def batches_root(cfg: Config) -> Path:
    p = cfg.workspace / "batches"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _link_or_copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def prepare(channel_id: str, n: int | None = None, cfg: Config | None = None) -> dict:
    """Take the next N queued clips (not yet in a batch) and build workspace/batches/<channel>/<date>/."""
    cfg = cfg or load_config()
    ch = chs.get(channel_id)
    if not ch:
        raise q.QueueError("Unknown channel")
    zone = chs.settings_of(ch, cfg)["timezone"]
    n = min(n or cfg.publishing.batch_max, cfg.publishing.batch_max)
    todo = [it for it in q.items(channel_id, ("queued",)) if it.slot_utc and not it.batch][:n]
    if not todo:
        raise q.QueueError("No scheduled clips waiting for a batch — schedule approved clips first.")
    stamp = utc_to_local(now_utc(), zone).strftime("%Y-%m-%d_%H%M")
    folder = batches_root(cfg) / slugify(ch.title or ch.id, 30) / stamp
    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, it in enumerate(todo, 1):
        src = q.clip_file(it, cfg)
        slot = utc_to_local(it.slot_utc, zone)
        name = f"{i:02d}_{slot:%Y-%m-%d_%H%M}_{slugify(it.title, 40)}.mp4"
        _link_or_copy(src, folder / name)
        thumb = src.parent / "_work" / f"{it.rank:02d}" / "thumb.jpg"
        if thumb.exists():
            _link_or_copy(thumb, folder / f"{i:02d}_thumb.jpg")
        entries.append({"n": i, "id": it.id, "file": name, "thumb": f"{i:02d}_thumb.jpg" if thumb.exists() else "",
                        "title": it.title, "description": it.description, "tags": ", ".join(it.tags),
                        "slot_local": f"{slot:%d %b %Y}", "slot_time": f"{slot:%I:%M %p}",
                        "slot_pretty": fmt_local(it.slot_utc, zone), "ig_caption": it.ig_caption})
        it.batch = str(folder.relative_to(cfg.workspace)).replace("\\", "/")
        q.save(it)
    (folder / "batch.json").write_text(json.dumps({"channel": ch.title, "channel_id": ch.id, "items": entries},
                                                  indent=2, ensure_ascii=False), encoding="utf-8")
    (folder / "batch.html").write_text(render_html(ch.title, ch.thumbnail, entries, zone), encoding="utf-8")
    return {"folder": str(folder), "relative": str(folder.relative_to(cfg.workspace)).replace("\\", "/"),
            "count": len(entries), "items": entries}


def list_batches(cfg: Config | None = None) -> list[dict]:
    cfg = cfg or load_config()
    out = []
    for meta in sorted(batches_root(cfg).glob("*/*/batch.json"), reverse=True):
        data = json.loads(meta.read_text(encoding="utf-8"))
        rel = str(meta.parent.relative_to(cfg.workspace)).replace("\\", "/")
        out.append({"relative": rel, "folder": str(meta.parent), "channel": data.get("channel"),
                    "channel_id": data.get("channel_id"), "count": len(data.get("items", [])), "name": meta.parent.name})
    return out


def render_html(channel: str, avatar: str, entries: list[dict], zone: str) -> str:
    def esc(s: str) -> str:
        return html.escape(s or "", quote=True)

    rows = []
    for e in entries:
        rows.append(f"""
<div class="card" data-id="{e['id']}">
  <div class="left">{f'<img src="{esc(e["thumb"])}" alt="">' if e['thumb'] else '<div class="noimg"></div>'}
    <label class="done"><input type="checkbox" onchange="mark(this)"> Done</label></div>
  <div class="body">
    <div class="when">#{e['n']} · <b>{esc(e['slot_local'])}</b> at <b>{esc(e['slot_time'])}</b> IST</div>
    <div class="file">{esc(e['file'])}</div>
    {field('Title', e['title'])}
    {field('Description', e['description'])}
    {field('Tags', e['tags'])}
    <details><summary>Instagram kit</summary>{field('Caption', e['ig_caption'])}</details>
  </div>
</div>""")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Upload batch — {esc(channel)}</title>
<style>
body{{background:#0f0f13;color:#eee;font-family:system-ui,sans-serif;margin:0;padding:20px;max-width:980px;margin:auto}}
h1{{font-size:20px;margin:0}} .head{{display:flex;gap:12px;align-items:center;margin-bottom:12px}} .head img{{width:44px;height:44px;border-radius:50%}}
ol.steps{{background:#18181f;border:1px solid #2a2a33;border-radius:12px;padding:14px 14px 14px 34px;line-height:1.6;font-size:14px}}
a.btn,button{{background:#ffe600;color:#000;border:0;border-radius:8px;padding:7px 12px;font-weight:600;cursor:pointer;text-decoration:none;font-size:13px}}
.card{{display:flex;gap:16px;background:#18181f;border:1px solid #2a2a33;border-radius:14px;padding:14px;margin:14px 0}}
.card.ok{{opacity:.45}} .left{{width:120px;flex:none}} .left img,.noimg{{width:120px;aspect-ratio:9/16;object-fit:cover;border-radius:10px;background:#000}}
.body{{flex:1;min-width:0}} .when{{font-size:15px;margin-bottom:4px}} .file{{color:#999;font-size:12px;margin-bottom:8px;word-break:break-all}}
.f{{margin:8px 0}} .f .lab{{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:#aaa}}
.f pre{{white-space:pre-wrap;background:#0f0f13;border:1px solid #2a2a33;border-radius:8px;padding:8px;margin:4px 0 0;font:13px system-ui}}
.done{{display:block;margin-top:8px;font-size:13px}} summary{{cursor:pointer;color:#aaa;font-size:13px;margin-top:6px}}
@media (max-width:600px){{.card{{flex-direction:column}} .left{{width:100%}} .left img{{width:120px}}}}
</style></head><body>
<div class="head">{f'<img src="{esc(avatar)}" alt="">' if avatar else ''}<div><h1>Upload batch — {esc(channel)}</h1>
<div style="color:#aaa;font-size:13px">{len(entries)} Shorts · times in {esc(zone)}</div></div></div>
<ol class="steps">
<li>Open <a class="btn" href="https://studio.youtube.com" target="_blank" rel="noopener">YouTube Studio</a> — check it's the <b>{esc(channel)}</b> channel (top-right avatar).</li>
<li><b>Create → Upload videos</b> and select <b>all .mp4 files in this folder</b> at once.</li>
<li>For each video (file names start with the number below): paste <b>Title</b>, <b>Description</b> and <b>Tags</b> (Show more → Tags) with the Copy buttons.</li>
<li>Audience: <b>No, it's not made for kids</b>. Altered content: <b>No</b>.</li>
<li><b>Visibility → Schedule</b> → set the date and time shown on the card (IST) → <b>Schedule</b>. Tick “Done”.</li>
<li>Back in ClipForge: <b>Schedule → Sync with YouTube</b> to link the uploads (works with read-only access).</li>
</ol>
{''.join(rows)}
<script>
function copy(btn){{const t=btn.closest('.f').querySelector('pre').innerText;
 (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).catch(()=>{{const a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove();}});
 btn.textContent='Copied ✓';setTimeout(()=>btn.textContent='Copy',1200);}}
function mark(cb){{const c=cb.closest('.card');c.classList.toggle('ok',cb.checked);try{{localStorage.setItem('cf-done-'+c.dataset.id,cb.checked?'1':'')}}catch(e){{}}}}
document.querySelectorAll('.card').forEach(c=>{{try{{if(localStorage.getItem('cf-done-'+c.dataset.id)){{c.classList.add('ok');c.querySelector('input').checked=true}}}}catch(e){{}}}});
</script></body></html>"""


def field(label: str, value: str) -> str:
    return (f'<div class="f"><div class="lab"><span>{html.escape(label)}</span>'
            f'<button onclick="copy(this)">Copy</button></div><pre>{html.escape(value or "")}</pre></div>')
