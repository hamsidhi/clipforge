"""Pipeline orchestration with per-stage caching. Used by BOTH the CLI and the web API.

Stages (names are what the UI shows):
  download → transcribe → analyze → select → reframe → captions → render
Every stage writes its output into workspace/<video_id>/ and records a fingerprint of the settings it
used in state.json; re-running skips stages whose outputs exist and whose fingerprint is unchanged.
"""

from __future__ import annotations

import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from clipforge.config import PROMPTS_DIR, Config
from clipforge.models import CandidateFile, ClipCandidate, ClipMetadata, CropPlan, EnergyData, Transcript, VideoInfo
from clipforge.utils.ffmpeg import Timer, extract_frame
from clipforge.utils.gpu import cpu_count, nvenc_available
from clipforge.utils.log import NullReporter, ProgressReporter, check_cancel, log
from clipforge.workspace import Workspace, fingerprint, video_id_for

STAGES = ["download", "transcribe", "analyze", "select", "reframe", "captions", "render"]
_render_lock = threading.Lock()


# ----------------------------------------------------------------------------- workspace helpers
def open_workspace(video_id: str, cfg: Config) -> Workspace:
    d = cfg.workspace / video_id
    if not d.exists():
        raise FileNotFoundError(f"No workspace for '{video_id}' in {cfg.workspace}. Run `clipforge list` to see videos.")
    return Workspace(cfg.workspace, video_id)


def list_workspaces(cfg: Config) -> list[tuple[str, str, int]]:
    out = []
    if not cfg.workspace.exists():
        return out
    for d in sorted(cfg.workspace.iterdir()):
        if d.is_dir() and (d / "info.json").exists():
            info = VideoInfo.model_validate_json((d / "info.json").read_text(encoding="utf-8"))
            n = len(list((d / "clips").glob("*.mp4"))) if (d / "clips").exists() else 0
            out.append((d.name, info.title, n))
    return out


def load_info(ws: Workspace) -> VideoInfo:
    return ws.read_model(ws.info, VideoInfo)


def load_transcript(ws: Workspace) -> Transcript:
    if not ws.transcript.exists():
        raise FileNotFoundError(f"{ws.transcript} missing — run `clipforge transcribe` first.")
    return ws.read_model(ws.transcript, Transcript)


def load_candidates(ws: Workspace) -> list[ClipCandidate]:
    if not ws.candidates.exists():
        return []
    return ws.read_model(ws.candidates, CandidateFile).candidates


def save_candidates(ws: Workspace, cands: list[ClipCandidate]) -> None:
    ws.write_model(ws.candidates, CandidateFile(video_id=ws.video_id, candidates=cands))


def get_llm(cfg: Config, required: bool = True):
    from clipforge.llm import LLMError, get_provider

    try:
        return get_provider(cfg.llm)
    except LLMError:
        if required:
            raise
        return None


# ----------------------------------------------------------------------------- stages
def _size(p: Path) -> int:
    return p.stat().st_size if p.exists() else 0


def prepare(source: str, cfg: Config, reporter: ProgressReporter | None = None,
            section: tuple[float, float] | None = None, video_id: str | None = None) -> Workspace:
    """download → transcribe → analyze. Returns the workspace."""
    from clipforge.stages import analyze_audio, download, transcribe

    reporter = reporter or NullReporter()
    ws = Workspace(cfg.workspace, video_id or video_id_for(source, section))

    # keyed on the video id, not the raw string: youtu.be/ID and watch?v=ID&si=… are the same video
    fp = fingerprint(ws.video_id, section)
    if ws.stage_done("download", fp, [ws.source, ws.info, ws.audio]):
        reporter.stage("download", "skipped")
        info = load_info(ws)
    else:
        reporter.stage("download", "running", source)
        info = download.run(source, ws, reporter, section)
        ws.mark_done("download", fp)
        reporter.stage("download", "done", f"{info.title} ({info.duration / 60:.1f} min)")
    check_cancel(reporter)

    fp = fingerprint(cfg.whisper.model_dump(), _size(ws.audio))
    if ws.stage_done("transcribe", fp, [ws.transcript]):
        reporter.stage("transcribe", "skipped")
    else:
        reporter.stage("transcribe", "running")
        transcribe.run(ws, cfg.whisper, reporter, info.duration)
        ws.mark_done("transcribe", fp)
        reporter.stage("transcribe", "done")
    check_cancel(reporter)

    fp = fingerprint("energy-v1", _size(ws.audio))
    if ws.stage_done("analyze", fp, [ws.energy]):
        reporter.stage("analyze", "skipped")
    else:
        reporter.stage("analyze", "running", "audio energy")
        e = analyze_audio.run(ws)
        ws.mark_done("analyze", fp)
        reporter.stage("analyze", "done", f"{len(e.peaks)} energy peaks")
    return ws


