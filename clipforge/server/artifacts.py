"""Moving workspace files between the server and a worker (Phase 15b).

* Only files of the normal workspace layout can move (`ALLOWED`): no absolute paths, no `..`, no other folders —
  a worker key can never read or write anything else on the server (path traversal is tested).
* Pull: the server lists `manifest()` (path, size, sha256 — hashes cached by size+mtime); the worker downloads only what
  differs (HTTP Range resumes a broken download).
* Push: the worker uploads into a staging folder per task + fence (`PUT …?offset=`, resumable, re-sending a chunk is
  safe), then `commit()` checks every size + sha256 and only then moves the files into the workspace. A checksum
  mismatch rejects the whole result; nothing half-written ever lands in the workspace.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from clipforge.workspace import Workspace, safe_replace

ALLOWED = re.compile(
    r"^(?:source\.mp4|audio\.wav|info\.json|thumbnail\.jpg|transcript\.json|scenes\.json|energy\.json|"
    r"llm_candidates\.json|candidates\.json|metadata\.json|metadata\.csv|gallery\.html|"
    r"select_cache/[\w.-]{1,120}\.json|clips/\d{2}_[\w.-]{1,160}\.mp4|clips/_work/\d{2}/[\w.-]{1,80})$")
MAX_FILE = 20 * 1024 ** 3  # 20 GB — a 3 h 1080p source is ~4 GB
PULL_SETS = {  # what each stage needs from the server
    "download": [],
    "transcribe": ["info.json", "audio.wav"],
    "select": ["info.json", "transcript.json", "energy.json", "candidates.json", "llm_candidates.json", "select_cache/"],
    "render": ["*"],
    "import": [],
}
STATE_KEYS = {"download", "transcribe", "analyze", "select"}


class ArtifactError(ValueError):
    pass


def check_path(rel: str) -> str:
    rel = rel.replace("\\", "/")
    if not rel or len(rel) > 300 or rel.startswith("/") or ":" in rel or any(p in ("", ".", "..") for p in rel.split("/")):
        raise ArtifactError("bad path")
    if not ALLOWED.match(rel):
        raise ArtifactError(f"'{rel}' isn't a workspace file")
    return rel


def resolve(root: Path, rel: str) -> Path:
    """Path inside `root` — validated twice (pattern + resolved parent check)."""
    rel = check_path(rel)
    base = root.resolve()
    target = (root / rel).resolve()
    if base not in target.parents:
        raise ArtifactError("bad path")
    return target


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_cache(ws_dir: Path) -> tuple[Path, dict]:
    p = ws_dir / ".hashes.json"
    try:
        return p, json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return p, {}


def manifest(ws: Workspace, stage: str = "render") -> list[dict]:
    """Files of the workspace a stage needs, with sha256 (cached by size + mtime: a 4 GB source is hashed once)."""
    wanted = PULL_SETS.get(stage, ["*"])
    cache_p, cache = _hash_cache(ws.dir)
    out, changed = [], False
    for f in sorted(ws.dir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(ws.dir).as_posix()
        if not ALLOWED.match(rel):
            continue
        if "*" not in wanted and not any(rel == w or (w.endswith("/") and rel.startswith(w)) for w in wanted):
            continue
        st = f.stat()
        key = f"{st.st_size}:{st.st_mtime_ns}"
        rec = cache.get(rel)
        if not rec or rec["key"] != key:
            rec = {"key": key, "sha256": sha256(f)}
            cache[rel] = rec
            changed = True
        out.append({"path": rel, "size": st.st_size, "sha256": rec["sha256"]})
    if changed:
        cache_p.write_text(json.dumps(cache), encoding="utf-8")
    return out


def local_sha(root: Path, rel: str) -> str | None:
    """sha256 of a file in a workspace (cached the same way) — the worker uses it to skip identical files."""
    f = root / rel
    if not f.exists():
        return None
    cache_p, cache = _hash_cache(root)
    st = f.stat()
    key = f"{st.st_size}:{st.st_mtime_ns}"
    rec = cache.get(rel)
    if rec and rec["key"] == key:
        return rec["sha256"]
    digest = sha256(f)
    cache[rel] = {"key": key, "sha256": digest}
    cache_p.write_text(json.dumps(cache), encoding="utf-8")
    return digest


# ============================================================================ staging (server side)
def staging_dir(workspace_root: Path, task_id: int, fence: int) -> Path:
    return workspace_root / "_incoming" / f"{int(task_id)}_{int(fence)}"


def write_chunk(workspace_root: Path, task_id: int, fence: int, rel: str, offset: int, chunks) -> int:
    """Write (an async-free iterable of) bytes at `offset` of a staged file. Returns the new size."""
    stage = staging_dir(workspace_root, task_id, fence)
    target = resolve(stage, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    size = target.stat().st_size if target.exists() else 0
    if offset > size or offset < 0:
        raise ArtifactError(f"expected offset ≤ {size}")
    free = shutil.disk_usage(workspace_root).free
    with open(target, "r+b" if target.exists() else "wb") as f:
        f.truncate(offset)  # a resent chunk after a partial write overwrites the partial data
        f.seek(offset)
        written = offset
        for chunk in chunks:
            written += len(chunk)
            if written > MAX_FILE or len(chunk) > free - 1024 ** 3:
                f.truncate(offset)
                raise ArtifactError("file too large for the server's disk")
            f.write(chunk)
    return target.stat().st_size


def staged_size(workspace_root: Path, task_id: int, fence: int, rel: str) -> int:
    t = resolve(staging_dir(workspace_root, task_id, fence), rel)
    return t.stat().st_size if t.exists() else 0


def commit(ws: Workspace, workspace_root: Path, task_id: int, fence: int, files: list[dict],
           state: dict | None = None, deleted: list[str] | None = None) -> int:
    """Verify every staged file (size + sha256), then move them into the workspace and merge the stage state.
    All-or-nothing: one bad checksum and nothing is moved."""
    stage = staging_dir(workspace_root, task_id, fence)
    checked: list[tuple[Path, Path]] = []
    for f in files:
        rel = check_path(f["path"])
        src = resolve(stage, rel)
        if not src.exists():
            raise ArtifactError(f"{rel} was not uploaded")
        if src.stat().st_size != int(f["size"]):
            raise ArtifactError(f"{rel}: size {src.stat().st_size} ≠ {f['size']} (upload incomplete — resume it)")
        if sha256(src) != f["sha256"]:
            src.unlink(missing_ok=True)
            raise ArtifactError(f"{rel}: checksum mismatch — the file was damaged on the way; upload it again")
        checked.append((src, resolve(ws.dir, rel)))
    for src, dst in checked:
        dst.parent.mkdir(parents=True, exist_ok=True)
        safe_replace(src, dst)
    for rel in deleted or []:  # e.g. a re-render replaced 03_old-title.mp4 by 03_new-title.mp4
        try:
            p = resolve(ws.dir, rel)
        except ArtifactError:
            continue
        p.unlink(missing_ok=True)
    if state:
        st = ws._state()
        for k, v in state.items():
            if k in STATE_KEYS and isinstance(v, dict) and "fingerprint" in v:
                st[k] = {"fingerprint": str(v["fingerprint"]), "done_at": float(v.get("done_at", 0))}
        ws.write_json(ws.state_file, st)
    shutil.rmtree(stage, ignore_errors=True)
    return len(checked)


def discard(workspace_root: Path, task_id: int) -> None:
    for d in (workspace_root / "_incoming").glob(f"{int(task_id)}_*"):
        shutil.rmtree(d, ignore_errors=True)


# ============================================================================ worker side helpers
def snapshot(ws_dir: Path) -> dict[str, tuple[int, int]]:
    out = {}
    if not ws_dir.exists():
        return out
    for f in ws_dir.rglob("*"):
        if f.is_file():
            rel = f.relative_to(ws_dir).as_posix()
            if ALLOWED.match(rel):
                st = f.stat()
                out[rel] = (st.st_size, st.st_mtime_ns)
    return out


def changed_since(ws_dir: Path, before: dict[str, tuple[int, int]]) -> tuple[list[str], list[str]]:
    """(new or modified files, deleted files) since `before`."""
    after = snapshot(ws_dir)
    changed = [p for p, v in after.items() if before.get(p) != v]
    deleted = [p for p in before if p not in after]
    return sorted(changed), sorted(deleted)
