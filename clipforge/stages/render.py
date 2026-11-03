"""Stage 8 — final ffmpeg render: per-shot framing → concat → burned captions → loudness-normalised audio.

Why one filtergraph per clip: every camera shot becomes its own chain (`trim` → crop/split/blur →
scale 1080x1920), the chains are joined with the `concat` FILTER and the ASS captions are burned once
over the joined video. Compared with rendering shot files and joining them with the concat demuxer,
this has no timestamp drift between audio and captions, no re-encode generations and allows a
different layout per shot. Moving crops use `sendcmd` to update `crop x` once per output frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from clipforge.config import Config
from clipforge.models import CropPlan, CropSegment
from clipforge.utils.ffmpeg import escape_filter_path, measure_loudness, run_ffmpeg
from clipforge.utils.gpu import nvenc_available


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


@dataclass
class Geometry:
    W: int
    H: int
    cw: int  # 9:16 crop size in source pixels
    ch: int


def geometry(plan: CropPlan, out_w: int, out_h: int) -> Geometry:
    W, H = plan.src_width, plan.src_height
    target = out_w / out_h
    if W / H > target:
        ch, cw = H, _even(H * target)
    else:
        cw, ch = W, _even(W / target)
    return Geometry(W, H, min(cw, W), min(ch, H))


def _x_px(cx: float, g: Geometry) -> int:
    return int(max(0, min(g.W - g.cw, round(cx * g.W - g.cw / 2))))


def write_sendcmd(seg: CropSegment, g: Geometry, fps: int, target: str, path: Path) -> bool:
    """Write per-frame `crop x` updates (linear interpolation between samples). False if the crop is static."""
    xs = [_x_px(x, g) for x in seg.xs]
    if len(set(xs)) <= 1:
        return False
    dur = seg.end - seg.start
    n = len(xs)
    lines = []
    last = None
    for f in range(int(dur * fps) + 1):
        t = f / fps
        pos = t / dur * (n - 1) if dur > 0 else 0
        i = min(n - 2, int(pos))
        x = int(round(xs[i] + (xs[i + 1] - xs[i]) * (pos - i)))
        if x != last:
            lines.append(f"{t:.3f} {target} x {x};")
            last = x
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _split_box(cx: float, cy: float, face_h: float, g: Geometry, out_w: int, half_h: int) -> tuple[int, int, int, int]:
    aspect = out_w / half_h  # 1080/960
    rh = min(g.H, max(face_h * g.H * 3.4, 0.42 * g.H))
    rw = rh * aspect
    if rw > g.W:
        rw = g.W
        rh = rw / aspect
    rw, rh = _even(rw), _even(rh)
    x = int(max(0, min(g.W - rw, cx * g.W - rw / 2)))
    y = int(max(0, min(g.H - rh, cy * g.H - rh * 0.42)))
    return rw, rh, x, y


def _filter_dir(target: Path, cwd: Path) -> str:
    """Path for a filter option, relative to ffmpeg's cwd when possible.

    Relative paths avoid Windows drive colons and characters like ' in user names, which cannot be
    escaped reliably inside quoted filter options. Falls back to an escaped absolute path (different drive).
    """
    import os

    try:
        rel = os.path.relpath(target, cwd).replace("\\", "/")
        if "'" not in rel:
            return rel
    except ValueError:  # different drive on Windows
        pass
    return escape_filter_path(target)


def build_filtergraph(plan: CropPlan, ass_path: Path | None, work: Path, cfg: Config,
                      loud: dict | None) -> tuple[str, str, str]:
    """Return (filter_complex, video_label, audio_label)."""
    r = cfg.render
    W, H, fps = r.width, r.height, r.fps
    g = geometry(plan, W, H)
    segs = plan.segments
    n = len(segs)
    parts: list[str] = []
    if n > 1:
        parts.append(f"[0:v]split={n}" + "".join(f"[s{i}]" for i in range(n)))
    tail = f"scale={W}:{H}:flags=lanczos,setsar=1,fps={fps},format=yuv420p"
    for i, seg in enumerate(segs):
        src = f"[s{i}]" if n > 1 else "[0:v]"
        head = f"{src}trim=start={seg.start:.3f}:end={seg.end:.3f},setpts=PTS-STARTPTS"
        if seg.mode == "split" and len(seg.faces) >= 2:
            half = H // 2
            boxes = [_split_box(fx, fy, seg.face_h, g, W, half) for fx, fy in seg.faces[:2]]
            parts.append(f"{head},split=2[a{i}][b{i}]")
            parts.append(f"[a{i}]crop={boxes[0][0]}:{boxes[0][1]}:{boxes[0][2]}:{boxes[0][3]},scale={W}:{half}:flags=lanczos,setsar=1[t{i}]")
            parts.append(f"[b{i}]crop={boxes[1][0]}:{boxes[1][1]}:{boxes[1][2]}:{boxes[1][3]},scale={W}:{H - half}:flags=lanczos,setsar=1[u{i}]")
            parts.append(f"[t{i}][u{i}]vstack=inputs=2,fps={fps},format=yuv420p[v{i}]")
        elif seg.mode == "blur":
            parts.append(f"{head},split=2[a{i}][b{i}]")
            parts.append(f"[a{i}]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
                         f"gblur=sigma=10,eq=brightness=-0.06,scale={W}:{H}[bg{i}]")
            parts.append(f"[b{i}]scale={W}:-2:flags=lanczos[fg{i}]")
            parts.append(f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps},format=yuv420p[v{i}]")
        else:  # track / center
            xs = seg.xs or [0.5]
            x0 = _x_px(xs[0], g)
            y0 = max(0, (g.H - g.ch) // 2)
            target = f"crop@c{i}"
            cmd_file = work / f"crop_{i}.cmd"
            moving = seg.mode == "track" and write_sendcmd(seg, g, fps, target, cmd_file)
            send = f"sendcmd=f='{cmd_file.name}'," if moving else ""  # relative: ffmpeg runs in `work`
            parts.append(f"{head},{send}{target}=w={g.cw}:h={g.ch}:x={x0}:y={y0},{tail}[v{i}]")
    if n > 1:
        parts.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]")
        vlabel = "vc"
    else:
        vlabel = "v0"
    if ass_path is not None:
        from clipforge.config import FONTS_DIR

        parts.append(f"[{vlabel}]ass=filename='{ass_path.name}':fontsdir='{_filter_dir(FONTS_DIR, work)}'[vout]")
        vlabel = "vout"

    target_i = r.loudness_lufs
    if loud:
        an = (f"loudnorm=I={target_i}:TP=-1.5:LRA=11:measured_I={loud['input_i']}:measured_TP={loud['input_tp']}:"
              f"measured_LRA={loud['input_lra']}:measured_thresh={loud['input_thresh']}:offset={loud['target_offset']}:linear=true")
    else:
        an = f"loudnorm=I={target_i}:TP=-1.5:LRA=11"
    parts.append(f"[0:a]{an},aresample=48000,aformat=channel_layouts=stereo[aout]")
    return ";".join(parts), vlabel, "aout"


def video_codec_args(cfg: Config) -> list[str]:
    r = cfg.render
    use_nv = nvenc_available() if str(r.use_nvenc).lower() == "auto" else str(r.use_nvenc).lower() == "true"
    if use_nv:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(r.crf + 1), "-b:v", "0",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", r.preset, "-crf", str(r.crf), "-profile:v", "high", "-pix_fmt", "yuv420p"]


def render_clip(source: Path, start: float, end: float, plan: CropPlan, ass_path: Path | None, out: Path,
                work: Path, cfg: Config, on_progress=None, cancel=None) -> Path:
    duration = end - start
    loud = measure_loudness(source, start, duration, cfg.render.loudness_lufs)
    graph, vlabel, alabel = build_filtergraph(plan, ass_path, work, cfg, loud)
    graph_file = work / "filtergraph.txt"
    graph_file.write_text(graph, encoding="utf-8")
    tmp = work / "render.tmp.mp4"  # not in clips/: a leftover must never look like a finished clip
    args = [
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(Path(source).resolve()),
        "-/filter_complex", graph_file.name,  # read the graph from a file (ffmpeg ≥7 syntax)
        "-map", f"[{vlabel}]", "-map", f"[{alabel}]",
        *video_codec_args(cfg),
        "-r", str(cfg.render.fps),
        "-c:a", "aac", "-b:a", cfg.render.audio_bitrate, "-ar", "48000",
        "-t", f"{duration:.3f}",
        "-movflags", "+faststart",
        "-metadata", "comment=", "-map_metadata", "-1",
        str(tmp.resolve()),
    ]
    try:
        run_ffmpeg(args, duration=duration, on_progress=on_progress, cancel=cancel, cwd=work)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return out