def select(ws: Workspace, cfg: Config, reporter: ProgressReporter | None = None, force: bool = False) -> list[ClipCandidate]:
    """LLM selection + refine → candidates.json. Hand edits to candidates.json are kept unless force=True."""
    from clipforge.stages import refine, select_clips

    reporter = reporter or NullReporter()
    prompt_text = (PROMPTS_DIR / "clip_selection.md").read_text(encoding="utf-8")
    # configured (not resolved) model: pulling a new Ollama model must not invalidate a finished selection
    fp = fingerprint(cfg.llm.model_dump(exclude={"timeout_sec", "ollama_url"}), cfg.clips.model_dump(),
                     cfg.scoring_weights.model_dump(), prompt_text, ws.stage_time("transcribe"))
    if not force and ws.stage_done("select", fp, [ws.candidates]):
        reporter.stage("select", "skipped")
        return load_candidates(ws)
    if not force and ws.candidates.exists() and ws.candidates.stat().st_mtime > ws.stage_time("select") + 2 \
            and ws.stage_time("select") > 0:
        log.warning("candidates.json was edited by hand — keeping your edits (use --force to re-select).")
        reporter.stage("select", "skipped", "kept hand-edited candidates.json")
        return load_candidates(ws)

    llm = get_llm(cfg)  # only now: a cached selection must not need a running LLM
    reporter.stage("select", "running", f"{llm.name}:{llm.model}")
    info = load_info(ws)
    transcript = load_transcript(ws)
    raw, failed = select_clips.run(ws, cfg, llm, reporter, info, transcript)
    energy = ws.read_model(ws.energy, EnergyData) if ws.energy.exists() else None
    cands = refine.refine(raw, transcript.all_words(), energy, cfg, info.duration, transcript.boundaries())
    if not cands:
        raise RuntimeError("No usable clips were found. Check the transcript, try another LLM provider, "
                           "or lower clips.min_sec in config.yaml.")
    _drop_stale_renders(ws, cands)
    save_candidates(ws, cands)
    if failed:
        # keep what we have, but don't mark the stage done: the next run retries only the failed windows
        log.warning(f"{failed} transcript window(s) failed in the LLM — re-run to retry them (others are cached).")
        reporter.stage("select", "done", f"{len(cands)} clips ({failed} window(s) to retry)")
    else:
        ws.mark_done("select", fp)
        reporter.stage("select", "done", f"{len(cands)} clips")
    return cands


def _drop_stale_renders(ws: Workspace, cands: list[ClipCandidate]) -> None:
    """After a new selection, remove renders whose rank now holds a different clip (keep identical ones)."""
    by_rank = {c.rank: c for c in cands}
    for f in ws.clips_dir.glob("*.mp4"):
        try:
            rank = int(f.name.split("_", 1)[0])
        except ValueError:
            continue
        work = ws.clips_dir / "_work" / f"{rank:02d}"
        prev = {}
        if (work / "render.json").exists():
            prev = json.loads((work / "render.json").read_text(encoding="utf-8")).get("candidate", {})
        new = by_rank.get(rank)
        if new is None or (prev.get("start"), prev.get("end")) != (new.start, new.end):
            f.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)


def _clip_fp(c: ClipCandidate, cfg: Config, words_sig: str) -> str:
    data = c.model_dump(exclude={"rank", "approved", "score", "why", "scores", "topic"})
    return fingerprint("render-v1", data, cfg.captions.model_dump(), cfg.render.model_dump(), cfg.reframe.model_dump(), words_sig)


def render(ws: Workspace, cfg: Config, reporter: ProgressReporter | None = None, only: list[int] | None = None,
           force: bool = False) -> list[Path]:
    """reframe → captions → render for every enabled clip (or `only` these ranks)."""
    from clipforge.stages import captions, reframe, render as rnd, romanize, scenes

    reporter = reporter or NullReporter()
    info = load_info(ws)
    transcript = load_transcript(ws)
    cands = [c for c in load_candidates(ws) if c.enabled and (only is None or c.rank in only)]
    if not cands:
        raise RuntimeError("No clips to render (candidates.json is empty, or --only matched nothing).")

    # Roman Hinglish only for the words we actually show
    needs_roman = [c for c in cands if captions.resolve_style(cfg.captions, c.caption_style).script == "roman"]
    if needs_roman:
        llm = get_llm(cfg, required=False)
        before = sum(1 for w in transcript.all_words() if w.roman is not None)
        romanize.romanize_ranges(transcript, [(c.start, c.end) for c in needs_roman], llm, reporter)
        if sum(1 for w in transcript.all_words() if w.roman is not None) != before:
            ws.write_model(ws.transcript, transcript)
    words = transcript.all_words()

    todo: list[tuple[ClipCandidate, str]] = []
    for c in cands:
        script = captions.resolve_style(cfg.captions, c.caption_style).script
        cw = [w.display(script) for w in words if c.start <= (w.start + w.end) / 2 <= c.end]
        fp = _clip_fp(c, cfg, fingerprint(cw))
        meta = ws.work_dir(c.rank) / "render.json"
        existing = ws.find_clip(c.rank)
        if not force and existing and existing.name == ws.clip_path(c.rank, c.title).name and meta.exists() \
                and json.loads(meta.read_text(encoding="utf-8")).get("fingerprint") == fp:
            continue
        todo.append((c, fp))
    if not todo:
        for s in ("reframe", "captions", "render"):
            reporter.stage(s, "skipped")
        return [p for c in cands if (p := ws.find_clip(c.rank))]

    # --- reframe
    reporter.stage("reframe", "running", f"{len(todo)} clip(s)")
    cuts = scenes.ensure(ws, [(c.start, c.end) for c, _ in todo], info.duration)
    detector = None
    plans: dict[int, CropPlan] = {}
    timer = Timer()
    for i, (c, _) in enumerate(todo):
        check_cancel(reporter)
        shots = scenes.shots_for(cuts, c.start, c.end)
        layout = c.layout or cfg.reframe.layout
        if layout != "blur" and detector is None:
            detector = reframe.FaceDetector()
        plan = reframe.plan_clip(str(ws.source), c.start, c.end, shots, info.width, info.height, cfg.reframe,
                                 cfg.render.width, cfg.render.height, detector,  # type: ignore[arg-type]
                                 c.crop_overrides, layout)
        ws.write_model(ws.work_dir(c.rank) / "crop_plan.json", plan)
        plans[c.rank] = plan
        frac = (i + 1) / len(todo)
        reporter.progress(frac, timer.eta(frac), f"clip {c.rank}: {', '.join(s.mode for s in plan.segments)}")
    reporter.stage("reframe", "done")

    # --- captions
    reporter.stage("captions", "running")
    ass_paths: dict[int, Path] = {}
    for c, _ in todo:
        script = captions.resolve_style(cfg.captions, c.caption_style).script
        cw = captions.clip_words(words, c, script)
        p = ws.work_dir(c.rank) / "captions.ass"
        p.write_text(captions.build_ass(cw, c, cfg.captions, cfg.render.width, cfg.render.height), encoding="utf-8-sig")
        ass_paths[c.rank] = p
    reporter.stage("captions", "done", f"{len(ass_paths)} subtitle files")

    # --- render
    reporter.stage("render", "running", f"{len(todo)} clip(s)")
    if cfg.render.workers == "auto":
        workers = 1 if nvenc_available() else max(1, min(4, cpu_count() // 4))
    else:
        workers = max(1, int(cfg.render.workers))
    progress = {c.rank: 0.0 for c, _ in todo}
    timer = Timer()
    lock = threading.Lock()

    def _report() -> None:
        frac = sum(progress.values()) / len(progress)
        done = sum(1 for v in progress.values() if v >= 1)
        reporter.progress(frac, timer.eta(frac), f"{done}/{len(progress)} clips rendered")

    def _one(c: ClipCandidate, fp: str) -> Path:
        work = ws.work_dir(c.rank)
        out = ws.clip_path(c.rank, c.title)
        with _render_lock:  # keep one previous version for "undo"
            for old in ws.clips_dir.glob(f"{c.rank:02d}_*.mp4"):
                if (work / "render.json").exists():
                    shutil.copy2(work / "render.json", work / "previous.json")
                    old.replace(work / "previous.mp4")
                else:
                    old.unlink(missing_ok=True)

        def on_prog(f: float) -> None:
            with lock:
                progress[c.rank] = f
            _report()

        rnd.render_clip(ws.source, c.start, c.end, plans[c.rank], ass_paths[c.rank], out, work, cfg,
                        on_progress=on_prog, cancel=reporter.cancelled)
        try:
            extract_frame(out, min(1.0, c.duration / 2), work / "thumb.jpg", width=360)
        except Exception as exc:
            log.warning(f"thumbnail failed for clip {c.rank}: {exc}")
        ws.write_json(work / "render.json", {"fingerprint": fp, "file": out.name, "candidate": c.model_dump()})
        with lock:
            progress[c.rank] = 1.0
        _report()
        cb = getattr(reporter, "clip_done", None)
        if cb:
            cb(c.rank)
        return out

    outputs: list[Path] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one, c, fp): c for c, fp in todo}
        for f in as_completed(futs):
            c = futs[f]
            try:
                outputs.append(f.result())
            except Exception as exc:
                from clipforge.utils.log import CancelledError

                if isinstance(exc, CancelledError):
                    for other in futs:
                        other.cancel()
                    raise
                errors.append(f"clip {c.rank}: {exc}")
                log.error(f"Render failed for clip {c.rank}: {exc}")
    if errors:
        reporter.stage("render", "error", "; ".join(e.splitlines()[0] for e in errors))
        raise RuntimeError(f"{len(errors)} clip(s) failed to render:\n" + "\n\n".join(errors))
    reporter.stage("render", "done", f"{len(outputs)} clip(s)")
    return outputs


def metadata(ws: Workspace, cfg: Config, reporter: ProgressReporter | None = None) -> list[ClipMetadata]:
    """metadata.csv + metadata.json + gallery.html for all rendered clips (LLM copy cached per clip)."""
    from clipforge.stages import metadata as md

    reporter = reporter or NullReporter()
    info = load_info(ws)
    words = load_transcript(ws).all_words()
    llm = get_llm(cfg, required=False)
    metas: list[ClipMetadata] = []
    for c in load_candidates(ws):
        if not c.enabled:
            continue
        f = ws.find_clip(c.rank)
        cache = ws.work_dir(c.rank) / "meta.json"
        fp = fingerprint("meta-v2", c.title, c.hook, c.start, c.end, info.title, info.url, cfg.llm.provider, cfg.llm.model)
        m = None
        if cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("fingerprint") == fp:
                m = ClipMetadata.model_validate(data["meta"])
                m.file, m.rank, m.score = (f.name if f else ""), c.rank, c.score
        if m is None:
            m, by_llm = md.build(c, f, info, words, llm, cfg.captions.script)
            if by_llm:  # template fallbacks are regenerated next time (the LLM may be back)
                ws.write_json(cache, {"fingerprint": fp, "meta": m.model_dump()})
        metas.append(m)
    md.write(metas, ws.metadata_csv, ws.metadata_json)
    ws.gallery.write_text(md.gallery_html(info, [m for m in metas if m.file]), encoding="utf-8")
    return metas


def build_gallery(ws: Workspace) -> Path:
    from clipforge.stages import metadata as md

    if not ws.metadata_json.exists():
        raise FileNotFoundError("No metadata yet — render the clips first.")
    metas = [ClipMetadata.model_validate(m) for m in json.loads(ws.metadata_json.read_text(encoding="utf-8"))]
    ws.gallery.write_text(md.gallery_html(load_info(ws), [m for m in metas if m.file]), encoding="utf-8")
    return ws.gallery


def run_all(source: str, cfg: Config, reporter: ProgressReporter | None = None,
            section: tuple[float, float] | None = None, force: bool = False, video_id: str | None = None) -> Workspace:
    reporter = reporter or NullReporter()
    ws = prepare(source, cfg, reporter, section, video_id)
    select(ws, cfg, reporter, force=force)
    render(ws, cfg, reporter, force=force)
    metadata(ws, cfg, reporter)
    return ws
